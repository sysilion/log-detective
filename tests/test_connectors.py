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
        self.fail_query = False      # Pinecone /query -> 503
        self.fail_chat = False       # Ollama /api/chat -> 500
        self.cloud_models = None     # e.g. ["glm-5.3-flash"]: /api/chat 404s for other models
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
                if self.path.endswith("/api/tags"):
                    return self._reply({"models": [{"name": m} for m in (fake.cloud_models or [])]})
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
                    if fake.fail_query:
                        return self._reply({"error": "upstream unavailable"}, 503)
                    f = body["filter"]["$and"]
                    lt, gt = f[0]["ts_start"]["$lt"], f[1]["ts_end"]["$gt"]
                    ms = [v for v in fake.vectors.values() if v["metadata"]["ts_start"] < lt
                          and v["metadata"]["ts_end"] > gt]
                    ms.sort(key=lambda v: -sum(a * b for a, b in zip(v["values"], body["vector"])))
                    return self._reply({"matches": [{"id": v["id"], "score": 0.5, "metadata": v["metadata"]}
                                                    for v in ms[:body["topK"]]]})
                if p.endswith("/api/chat"):
                    if fake.fail_chat:
                        return self._reply({"error": "internal"}, 500)
                    if fake.cloud_models is not None and body["model"] not in fake.cloud_models:
                        return self._reply({"error": f"model '{body['model']}' not found"}, 404)
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


NOW = datetime(2026, 11, 24, 9, 0, tzinfo=KST)
Q = "어제 새벽 3시에 왜 느려졌어?"


@pytest.fixture()
def gw_det(gw, tmp_path):
    gw.fail_query = gw.fail_chat = False
    gw.cloud_models = None
    generate(tmp_path / "logs", datetime(2026, 11, 23), KST, hours=4)
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm = tmp_path / "home", KST, "auto", "hash", "auto"
    cfg.pinecone_gateway = gw.base + "/pinecone/conn_test"
    cfg.ollama_gateway = gw.base + "/ollama/conn_test2"
    d = Detective(cfg)
    d.ingest([str(tmp_path / "logs")], reset=True, log=lambda *_: None)
    return d


def test_pinecone_down_falls_back_to_local_rerank(gw, gw_det):
    gw.fail_query = True
    res = gw_det.ask(Q, now=NOW)
    assert res["retrieval_error"] and "503" in res["retrieval_error"]
    assert res["retrieved"] and res["answer"].startswith("LLM says")
    assert any("nightly_sales_report" in e["line"] for e in res["evidence"])


def test_missing_model_is_auto_picked(gw, gw_det):
    gw.cloud_models = ["glm-5.3-flash", "kimi-k3"]
    res = gw_det.ask(Q, now=NOW)
    assert res["llm"].endswith(":glm-5.3-flash") and res["answer"].startswith("LLM says")
    tried = [c[2]["model"] for c in gw.calls if c[1].endswith("/api/chat")][-2:]
    assert tried == ["gemma4:31b", "glm-5.3-flash"]


def test_llm_down_falls_back_to_heuristic(gw, gw_det):
    gw.fail_chat = True
    res = gw_det.ask(Q, now=NOW)
    assert res["llm"].startswith("heuristic") and "500" in res["llm_error"]
    assert "nightly_sales_report" in res["answer"]


def test_no_connectors_means_local_everything(tmp_path):
    cfg = Config()
    cfg.home = tmp_path / "home"
    d = Detective(cfg)
    assert d.vs.name == "local" and d.emb.name == "hash"
    from logdet.llm import get_llm
    cfg.ollama_host = "http://127.0.0.1:9"  # nothing listening
    assert get_llm(cfg) is None


def test_pick_model_prefers_known_families():
    from logdet.llm import pick_model
    assert pick_model(["kimi-k3", "gemma4:12b", "gemma4:31b"]) == "gemma4:31b"
    assert pick_model(["zzz:1b", "deepseek-v4.1-flash"]) == "deepseek-v4.1-flash"
    assert pick_model(["zzz:1b"]) == "zzz:1b" and pick_model([]) is None
