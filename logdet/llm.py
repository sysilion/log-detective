"""LLM backends.

- Gemini API (default, free tier): Sprites "Google Gemini" connector via the gateway, or GEMINI_API_KEY.
- Ollama Cloud: Sprites "Ollama Cloud" connector via the gateway, or OLLAMA_API_KEY; else a local `ollama serve`.
- None -> the agent falls back to a deterministic rule-based report.
"""
from __future__ import annotations

from .config import Config
from .net import HTTPError, gemini_gateway, ollama_gateway, request_json

GEMINI_API = "https://generativelanguage.googleapis.com"
# Stable free-tier Flash-Lite model; minimal thinking by default -> low latency. Override with LOGDET_GEMINI_MODEL.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_PREFERRED = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite",
                    "gemini-2.5-flash-lite"]

OLLAMA_CLOUD = "https://ollama.com"
DEFAULT_LOCAL_MODEL = "qwen2.5:3b"
# Ollama Cloud direct/gateway API uses bare tags (no "-cloud" suffix); gemma4:31b is the docs' example.
DEFAULT_CLOUD_MODEL = "gemma4:31b"
# prefixes tried in order when the configured model is not on the server (names from /api/tags)
PREFERRED_PREFIXES = ["gemma4:31b", "gemma4", "glm-5.3-flash", "deepseek-v4.1-flash", "minimax-m3.1-flash",
                      "mistral-large", "glm-5", "kimi-k3", "deepseek", "qwen", "llama"]


def pick_model(available: list[str], preferred: list[str] | None = None) -> str | None:
    for pre in preferred or PREFERRED_PREFIXES:
        hit = next((m for m in available if m and m.startswith(pre)), None)
        if hit:
            return hit
    return available[0] if available else None


def _model_missing(e: HTTPError) -> bool:
    return e.status in (400, 404) and "model" in e.body.lower()


class OllamaChat:
    def __init__(self, base: str, model: str, headers: dict | None = None, label: str = "ollama"):
        self.base, self.model, self.headers, self.label = base.rstrip("/"), model, headers or {}, label

    @property
    def name(self) -> str:
        return f"{self.label}:{self.model}"

    def models(self) -> list[str]:
        res = request_json("GET", self.base + "/api/tags", headers=self.headers, timeout=15)
        return [m.get("name") or m.get("model") for m in (res or {}).get("models", [])]

    def stream(self, system: str, user: str):
        """Yield the answer in chunks. Default: one chunk from chat()."""
        yield self.chat(system, user)

    def chat(self, system: str, user: str) -> str:
        try:
            return self._chat(system, user)
        except HTTPError as e:
            if not _model_missing(e):
                raise
            pick = pick_model(self.models())
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


class GeminiChat:
    """Gemini API generateContent (REST). Through the Sprites gateway no key header is sent."""

    def __init__(self, base: str, model: str, headers: dict | None = None, label: str = "gemini"):
        self.base, self.model, self.headers, self.label = base.rstrip("/"), model, headers or {}, label

    @property
    def name(self) -> str:
        return f"{self.label}:{self.model}"

    def models(self) -> list[str]:
        res = request_json("GET", self.base + "/v1beta/models?pageSize=200", headers=self.headers, timeout=15)
        return [m["name"].removeprefix("models/") for m in (res or {}).get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", ["generateContent"])]

    def stream(self, system: str, user: str):
        """Yield the answer in chunks. Default: one chunk from chat()."""
        yield self.chat(system, user)

    def chat(self, system: str, user: str) -> str:
        try:
            return self._chat(system, user)
        except HTTPError as e:
            if e.status in (429, 500, 503):  # overloaded / rate limited: retry once, on the default model
                import time
                time.sleep(1.5)
                self.model = DEFAULT_GEMINI_MODEL
                return self._chat(system, user)
            if not _model_missing(e):
                raise
            pick = pick_model(self.models(), GEMINI_PREFERRED)
            if not pick or pick == self.model:
                raise
            self.model = pick
            return self._chat(system, user)

    def _chat(self, system: str, user: str) -> str:
        res = request_json("POST", f"{self.base}/v1beta/models/{self.model}:generateContent", {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
        }, self.headers, timeout=120)
        cands = (res or {}).get("candidates") or []
        if not cands:
            raise RuntimeError(f"Gemini returned no candidates: {str(res.get('promptFeedback', res))[:200]}")
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def _local_ollama_up(cfg: Config) -> bool:
    try:
        request_json("GET", cfg.ollama_host.rstrip("/") + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


def gemini_backend(cfg: Config) -> GeminiChat | None:
    model = cfg.gemini_model or DEFAULT_GEMINI_MODEL
    gw = gemini_gateway(cfg)
    if gw:
        return GeminiChat(gw, model, label="gemini(sprites-connector)")
    if cfg.gemini_api_key:
        return GeminiChat(GEMINI_API, model, {"x-goog-api-key": cfg.gemini_api_key}, label="gemini")
    return None


def ollama_cloud_backend(cfg: Config) -> OllamaChat | None:
    model = cfg.model or DEFAULT_CLOUD_MODEL
    gw = ollama_gateway(cfg)
    if gw:
        return OllamaChat(gw, model, label="ollama-cloud(sprites-connector)")
    if cfg.ollama_api_key:
        return OllamaChat(OLLAMA_CLOUD, model, {"Authorization": f"Bearer {cfg.ollama_api_key}"}, label="ollama-cloud")
    return None


def ollama_local_backend(cfg: Config) -> OllamaChat | None:
    if _local_ollama_up(cfg):
        return OllamaChat(cfg.ollama_host, cfg.model or DEFAULT_LOCAL_MODEL, label="ollama-local")
    return None


def get_llm(cfg: Config):
    mode = cfg.llm
    if mode == "none":
        return None
    if mode == "gemini":
        llm = gemini_backend(cfg)
        if not llm:
            raise RuntimeError("Gemini needs the Sprites Gemini connector or GEMINI_API_KEY (or 'google' in the auth file)")
        return llm
    if mode in ("ollama", "ollama-cloud"):
        llm = ollama_cloud_backend(cfg) or (ollama_local_backend(cfg) if mode == "ollama" else None)
        if not llm:
            raise RuntimeError("Ollama needs the Sprites Ollama Cloud connector, OLLAMA_API_KEY, or a local ollama server")
        return llm
    if mode == "ollama-local":
        return ollama_local_backend(cfg)
    # auto: Gemini (free) -> Ollama Cloud -> local Ollama -> rule-based report
    return gemini_backend(cfg) or ollama_cloud_backend(cfg) or ollama_local_backend(cfg)
