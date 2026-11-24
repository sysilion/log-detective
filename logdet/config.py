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


DEFAULT_AUTH_FILE = "~/proj/auth.json"


def auth_file_path() -> str:
    return os.path.expanduser(_env("LOGDET_AUTH_FILE") or DEFAULT_AUTH_FILE)


def auth_key(provider: str) -> str:
    """Read one provider's key from the shared auth file (read-only, never logged).

    Format: {"<provider>": {"type": "api", "key": "..."}, ...}. Only used for local direct-API
    mode; on a Sprite the connectors inject credentials through the gateway.
    """
    import json
    path = auth_file_path()
    if not os.path.isfile(path):
        return ""
    try:
        with open(path) as fh:
            entry = json.load(fh).get(provider) or {}
        return str(entry.get("key") or "").strip() if isinstance(entry, dict) else ""
    except (OSError, ValueError):
        return ""


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

    # continuous ingestion
    flush_seconds: float = field(default_factory=lambda: float(_env("LOGDET_FLUSH_SECONDS", "30")))
    # HTTP push ingest (POST /api/ingest) is disabled unless a token is set; never set it on the public demo
    ingest_token: str = field(default_factory=lambda: _env("LOGDET_INGEST_TOKEN"))
    ingest_max_bytes: int = field(default_factory=lambda: int(_env("LOGDET_INGEST_MAX_BYTES", "1000000")))
    ingest_per_minute: int = field(default_factory=lambda: int(_env("LOGDET_INGEST_PER_MINUTE", "120")))
    # token for external schedulers to trigger one watch / pull cycle (POST /api/run/...); disabled when unset
    trigger_token: str = field(default_factory=lambda: _env("LOGDET_TRIGGER_TOKEN"))

    # data retention: events, chunks and vectors whose window ended before now - N days are pruned (0 = forever)
    retention_days: float = field(default_factory=lambda: float(_env("LOGDET_RETENTION_DAYS", "30")))

    # vector store: auto | pinecone | local
    vector: str = field(default_factory=lambda: _env("LOGDET_VECTOR", "auto"))
    pinecone_index: str = field(default_factory=lambda: _env("LOGDET_PINECONE_INDEX", "log-detective"))
    pinecone_namespace: str = field(default_factory=lambda: _env("LOGDET_PINECONE_NAMESPACE", "logs"))
    pinecone_gateway: str = field(default_factory=lambda: _env("LOGDET_PINECONE_GATEWAY"))
    pinecone_api_key: str = field(default_factory=lambda: _env("PINECONE_API_KEY") or auth_key("pinecone"))

    # embeddings: hash | pinecone | ollama
    embed: str = field(default_factory=lambda: _env("LOGDET_EMBED", "hash"))
    embed_dim: int = field(default_factory=lambda: int(_env("LOGDET_EMBED_DIM", "384")))
    pinecone_embed_model: str = field(
        default_factory=lambda: _env("LOGDET_PINECONE_EMBED_MODEL", "multilingual-e5-large"))
    ollama_embed_model: str = field(default_factory=lambda: _env("LOGDET_OLLAMA_EMBED_MODEL", "nomic-embed-text"))

    # LLM: auto | gemini | ollama (Ollama Cloud, then local server) | ollama-local | none
    gemini_gateway: str = field(default_factory=lambda: _env("LOGDET_GEMINI_GATEWAY"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY") or auth_key("google"))
    gemini_model: str = field(default_factory=lambda: _env("LOGDET_GEMINI_MODEL"))
    llm: str = field(default_factory=lambda: _env("LOGDET_LLM", "auto"))
    model: str = field(default_factory=lambda: _env("LOGDET_MODEL"))
    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    ollama_gateway: str = field(default_factory=lambda: _env("LOGDET_OLLAMA_GATEWAY"))
    ollama_api_key: str = field(default_factory=lambda: _env("OLLAMA_API_KEY") or auth_key("ollama"))

    # pinned "current time" for demos (ISO 8601), so "yesterday 3am" keeps pointing at the sample incident
    now_override: str = field(default_factory=lambda: _env("LOGDET_NOW"))

    # gateway discovery (only meaningful inside a Sprite)
    discover: bool = field(default_factory=lambda: _env("LOGDET_DISCOVER", "auto") != "0")

    @property
    def db_path(self) -> Path:
        return self.home / "logdet.sqlite3"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


def parse_now(value: str | None, tz: ZoneInfo):
    """Parse an ISO 8601 'current time' override; naive values are taken in `tz`. Empty -> None."""
    from datetime import datetime
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def retention_cutoff(now_ts: float, days: float) -> float | None:
    """Epoch cutoff for retention: data whose window ended before it is expired. None = keep forever."""
    if days < 0:
        raise ValueError("retention days must be >= 0")
    return None if days == 0 else now_ts - days * 86400


def in_sprite() -> bool:
    return os.path.isdir("/.sprite") or bool(_env("SPRITE_NAME"))
