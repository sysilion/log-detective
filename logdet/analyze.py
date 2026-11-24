"""Deterministic statistics: latency/error spikes and new error signatures vs a baseline."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .store import Store

CHANGE_RE = re.compile(
    r"\b(deploy(?:ed|ing|ment)?|release[d]?|rollout|rolled|restart(?:ed|ing)?|start(?:ed|ing)|"
    r"migrat\w*|config(?:uration)? (?:change|reload)\w*|scal(?:ed|ing)|cron|batch|job|"
    r"failover|promot\w*|oom\w*|killed|upgrade\w*|feature flag)\b", re.I)


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]


@dataclass
class Window:
    start: float
    end: float
    lines: int = 0
    requests: int = 0
    errors: int = 0
    error_rate: float = 0.0
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None


@dataclass
class Stats:
    window: Window
    baseline: Window
    timeline: list[dict] = field(default_factory=list)
    anomaly: dict | None = None
    signatures: list[dict] = field(default_factory=list)
    paths: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _requests(rows) -> list:
    reqs = [r for r in rows if r["status"] is not None]
    nginx = [r for r in reqs if r["service"] == "nginx"]
    return nginx or reqs


def _window(rows, start: float, end: float) -> Window:
    reqs = _requests(rows)
    lat = [r["latency_ms"] for r in (reqs or rows) if r["latency_ms"] is not None]
    w = Window(start, end, lines=len(rows), requests=len(reqs))
    w.errors = sum(1 for r in reqs if r["status"] >= 500) if reqs else sum(r["is_error"] for r in rows)
    denom = len(reqs) or len(rows)
    w.error_rate = w.errors / denom if denom else 0.0
    w.p50, w.p95, w.p99 = pct(lat, .5), pct(lat, .95), pct(lat, .99)
    return w


def _is_problem(r) -> bool:
    if r["service"] == "nginx":
        return r["status"] is not None and r["status"] >= 500
    return bool(r["is_error"]) or r["level"] in ("WARN", "WARNING")


def analyze(store: Store, start: float, end: float) -> Stats:
    rows = store.events(start, end)
    span = max(end - start, 1800.0)
    base_rows = store.events(start - span, start)
    b_start = start - span
    base_len = span
    if len(base_rows) < 20:  # not enough history right before: use everything outside the window
        rng = store.time_range()
        if rng:
            base_rows = store.events(rng[0], start) + store.events(end, rng[1] + 1)
            b_start = min(rng[0], start)
            base_len = max(start - rng[0], 0) + max(rng[1] + 1 - end, 0)
    st = Stats(_window(rows, start, end), _window(base_rows, b_start, start))
    b = st.baseline

    # per-minute timeline
    buckets: dict[int, list] = {}
    for r in _requests(rows) or rows:
        buckets.setdefault(int(r["ts"] // 60) * 60, []).append(r)
    for t in sorted(buckets):
        rs = buckets[t]
        lat = [r["latency_ms"] for r in rs if r["latency_ms"] is not None]
        err = sum(1 for r in rs if (r["status"] or 0) >= 500 or (r["status"] is None and r["is_error"]))
        st.timeline.append({"t": t, "n": len(rs), "err": err, "p95": pct(lat, .95)})

    # anomaly = minutes clearly worse than baseline
    bp95 = b.p95 or (st.window.p50 or 0)
    bad = []
    for m in st.timeline:
        slow = m["p95"] is not None and bp95 and m["p95"] > max(2.5 * bp95, bp95 + 200)
        erry = m["n"] >= 5 and m["err"] / m["n"] > b.error_rate + 0.05
        if slow or erry:
            bad.append(m)
    if bad:
        peak = max(bad, key=lambda m: (m["p95"] or 0))
        st.anomaly = {"start": bad[0]["t"], "end": bad[-1]["t"] + 60, "peak": peak["t"],
                      "peak_p95": peak["p95"], "bad_minutes": len(bad),
                      "peak_error_rate": max(m["err"] / m["n"] for m in bad if m["n"])}

    # error signatures: window vs baseline, rate-normalized
    win_len, base_len = max(end - start, 1), max(base_len, 1)
    base_sig: dict[str, int] = {}
    for r in base_rows:
        if _is_problem(r):
            base_sig[r["sig"]] = base_sig.get(r["sig"], 0) + 1
    sigs: dict[str, dict] = {}
    for r in rows:
        if not _is_problem(r):
            continue
        s = sigs.setdefault(r["sig"], {"sig": r["sig"], "count": 0, "first_ts": r["ts"], "level": r["level"],
                                       "source": r["source"], "first_event_id": r["id"]})
        s["count"] += 1
    for s in sigs.values():
        bc = base_sig.get(s["sig"], 0)
        s["base_count"] = bc
        s["new"] = bc == 0
        s["ratio"] = (s["count"] / win_len) / (bc / base_len) if bc else None
    st.signatures = sorted(
        sigs.values(), key=lambda s: (not s["new"] and (s["ratio"] or 0) < 3, -s["count"]))[:10]

    # slowest paths vs baseline
    def by_path(rs):
        d: dict[str, list] = {}
        for r in _requests(rs):
            if r["path"] and r["latency_ms"] is not None:
                d.setdefault(re.sub(r"/\d+", "/:id", r["path"]), []).append(r["latency_ms"])
        return d
    wp, bpth = by_path(rows), by_path(base_rows)
    st.paths = sorted(
        ({"path": p, "n": len(v), "p95": pct(v, .95), "base_p95": pct(bpth.get(p, []), .95)}
         for p, v in wp.items() if len(v) >= 5),
        key=lambda d: -((d["p95"] or 0) / (d["base_p95"] or d["p95"] or 1)))[:5]

    # change events shortly before / during the window
    for r in store.events(start - 1800, end):
        if r["service"] != "nginx" and CHANGE_RE.search(r["msg"] or ""):
            st.changes.append({"ts": r["ts"], "source": r["source"], "lineno": r["lineno"],
                               "msg": r["msg"][:200], "id": r["id"]})
    st.changes = st.changes[:15]
    return st
