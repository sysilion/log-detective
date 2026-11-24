"""Embedders: local feature hashing (default, no network), Pinecone Inference, or Ollama."""
from __future__ import annotations

import hashlib
import math
import re

from .config import Config
from .net import PINECONE_API, PINECONE_VERSION, pinecone_gateway, request_json


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{1,40}|[가-힣]{1,10}|\d{3}")


class HashEmbedder:
    """Deterministic bag-of-tokens feature hashing (unigrams + bigrams), L2 normalized.

    Log text is mostly lexical (error names, routes, status codes), so this works
    surprisingly well for retrieval and keeps the demo fully offline.
    """
    name = "hash"

    def __init__(self, dim: int = 384):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        toks = [t.lower() for t in _TOKEN_RE.findall(text)]
        feats = toks + [a + " " + b for a, b in zip(toks, toks[1:])]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        return [self._vec(t) for t in texts]


class PineconeEmbedder:
    """Pinecone Inference `/embed` (through the Sprites gateway when available)."""
    name = "pinecone"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        gw = pinecone_gateway(cfg)
        if gw:
            self.url, self.headers = gw + "/embed", {"X-Pinecone-API-Version": PINECONE_VERSION}
        elif cfg.pinecone_api_key:
            self.url = PINECONE_API + "/embed"
            self.headers = {"Api-Key": cfg.pinecone_api_key, "X-Pinecone-API-Version": PINECONE_VERSION}
        else:
            raise RuntimeError("Pinecone embedder needs the Pinecone connector (on a Sprite) or PINECONE_API_KEY")

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 90):
            res = request_json("POST", self.url, {
                "model": self.cfg.pinecone_embed_model,
                "parameters": {"input_type": kind, "truncate": "END"},
                "inputs": [{"text": t[:4000]} for t in texts[i:i + 90]],
            }, self.headers)
            out.extend(d["values"] for d in res["data"])
        return out


class OllamaEmbedder:
    name = "ollama"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            res = request_json("POST", self.cfg.ollama_host.rstrip("/") + "/api/embed",
                               {"model": self.cfg.ollama_embed_model, "input": texts[i:i + 32]}, timeout=300)
            out.extend(res["embeddings"])
        return out


def get_embedder(cfg: Config):
    if cfg.embed == "pinecone":
        return PineconeEmbedder(cfg)
    if cfg.embed == "ollama":
        return OllamaEmbedder(cfg)
    return HashEmbedder(cfg.embed_dim)
