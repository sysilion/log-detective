"""pull-es against a mocked Elasticsearch: PIT + search_after, range fallback, auth, checkpoints, dedupe."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from logdet.agent import Detective
from logdet.config import Config
from logdet.es import ESConfig, pull_once

KST = ZoneInfo("Asia/Seoul")
T0 = 1_795_370_400_000  # 2026-11-23 03:00 KST in epoch ms


def make_docs(n, start=0):
    # three docs share each millisecond timestamp -> ties across page boundaries
    return [{"_id": f"d{i}", "_index": "logs-2026.11.23", "ts": T0 + (i // 3) * 1000,
             "_source": {"@timestamp": "x", "message": f"GET /api/cart {'503' if i % 10 == 0 else '200'} #{i}",
                         "http": {"response": {"status_code": 503 if i % 10 == 0 else 200}},
                         "event": {"duration": 3_000_000_000 if i % 10 == 0 else 90_000_000},
                         "service": {"name": "checkout"}, "log": {"level": "error" if i % 10 == 0 else "info"}}}
            for i in range(start, start + n)]


class FakeES:
    def __init__(self, pit=True):
        self.docs, self.calls, self.pit_ok, self.pits = make_docs(2500), [], pit, set()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_DELETE(self):
                body = self._body()
                fake.pits.discard(body.get("id"))
                self._reply({"succeeded": True})

            def do_POST(self):
                body = self._body()
                fake.calls.append((self.path, body, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "ApiKey k123":
                    return self._reply({"error": "unauthorized"}, 401)
                if "/_pit" in self.path:
                    if not fake.pit_ok:
                        return self._reply({"error": "no pit"}, 404)
                    fake.pits.add("pit-1")
                    return self._reply({"id": "pit-1"})
                flt = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"]
                rows = sorted(((d["ts"], i, d) for i, d in enumerate(fake.docs) if d["ts"] >= flt))
                if "pit" in body:
                    assert body["pit"]["id"] in fake.pits
                    if "search_after" in body:
                        sa = tuple(body["search_after"])
                        rows = [r for r in rows if (r[0], r[1]) > sa]
                    hits = [{**d, "sort": [ts, i]} for ts, i, d in rows[:body["size"]]]
                else:
                    hits = [{**d, "sort": [ts]} for ts, i, d in rows[:body["size"]]]
                self._reply({"hits": {"hits": [{k: v for k, v in h.items() if k != "ts"} for h in hits]},
                             **({"pit_id": "pit-1"} if "pit" in body else {})})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture()
def det(tmp_path):
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm, cfg.retention_days = \
        tmp_path / "h", KST, "local", "hash", "none", 0
    return Detective(cfg)


def es_cfg(url):
    return ESConfig(url=url, index="logs-*", ts_field="@timestamp", query="", fields_json="",
                    latency_unit="ns", batch=1000, start="2026-11-22T00:00:00+00:00",
                    api_key="k123", user="", password="")


@pytest.mark.parametrize("pit", [True, False])
def test_pull_all_once_then_only_new(det, pit):
    es = FakeES(pit=pit)
    try:
        r1 = pull_once(det, es_cfg(es.url), log=lambda *_: None)
        assert r1["mode"] == ("pit" if pit else "range")
        assert r1["accepted"] == 2500 and det.store.counts()[0] == 2500  # nothing lost at tie boundaries
        assert not es.pits  # PIT closed
        r2 = pull_once(det, es_cfg(es.url), log=lambda *_: None)
        assert r2["accepted"] == 0 and det.store.counts()[0] == 2500  # boundary refetch deduped
        es.docs += make_docs(50, start=2500)
        r3 = pull_once(det, es_cfg(es.url), log=lambda *_: None)
        assert r3["accepted"] == 50 and det.store.counts()[0] == 2550
    finally:
        es.srv.shutdown()
    row = det.store.db.execute("SELECT * FROM events WHERE status=503 LIMIT 1").fetchone()
    assert row["source"] == "checkout" and row["level"] == "ERROR" and abs(row["latency_ms"] - 3000) < 1e-6
    assert all(c[2] == "ApiKey k123" for c in es.calls)


def test_field_overrides_and_query(det):
    es = FakeES()
    cfg = es_cfg(es.url)
    cfg.fields_json, cfg.query = '{"service": "nonexistent.field"}', "service.name:checkout"
    try:
        pull_once(det, cfg, log=lambda *_: None)
    finally:
        es.srv.shutdown()
    body = next(c[1] for c in es.calls if "_search" in c[0])
    assert body["query"]["bool"]["filter"][1] == {"query_string": {"query": "service.name:checkout"}}
    assert det.store.db.execute("SELECT DISTINCT source FROM events").fetchone()[0] == "logs-2026.11.23"
