"""LLM backends: local Ollama, Ollama Cloud via the Sprites connector (or OLLAMA_API_KEY)."""
from __future__ import annotations

from .config import Config
from .net import HTTPError, ollama_gateway, request_json

OLLAMA_CLOUD = "https://ollama.com"
DEFAULT_LOCAL_MODEL = "qwen2.5:3b"
# Ollama Cloud direct/gateway API uses bare tags (no "-cloud" suffix); gemma4:31b is the docs' example.
DEFAULT_CLOUD_MODEL = "gemma4:31b"
# prefixes tried in order when the configured model is not on the server (names from /api/tags)
PREFERRED_PREFIXES = ["gemma4:31b", "gemma4", "glm-5.3-flash", "deepseek-v4.1-flash", "minimax-m3.1-flash",
                      "mistral-large", "glm-5", "kimi-k3", "deepseek", "qwen", "llama"]


def pick_model(available: list[str]) -> str | None:
    for pre in PREFERRED_PREFIXES:
        hit = next((m for m in available if m and m.startswith(pre)), None)
        if hit:
            return hit
    return available[0] if available else None


class OllamaChat:
    def __init__(self, base: str, model: str, headers: dict | None = None, label: str = "ollama"):
        self.base, self.model, self.headers, self.label = base.rstrip("/"), model, headers or {}, label

    @property
    def name(self) -> str:
        return f"{self.label}:{self.model}"

    def models(self) -> list[str]:
        res = request_json("GET", self.base + "/api/tags", headers=self.headers, timeout=15)
        return [m.get("name") or m.get("model") for m in (res or {}).get("models", [])]

    def chat(self, system: str, user: str) -> str:
        try:
            return self._chat(system, user)
        except HTTPError as e:
            if e.status not in (400, 404) or "model" not in e.body.lower():
                raise
            avail = self.models()
            pick = pick_model(avail)
            if not pick or pick == self.model:
                raise
            self.model = pick
            return self._chat(system, user)

    def _chat(self, system: str, user: str) -> str:
        res = request_json("POST", self.base + "/api/chat", {
            "model": self.model, "stream": False,
            "options": {"temperature": 0.2},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }, self.headers, timeout=300)
        return (res.get("message") or {}).get("content", "").strip()


def _local_ollama_up(cfg: Config) -> bool:
    try:
        request_json("GET", cfg.ollama_host.rstrip("/") + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


def get_llm(cfg: Config) -> OllamaChat | None:
    mode = cfg.llm
    if mode == "none":
        return None
    if mode in ("auto", "ollama-cloud"):
        gw = ollama_gateway(cfg)
        if gw:
            return OllamaChat(gw, cfg.model or DEFAULT_CLOUD_MODEL, label="ollama-cloud(sprites-connector)")
        if cfg.ollama_api_key:
            return OllamaChat(OLLAMA_CLOUD, cfg.model or DEFAULT_CLOUD_MODEL,
                              {"Authorization": f"Bearer {cfg.ollama_api_key}"}, label="ollama-cloud")
        if mode == "ollama-cloud":
            raise RuntimeError("Ollama Cloud needs the Sprites Ollama connector or OLLAMA_API_KEY")
    if mode == "ollama" or (mode == "auto" and _local_ollama_up(cfg)):
        return OllamaChat(cfg.ollama_host, cfg.model or DEFAULT_LOCAL_MODEL, label="ollama-local")
    return None
