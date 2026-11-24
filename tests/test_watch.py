"""Watch mode: gating, dedupe across checks and restarts, resolve, LLM only on new incidents, replay, config."""
import json
import os
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest

from logdet.agent import Detective
from logdet.config import Config
from logdet.sample import generate
from logdet.watch import Watch, WatchConfig, Watcher, evaluate, load_config, parse_duration, recent_findings

KST = ZoneInfo("Asia/Seoul")
T = lambda h, m: datetime(2026, 11, 23, h, m, 30, tzinfo=KST).timestamp()  # noqa: E731


@pytest.fixture(scope="module")
def det(tmp_path_factory):
    root = tmp_path_factory.mktemp("watch")
    generate(root / "logs", datetime(2026, 11, 23), KST, hours=6)
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm, cfg.retention_days = \
        root / "home", KST, "local", "hash", "none", 0
    d = Detective(cfg)
    d.ingest([str(root / "logs")], reset=True, log=lambda *_: None)
    return d


@pytest.fixture()
def watcher(det):
    sent = []
    w = Watcher(det, WatchConfig([Watch(name="checkout", interval=60, lookback=900)]), log=lambda *_: None,
                notify_fn=lambda f, tz, fc, ui, log=None: sent.append(f) or {})
    w.reset()
    w.sent = sent
    return w


def test_parse_duration():
    assert parse_duration("30s") == 30 and parse_duration("15m") == 900 and parse_duration("1h") == 3600
    assert parse_duration(45) == 45
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_evaluate_quiet_vs_incident(det):
    w = Watch()
    assert not evaluate(det.store, w, T(1, 0), KST)["flagged"]  # quiet hour (payment warnings < min count)
    ev = evaluate(det.store, w, T(3, 5), KST)
    assert ev["flagged"] and any("5xx" in r or "anomalous" in r for r in ev["reasons"])


def test_dedupe_resolve_and_llm_only_on_new_incident(det, watcher, monkeypatch):
    calls = []
    real = det.answer
    monkeypatch.setattr(det, "answer", lambda inv, use_llm=True: calls.append(1) or real(inv, use_llm))
    assert watcher.check(watcher.wcfg.watches[0], T(2, 30)) is None
    f1 = watcher.check(watcher.wcfg.watches[0], T(3, 1))
    assert f1 and f1["type"] == "alert" and f1["evidence"] and len(calls) == 1
    for m in range(2, 20):  # incident ongoing: no duplicate alerts, no extra LLM calls
        assert watcher.check(watcher.wcfg.watches[0], T(3, m)) is None
    # a restart (new Watcher reading the same state file) must not re-alert the same incident
    w2 = Watcher(det, watcher.wcfg, log=lambda *_: None, notify_fn=lambda *a, **k: {})
    assert w2.check(w2.wcfg.watches[0], T(3, 21)) is None
    assert len(calls) == 1
    r = w2.check(w2.wcfg.watches[0], T(3, 50))  # 15-min window past recovery
    assert r and r["type"] == "resolved"
    kinds = [f["type"] for f in recent_findings(det.cfg.home)]
    assert kinds == ["resolved", "alert"] and len(watcher.sent) == 1


def test_replay_detects_incident_within_two_minutes(det, watcher):
    found = watcher.replay(T(2, 0), T(4, 0), speed=0)
    alerts = [f for f in found if f["type"] == "alert"]
    assert len(alerts) == 1 and alerts[0]["replay"]
    onset = datetime(2026, 11, 23, 2, 59, tzinfo=KST).timestamp()
    assert onset <= alerts[0]["at"] <= onset + 180
    assert not watcher.sent  # replays don't page anyone by default


def test_load_toml_config(tmp_path, monkeypatch):
    p = tmp_path / "logdet.toml"
    p.write_text('[alerts]\nui_url = "https://x"\n\n[[watch]]\nname = "api"\ninterval = "30s"\nlookback = "10m"\n'
                 '[watch.thresholds]\nerror_rate = 0.1\np95_multiplier = 4\n')
    monkeypatch.setenv("LOGDET_WATCH_CONFIG", str(p))
    c = load_config(tmp_path)
    w = c.watches[0]
    assert (w.name, w.interval, w.lookback, w.error_rate, w.p95_multiplier) == ("api", 30, 600, 0.1, 4.0)
    assert c.alerts["ui_url"] == "https://x"


def test_keep_awake_holds_and_releases_task(tmp_path):
    from logdet.keepawake import KeepAwake
    import tempfile
    sock_path = os.path.join(tempfile.mkdtemp(prefix="ld", dir="/tmp"), "api.sock")  # AF_UNIX path length limit
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self, code):
            n = int(self.headers.get("Content-Length") or 0)
            calls.append((self.command, self.path, json.loads(self.rfile.read(n)) if n else None))
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self):
            self._ok(200)

        def do_DELETE(self):
            self._ok(204)

    class UnixServer(HTTPServer):
        address_family = socket.AF_UNIX

        def server_bind(self):
            self.socket.bind(self.server_address)

        def get_request(self):
            req, _ = self.socket.accept()
            return req, ("local", 0)

    srv = UnixServer(sock_path, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with KeepAwake("watch", enabled=True, sock=sock_path, refresh=0.05, log=lambda *_: None):
            for _ in range(100):
                if len(calls) >= 2:
                    break
                threading.Event().wait(0.02)
    finally:
        srv.shutdown()
    assert calls[0] == ("PUT", "/v1/tasks/logdet-watch", {"expire": "5m"})
    assert calls[-1][:2] == ("DELETE", "/v1/tasks/logdet-watch")
    assert not KeepAwake("x", enabled=True, sock=str(tmp_path / "missing.sock")).active
