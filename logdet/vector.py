"""Vector stores: Pinecone (Sprites connector or direct API key) and a local JSON fallback."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .config import Config
from .net import PINECONE_API, PINECONE_VERSION, HTTPError, pinecone_gateway, request_json



def time_overlap_filter(start: float | None, end: float | None) -> dict | None:
    if start is None or end is None:
        return None
    return {"$and": [{"ts_start": {"$lt": end}}, {"ts_end": {"$gt": start}}]}


class LocalVectorStore:
    """Brute-force cosine search over a JSON file. Good for tests and offline demos."""
    name = "local"

    def __init__(self, path: Path):
        self.path = path
        self.items: dict[str, dict] = {}
        if path.exists():
            self.items = json.loads(path.read_text())

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.items))

    def reset(self) -> None:
        self.items = {}
        self._save()

    def upsert(self, vectors: list[dict]) -> None:
        for v in vectors:
            self.items[v["id"]] = {"values": v["values"], "metadata": v["metadata"]}
        self._save()

    def query(self, vector: list[float], top_k: int, start: float | None = None,
              end: float | None = None) -> list[dict]:
        nq = math.sqrt(sum(x * x for x in vector)) or 1.0
        scored = []
        for vid, it in self.items.items():
            md = it["metadata"]
            if start is not None and end is not None and not (md["ts_start"] < end and md["ts_end"] > start):
                continue
            vals = it["values"]
            if len(vals) != len(vector):
                continue
            nv = math.sqrt(sum(x * x for x in vals)) or 1.0
            scored.append((sum(a * b for a, b in zip(vector, vals)) / (nq * nv), vid, md))
        scored.sort(key=lambda s: -s[0])
        return [{"id": vid, "score": s, "metadata": md} for s, vid, md in scored[:top_k]]


class PineconeStore:
    """Pinecone data plane over REST.

    On a Sprite the calls go through the Sprites API gateway
    (`https://api.sprites.dev/v1/gateway/<provider>/<conn>/indexes/<index>/...`): the
    Sprite never holds the Pinecone key. Locally, PINECONE_API_KEY is used directly.
    """
    name = "pinecone"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ns = cfg.pinecone_namespace
        gw = pinecone_gateway(cfg)
        self.via = "gateway" if gw else "direct"
        if gw:
            self.base = f"{gw}/indexes/{cfg.pinecone_index}"
            self.control = gw
            self.headers = {"X-Pinecone-API-Version": PINECONE_VERSION}
        elif cfg.pinecone_api_key:
            self.headers = {"Api-Key": cfg.pinecone_api_key, "X-Pinecone-API-Version": PINECONE_VERSION}
            self.control = PINECONE_API
            self.base = ""  # resolved lazily from describe_index
        else:
            raise RuntimeError("Pinecone needs the Sprites Pinecone connector or PINECONE_API_KEY")

    def _host(self) -> str:
        if not self.base:
            info = request_json("GET", f"{self.control}/indexes/{self.cfg.pinecone_index}", headers=self.headers)
            self.base = "https://" + info["host"]
        return self.base

    def ensure_index(self, dim: int) -> str:
        """Create a serverless index if it does not exist (direct mode; best effort via gateway)."""
        try:
            info = request_json("GET", f"{self.control}/indexes/{self.cfg.pinecone_index}", headers=self.headers)
            have = info.get("dimension") if isinstance(info, dict) else None
            if have and have != dim:
                raise RuntimeError(f"Pinecone index '{self.cfg.pinecone_index}' has dimension {have}, "
                                   f"but the embedder produces {dim}. Recreate it or change LOGDET_EMBED.")
            return "exists"
        except HTTPError as e:
            if e.status != 404:
                return f"unchecked ({e.status})"
        request_json("POST", f"{self.control}/indexes", {
            "name": self.cfg.pinecone_index, "dimension": dim, "metric": "cosine",
            "spec": {"serverless": {"cloud": "aws", "region": "us-east-1"}},
        }, self.headers)
        return "created"

    def count(self) -> int:
        res = request_json("POST", self._host() + "/describe_index_stats", {}, self.headers)
        return int(((res.get("namespaces") or {}).get(self.ns) or {}).get("vectorCount", 0))

    def wait_until(self, expected: int, timeout: float = 30.0) -> int:
        """Pinecone serverless is eventually consistent: wait until the namespace shows the upserts."""
        import time
        deadline, n = time.time() + timeout, 0
        while time.time() < deadline:
            n = self.count()
            if n >= expected:
                break
            time.sleep(1.5)
        return n

    def reset(self) -> None:
        try:
            request_json("POST", self._host() + "/vectors/delete",
                         {"deleteAll": True, "namespace": self.ns}, self.headers)
        except HTTPError as e:
            if e.status != 404:  # namespace not found is fine
                raise

    def upsert(self, vectors: list[dict]) -> None:
        for i in range(0, len(vectors), 100):
            request_json("POST", self._host() + "/vectors/upsert",
                         {"vectors": vectors[i:i + 100], "namespace": self.ns}, self.headers)

    def query(self, vector: list[float], top_k: int, start: float | None = None,
              end: float | None = None) -> list[dict]:
        body = {"vector": vector, "topK": top_k, "includeMetadata": True, "namespace": self.ns}
        flt = time_overlap_filter(start, end)
        if flt:
            body["filter"] = flt
        res = request_json("POST", self._host() + "/query", body, self.headers)
        return [{"id": m["id"], "score": m.get("score", 0.0), "metadata": m.get("metadata", {})}
                for m in res.get("matches", [])]


def get_vector_store(cfg: Config):
    mode = cfg.vector
    if mode == "auto":
        mode = "pinecone" if (pinecone_gateway(cfg) or cfg.pinecone_api_key) else "local"
    if mode == "pinecone":
        return PineconeStore(cfg)
    cfg.ensure_home()
    return LocalVectorStore(cfg.home / "vectors.json")
