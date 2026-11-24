"""HTTP-level tests for the public endpoint: routing, input validation, rate limiting, caching, streaming."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logdet.agent import Detective
from logdet.config import Config
from logdet.sample import generate
from logdet.web import AskGuard, make_server

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(scope="module")
def det(tmp_path_factory):
    root = tmp_path_factory.mktemp("web")
    generate(root / "logs", datetime(2026, 11, 23), KST, hours=4)
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm = root / "home", KST, "local", "hash", "none"
    cfg.now_override = "2026-11-24T09:00"
    d = Detective(cfg)
    d.ingest([str(root / "logs")], reset=True, log=lambda *_: None)
    return d


@pytest.fixture()
def server(det):
    guard = AskGuard(per_ip_per_hour=4, daily_budget=100)
    srv = make_server(det.cfg, "127.0.0.1", 0, det=det, guard=guard)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", guard
    srv.shutdown()


def call(url, method="GET", body=None, headers=None, raw=False):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read().decode()
            return r.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_routes_and_health(server):
    base, _ = server
    assert call(base + "/", raw=True)[0] == 200
    code, h = call(base + "/api/health")
    assert code == 200 and h["events"] > 0 and h["now"] == "2026-11-24T09:00"
    assert call(base + "/api/ingest", "POST", {"q": "x"})[0] == 404
    assert call(base + "/api/ask")[0] == 404
    assert call(base + "/admin")[0] == 404


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b'{"q": "   "}', b'{"q": "' + b"x" * 5000 + b'"}'])
def test_bad_input_is_400(server, body):
    base, _ = server
    assert call(base + "/api/ask", "POST", body)[0] == 400


def test_ask_hides_upstream_errors(server):
    base, _ = server
    code, res = call(base + "/api/ask", "POST", {"q": "Why did checkout slow down at 3am yesterday?"})
    assert code == 200 and res["stats"]["anomaly"]
    assert "llm_error" not in res and "retrieval_error" not in res
    assert res["llm_fallback"] is False and res["retrieval_fallback"] is False


def test_stream_framing(server):
    base, _ = server
    code, raw = call(base + "/api/ask/stream", "POST", {"q": "어제 새벽 3시에 왜 느려졌어?"}, raw=True)
    events = [json.loads(line) for line in raw.splitlines()]
    assert code == 200 and events[0]["type"] == "meta" and events[-1]["type"] == "done"
    assert "answer" not in events[0] and events[0]["evidence"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == events[-1]["answer"]
    assert "llm_error" not in events[-1]


def test_rate_limit_and_cache(server):
    base, guard = server
    hdr = lambda i: {"X-Forwarded-For": f"6.6.6.{i}, 203.0.113.7"}  # noqa: E731 spoofed left-most hop
    codes = [call(base + "/api/ask", "POST", {"q": f"what happened at {i}am yesterday?"}, hdr(i))[0]
             for i in range(1, 6)]
    assert codes == [200, 200, 200, 200, 429]  # spoofing the left-most XFF entry doesn't reset the limit
    # a cached question is served without spending quota
    assert call(base + "/api/ask", "POST", {"q": "What happened at 1am yesterday?"}, hdr(9))[0] == 200


def test_degraded_answers_are_not_cached(det, server):
    base, guard = server
    det.cfg.llm, det.cfg.ollama_host = "ollama-cloud", "http://127.0.0.1:9"  # no key, no gateway -> error
    try:
        code, res = call(base + "/api/ask", "POST", {"q": "why slow at 3am yesterday"})
    finally:
        det.cfg.llm = "none"
    assert code == 200 and res["llm_fallback"] is True and res["llm"].startswith("rule-based")
    assert guard.cached("why slow at 3am yesterday") is None
