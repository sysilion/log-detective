"""Watch mode: scheduled anomaly questions with deterministic gating, deduplicated incidents and alerts.

Each check runs the cheap statistics first (5xx rate, p95 vs baseline, new error signatures, anomalous
minutes). Only when something is flagged *and* it is a new incident does it ask the LLM for a cited
summary. That saves free-tier quota. Findings go to LOGDET_HOME/findings.jsonl, and open incidents to
watch_state.json, so a restart or a Sprite pause/resume never re-alerts the same incident.

Config: LOGDET_WATCH_CONFIG (TOML or JSON), else LOGDET_HOME/logdet.toml, else ./logdet.toml, else one
default watch. Example:

    [alerts]
    ui_url = "https://log-detective-xxxx.sprites.app"

    [[watch]]
    name = "checkout-health"
    question = "Are there any error spikes or latency regressions in the last 15 minutes?"
    interval = "1m"
    lookback = "15m"
    [watch.thresholds]
    error_rate = 0.05          # window 5xx rate
    p95_multiplier = 3.0       # window p95 / baseline p95
    new_signatures = 1         # how many new error signatures (each seen >= min_signature_count times)
    min_signature_count = 5
    min_bad_minutes = 2        # anomalous minutes before alerting on the latency/5xx shape
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .alerts import notify
from .analyze import analyze
from .config import _env, parse_now
from .timeparse import TimeRange

DEFAULT_QUESTION = "Are there any error spikes or latency regressions in the last 15 minutes?"


def parse_duration(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(v))
    if not m:
        raise ValueError(f"bad duration: {v!r} (use e.g. 30s, 15m, 1h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


@dataclass
class Watch:
    name: str = "default"
    question: str = DEFAULT_QUESTION
    interval: float = 60.0
    lookback: float = 900.0
    error_rate: float = 0.05
    p95_multiplier: float = 3.0
    new_signatures: int = 1
    min_signature_count: int = 5
    min_bad_minutes: int = 2   # anomalous minutes needed before alerting on latency/5xx shape alone
    use_llm: bool = True


@dataclass
class WatchConfig:
    watches: list[Watch] = field(default_factory=lambda: [Watch()])
    alerts: dict = field(default_factory=dict)
    path: str | None = None


def load_config(home: Path) -> WatchConfig:
    cands = [_env("LOGDET_WATCH_CONFIG"), str(Path(home) / "logdet.toml"), "logdet.toml", "logdet.json"]
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        raw = Path(c).read_bytes()
        if c.endswith(".json"):
            data = json.loads(raw)
        else:
            try:
                import tomllib
            except ImportError as e:  # Python 3.10
                raise RuntimeError("TOML watch config needs Python 3.11+; use a .json file instead") from e
            data = tomllib.loads(raw.decode())
        watches = []
        for w in data.get("watch") or data.get("watches") or []:
            th = w.get("thresholds") or {}
            watches.append(Watch(
                name=str(w.get("name", f"watch{len(watches) + 1}")), question=str(w.get("question", DEFAULT_QUESTION)),
                interval=parse_duration(w.get("interval", "1m")), lookback=parse_duration(w.get("lookback", "15m")),
                error_rate=float(th.get("error_rate", 0.05)), p95_multiplier=float(th.get("p95_multiplier", 3.0)),
                new_signatures=int(th.get("new_signatures", 1)),
                min_signature_count=int(th.get("min_signature_count", 5)),
                min_bad_minutes=int(th.get("min_bad_minutes", 2)), use_llm=bool(w.get("use_llm", True))))
        return WatchConfig(watches or [Watch()], dict(data.get("alerts") or {}), c)
    return WatchConfig()


def evaluate(store, w: Watch, now_ts: float, tz=None) -> dict:
    """Deterministic check. Returns {flagged, severity, reasons, stats, incident}."""
    start = now_ts - w.lookback
    st = analyze(store, start, now_ts)
    win, base = st.window, st.baseline
    reasons, crit = [], False
    if win.requests >= 20 and win.error_rate >= w.error_rate:
        reasons.append(f"5xx rate {win.error_rate:.1%} ≥ {w.error_rate:.0%}")
        crit |= win.error_rate >= 2 * w.error_rate
    if base.p95 and win.p95 and win.p95 / base.p95 >= w.p95_multiplier:
        ratio = win.p95 / base.p95
        reasons.append(f"p95 {win.p95:.0f}ms is {ratio:.1f}x baseline {base.p95:.0f}ms")
        crit |= ratio >= 2 * w.p95_multiplier
    new = [s for s in st.signatures if s["new"] and s["count"] >= w.min_signature_count]
    if len(new) >= w.new_signatures and new:
        reasons.append(f"{len(new)} new error signature(s): " + "; ".join(s["sig"][:80] for s in new[:2]))
    if st.anomaly and st.anomaly["bad_minutes"] >= w.min_bad_minutes:
        a = st.anomaly
        since = datetime.fromtimestamp(a["start"], tz).strftime("%H:%M")
        reasons.append(f"{a['bad_minutes']} anomalous minute(s) since {since}, peak p95 {a['peak_p95'] or 0:.0f}ms")
    flagged = bool(reasons)
    first = min([s["first_ts"] for s in new] + ([st.anomaly["start"]] if st.anomaly else []), default=None)
    return {"flagged": flagged, "severity": "critical" if crit else "warning", "reasons": reasons,
            "stats": st, "first_ts": first, "window": [start, now_ts]}


class Watcher:
    """Runs watches, deduplicates incidents, stores findings and sends alerts."""

    def __init__(self, det, wcfg: WatchConfig | None = None, log=print, notify_fn=notify):
        self.det, self.cfg = det, det.cfg
        self.wcfg = wcfg or load_config(det.cfg.home)
        self.log, self.notify_fn = log, notify_fn
        self.findings_path = Path(det.cfg.home) / "findings.jsonl"
        self.state_path = Path(det.cfg.home) / "watch_state.json"
        self.lock = threading.Lock()
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.ui_url = _env("LOGDET_PUBLIC_URL") or str(self.wcfg.alerts.get("ui_url") or "")

    # ------------------------------------------------------------ persistence
    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, self.state_path)

    def _append(self, f: dict) -> None:
        with open(self.findings_path, "a") as fh:
            fh.write(json.dumps(f, ensure_ascii=False, default=str) + "\n")

    def reset(self) -> None:
        self.state = {}
        self._save_state()
        self.findings_path.write_text("")

    # ------------------------------------------------------------ one check
    def check(self, w: Watch, now_ts: float, replay: bool = False, db_lock=None) -> dict | None:
        """Run one watch at `now_ts`. Returns the new finding (alert or resolved), or None."""
        lock = db_lock or threading.Lock()
        with lock:
            ev = evaluate(self.det.store, w, now_ts, self.cfg.tz)
        open_inc = self.state.get(w.name)
        if ev["flagged"]:
            if open_inc:  # same incident still ongoing: no new alert
                open_inc["last_seen"], open_inc["checks"] = now_ts, open_inc.get("checks", 1) + 1
                self._save_state()
                return None
            finding = self._new_alert(w, ev, now_ts, replay, lock)
            self.state[w.name] = {"incident": finding["incident"], "opened": now_ts, "last_seen": now_ts,
                                  "finding": finding["id"], "checks": 1, "severity": finding["severity"]}
            self._save_state()
            return finding
        if open_inc:
            del self.state[w.name]
            self._save_state()
            f = {"id": uuid.uuid4().hex[:12], "type": "resolved", "watch": w.name, "severity": "resolved",
                 "incident": open_inc["incident"], "created": time.time(), "at": now_ts,
                 "window": [open_inc["opened"], now_ts], "replay": replay,
                 "reasons": [f"back to normal after {int((now_ts - open_inc['opened']) / 60)} min"],
                 "summary": f"{w.name}: metrics are back within thresholds.", "evidence": []}
            self._append(f)
            self._deliver(f)
            return f
        return None

    def _new_alert(self, w: Watch, ev: dict, now_ts: float, replay: bool, lock) -> dict:
        tz = self.cfg.tz
        start, end = ev["window"]
        tr = TimeRange(datetime.fromtimestamp(start, tz), datetime.fromtimestamp(end, tz),
                       f"watch {w.name}", explicit=True)
        with lock:
            inv = self.det.prepare(w.question, now=datetime.fromtimestamp(end, tz), tr=tr)
        res = self.det.answer(inv, use_llm=w.use_llm)  # LLM only here: flagged + new incident
        if w.use_llm and res.get("llm_error"):  # one alert = one LLM call; worth one retry on a hiccup
            time.sleep(3)
            res = self.det.answer(inv, use_llm=True)
        first = ev["first_ts"] or start
        incident = hashlib.sha1(f"{w.name}|{int(first // 60)}".encode()).hexdigest()[:10]
        f = {"id": uuid.uuid4().hex[:12], "type": "alert", "watch": w.name, "severity": ev["severity"],
             "incident": incident, "created": time.time(), "at": now_ts, "window": [start, end],
             "first_ts": first, "replay": replay, "reasons": ev["reasons"], "question": w.question,
             "summary": res["answer"], "llm": res["llm"], "llm_fallback": bool(res.get("llm_error")),
             "evidence": [{k: e.get(k) for k in ("cite", "source", "lineno", "ts", "line")}
                          for e in res["evidence"][:8]]}
        self._append(f)
        self._deliver(f)
        self.log(f"[watch] {w.name}: {f['severity']} alert ({'; '.join(ev['reasons'])[:160]})")
        return f

    def _deliver(self, f: dict) -> None:
        if f.get("replay") and _env("LOGDET_ALERT_REPLAY", "0") != "1":
            return  # don't page anyone for a simulated replay unless asked to
        self.notify_fn(f, self.cfg.tz, self.wcfg.alerts, self.ui_url, log=self.log)

    # ------------------------------------------------------------ loops
    def now_ts(self) -> float:
        now = parse_now(self.cfg.now_override, self.cfg.tz)
        return (now.timestamp() if now else time.time())

    def run_once(self, db_lock=None) -> list[dict]:
        now = self.now_ts()
        return [f for w in self.wcfg.watches if (f := self.check(w, now, db_lock=db_lock))]

    def run_forever(self, stop: threading.Event | None = None, db_lock=None) -> None:
        """Live loop. After a Sprite pause, the next tick simply checks the current window again."""
        stop = stop or threading.Event()
        nxt = {w.name: 0.0 for w in self.wcfg.watches}
        while not stop.is_set():
            t = time.time()
            for w in self.wcfg.watches:
                if t >= nxt[w.name]:
                    nxt[w.name] = t + w.interval
                    try:
                        self.check(w, self.now_ts(), db_lock=db_lock)
                    except Exception as e:
                        self.log(f"[watch] {w.name}: check failed: {str(e)[:200]}")
            stop.wait(1.0)

    def replay(self, start_ts: float, end_ts: float, speed: float = 0.0, step: float | None = None,
               stop: threading.Event | None = None) -> list[dict]:
        """Replay a past period on a simulated clock: each step checks [t - lookback, t] using only data
        that existed at t. speed = simulated seconds per real second (0 = as fast as possible)."""
        out, t = [], start_ts
        step = step or min(w.interval for w in self.wcfg.watches)
        while t <= end_ts and not (stop and stop.is_set()):
            for w in self.wcfg.watches:
                f = self.check(w, t, replay=True)
                if f:
                    out.append(f)
            if speed > 0:
                time.sleep(step / speed)
            t += step
        return out


def recent_findings(home: Path, limit: int = 30) -> list[dict]:
    p = Path(home) / "findings.jsonl"
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        if len(out) >= limit:
            break
    return out


def default_replay_range(store, lookback: float) -> tuple[float, float]:
    rng = store.time_range()
    if not rng:
        raise RuntimeError("no data to replay; ingest something first")
    return rng[0] + lookback, rng[1] + 60


