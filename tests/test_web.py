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


@pytest.fixture()
def ingest_server(tmp_path, request):
    per_minute = getattr(request, "param", 5)
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm, cfg.retention_days = \
        tmp_path / "h", KST, "local", "hash", "none", 0
    cfg.ingest_token, cfg.ingest_max_bytes, cfg.ingest_per_minute = "s3cret-token", 2000, per_minute
    d = Detective(cfg)
    srv = make_server(cfg, "127.0.0.1", 0, det=d, guard=AskGuard(100, 100))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", d
    srv.shutdown()


def test_ingest_disabled_without_token(server):
    base, _ = server
    assert call(base + "/api/ingest?source=x", "POST", b"hello", {"Authorization": "Bearer anything"})[0] == 404


def test_ingest_auth_limits_and_parsing(ingest_server):
    base, d = ingest_server
    ok = {"Authorization": "Bearer s3cret-token", "Content-Type": "application/x-ndjson"}
    nd = b'{"date": 1795370400.5, "log": "GET /api/cart 503", "level": "error"}\n' \
         b'{"timestamp": "2026-11-23T03:00:01+09:00", "message": "pool exhausted", "level": "WARN"}\n'
    assert call(base + "/api/ingest?source=fb", "POST", nd)[0] == 401
    assert call(base + "/api/ingest?source=fb", "POST", nd, {"Authorization": "Bearer wrong"})[0] == 401
    code, res = call(base + "/api/ingest?source=fb", "POST", nd, ok)
    assert code == 200 and res["accepted"] == 2 and res["windows_upserted"] >= 1
    assert d.store.max_lineno("fb") == 2 and d.vs.items
    assert call(base + "/api/ingest?source=bad/name", "POST", nd, ok)[0] == 400
    assert call(base + "/api/ingest?source=fb", "POST", b"x" * 3000, ok)[0] == 413
    codes = [call(base + "/api/ingest?source=fb", "POST", b"2026-11-23 03:00:09 INFO hi\n", ok)[0]
             for _ in range(5)]
    assert 429 in codes  # 5 per minute, several already used above


@pytest.mark.parametrize("ingest_server", [100], indirect=True)
def test_ingest_is_idempotent_and_atomic(ingest_server):
    base, d = ingest_server
    ok = {"Authorization": "Bearer s3cret-token"}
    batch = b"2026-11-23 03:00:01 ERROR pool exhausted\n2026-11-23 03:00:02 WARN slow\n"
    code, r1 = call(base + "/api/ingest?source=app", "POST", batch, ok)
    assert code == 200 and r1["accepted"] == 2 and r1["duplicates"] == 0
    # plain retry of the same batch: nothing double-counted in the stats store
    code, r2 = call(base + "/api/ingest?source=app", "POST", batch, ok)
    assert code == 200 and r2["accepted"] == 0 and r2["duplicates"] == 2
    assert d.store.max_lineno("app") == 2 and d.store.counts()[0] == 2
    # Idempotency-Key replays the stored response even if the body differs
    hk = {**ok, "Idempotency-Key": "batch-42"}
    _, r3 = call(base + "/api/ingest?source=app", "POST", b"2026-11-23 03:00:05 INFO new\n", hk)
    _, r4 = call(base + "/api/ingest?source=app", "POST", b"2026-11-23 03:00:06 INFO other\n", hk)
    assert r3 == r4 and r3["accepted"] == 1 and d.store.counts()[0] == 3
    # vector store failure: 503 + Retry-After, nothing kept; the retry succeeds without duplicates
    real = d.vs.upsert
    d.vs.upsert = lambda v: (_ for _ in ()).throw(RuntimeError("pinecone down"))
    try:
        req = urllib.request.Request(base + "/api/ingest?source=app", data=b"2026-11-23 03:00:09 ERROR x\n",
                                     method="POST", headers=ok)
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 503 and ei.value.headers["Retry-After"] == "10"
    finally:
        d.vs.upsert = real
    assert d.store.counts()[0] == 3
    _, r5 = call(base + "/api/ingest?source=app", "POST", b"2026-11-23 03:00:09 ERROR x\n", ok)
    assert r5["accepted"] == 1 and d.store.counts()[0] == 4
