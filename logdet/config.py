"""Runtime configuration, read from environment variables.

Every setting has a safe local default so `logdet` works offline (hash
embeddings + local vector store + heuristic report). On a Sprite, the Pinecone
and Ollama Cloud connectors are auto-discovered through the Sprites API gateway.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

SPRITES_GATEWAY_LIST = "https://api.sprites.dev/v1/gateway/list"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def local_tz_name() -> str:
    tz = _env("LOGDET_TZ") or _env("TZ")
    if tz:
        return tz
    try:  # macOS / most Linux distros
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(_env("LOGDET_HOME", ".logdet")))
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(local_tz_name()))

    # chunking
    window_seconds: int = field(default_factory=lambda: int(_env("LOGDET_WINDOW", "120")))

    # vector store: auto | pinecone | local
    vector: str = field(default_factory=lambda: _env("LOGDET_VECTOR", "auto"))
    pinecone_index: str = field(default_factory=lambda: _env("LOGDET_PINECONE_INDEX", "log-detective"))
    pinecone_namespace: str = field(default_factory=lambda: _env("LOGDET_PINECONE_NAMESPACE", "logs"))
    pinecone_gateway: str = field(default_factory=lambda: _env("LOGDET_PINECONE_GATEWAY"))
    pinecone_api_key: str = field(default_factory=lambda: _env("PINECONE_API_KEY"))

    # embeddings: hash | pinecone | ollama
    embed: str = field(default_factory=lambda: _env("LOGDET_EMBED", "hash"))
    embed_dim: int = field(default_factory=lambda: int(_env("LOGDET_EMBED_DIM", "384")))
    pinecone_embed_model: str = field(
        default_factory=lambda: _env("LOGDET_PINECONE_EMBED_MODEL", "multilingual-e5-large"))
    ollama_embed_model: str = field(default_factory=lambda: _env("LOGDET_OLLAMA_EMBED_MODEL", "nomic-embed-text"))

    # LLM: auto | ollama | ollama-cloud | none
    llm: str = field(default_factory=lambda: _env("LOGDET_LLM", "auto"))
    model: str = field(default_factory=lambda: _env("LOGDET_MODEL"))
    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    ollama_gateway: str = field(default_factory=lambda: _env("LOGDET_OLLAMA_GATEWAY"))
    ollama_api_key: str = field(default_factory=lambda: _env("OLLAMA_API_KEY"))

    # gateway discovery (only meaningful inside a Sprite)
    discover: bool = field(default_factory=lambda: _env("LOGDET_DISCOVER", "auto") != "0")

    @property
    def db_path(self) -> Path:
        return self.home / "logdet.sqlite3"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


def in_sprite() -> bool:
    return os.path.isdir("/.sprite") or bool(_env("SPRITE_NAME"))
