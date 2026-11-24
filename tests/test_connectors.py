"""Wire-level tests for the Sprites-gateway Pinecone + Ollama Cloud code paths, against a fake gateway."""
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from logdet.agent import Detective
from logdet.config import Config
from logdet.net import parse_gateway_list
from logdet.sample import generate

KST = ZoneInfo("Asia/Seoul")


class FakeGateway:
    def __init__(self):
        self.calls, self.vectors = [], {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.calls.append(("GET", self.path, None, dict(self.headers)))
                if self.path.endswith("/indexes/log-detective"):
                    return self._reply({"name": "log-detective", "dimension": 384, "host": "unused"})
                self._reply({"error": "nope"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(("POST", self.path, body, dict(self.headers)))
                p = self.path
                if p.endswith("/vectors/upsert"):
                    for v in body["vectors"]:
                        fake.vectors[v["id"]] = v
                    return self._reply({"upsertedCount": len(body["vectors"])})
                if p.endswith("/vectors/delete"):
                    fake.vectors.clear()
                    return self._reply({})
                if p.endswith("/query"):
                    f = body["filter"]["$and"]
                    lt, gt = f[0]["ts_start"]["$lt"], f[1]["ts_end"]["$gt"]
                    ms = [v for v in fake.vectors.values() if v["metadata"]["ts_start"] < lt
                          and v["metadata"]["ts_end"] > gt]
                    ms.sort(key=lambda v: -sum(a * b for a, b in zip(v["values"], body["vector"])))
                    return self._reply({"matches": [{"id": v["id"], "score": 0.5, "metadata": v["metadata"]}
                                                    for v in ms[:body["topK"]]]})
                if p.endswith("/api/chat"):
                    return self._reply({"message": {"role": "assistant", "content": "LLM says: pool exhausted [E1]"}})
                self._reply({"error": "unknown"}, 404)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1/gateway"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def gw():
    g = FakeGateway()
    yield g
    g.srv.shutdown()


def test_ingest_and_ask_through_gateway(gw, tmp_path):
    generate(tmp_path / "logs", datetime(2026, 11, 23), KST, hours=4)
    cfg = Config()
    cfg.home, cfg.tz = tmp_path / "home", KST
    cfg.vector, cfg.embed, cfg.llm = "auto", "hash", "auto"
    cfg.pinecone_gateway = gw.base + "/pinecone/conn_test"
    cfg.ollama_gateway = gw.base + "/ollama/conn_test2"
    d = Detective(cfg)
    assert d.vs.name == "pinecone" and d.vs.via == "gateway"
    d.ingest([str(tmp_path / "logs")], reset=True, log=lambda *_: None)
    assert len(gw.vectors) > 100
    up = next(c for c in gw.calls if c[1].endswith("/vectors/upsert"))
    assert up[1] == "/v1/gateway/pinecone/conn_test/indexes/log-detective/vectors/upsert"
    assert "Authorization" not in up[3] and "Api-Key" not in up[3]  # the gateway injects credentials
    assert up[2]["namespace"] == "logs" and len(up[2]["vectors"]) <= 100

    res = d.ask("어제 새벽 3시에 왜 느려졌어?", now=datetime(2026, 11, 24, 9, 0, tzinfo=KST))
    assert res["answer"] == "LLM says: pool exhausted [E1]"
    assert res["llm"].startswith("ollama-cloud(sprites-connector)")
    chat = next(c for c in gw.calls if c[1].endswith("/api/chat"))
    assert chat[1] == "/v1/gateway/ollama/conn_test2/api/chat"
    prompt = chat[2]["messages"][1]["content"]
    assert "ANOMALY:" in prompt and "[E1]" in prompt and "nightly_sales_report" in prompt
    assert res["retrieved"]


def test_parse_gateway_list_variants():
    payload = {"providers": [
        {"provider": "pinecone", "gateway_base_url": "https://api.sprites.dev/v1/gateway/pinecone/conn_a/"},
        {"provider": "ollama-cloud", "usage_snippet":
            "curl https://api.sprites.dev/v1/gateway/ollama-cloud/conn_b/api/tags"},
        {"provider": "box", "setup_url": "https://fly.io/..."},
    ]}
    got = parse_gateway_list(payload)
    assert got["pinecone"] == "https://api.sprites.dev/v1/gateway/pinecone/conn_a"
    assert got["ollama"] == "https://api.sprites.dev/v1/gateway/ollama-cloud/conn_b"
    assert "box" not in got
