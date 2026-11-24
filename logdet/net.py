"""Tiny JSON-over-HTTP helper (stdlib only) and Sprites gateway discovery."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import SPRITES_GATEWAY_LIST, Config, in_sprite


class HTTPError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status
        self.body = body


def request_json(method: str, url: str, payload: Any = None, headers: dict | None = None,
                 timeout: float = 60.0) -> Any:
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "log-detective/0.1"}
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "null"
    except urllib.error.HTTPError as e:
        raise HTTPError(e.code, url, e.read().decode(errors="replace")) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_gateway_list(payload: Any) -> dict[str, str]:
    """Map provider -> gateway base URL from a /v1/gateway/list response.

    The response format is not formally documented, so be tolerant: look for any
    object carrying a provider-ish name and a gateway URL (`gateway_base_url`, or
    derived from `usage_snippet`).
    """
    found: dict[str, str] = {}
    for obj in _walk(payload):
        name = str(obj.get("provider") or obj.get("slug") or obj.get("name") or obj.get("id") or "").lower()
        base = obj.get("gateway_base_url") or obj.get("base_url") or obj.get("url") or ""
        if not base and isinstance(obj.get("usage_snippet"), str):
            for tok in obj["usage_snippet"].split():
                if "/v1/gateway/" in tok:
                    tok = tok.strip("'\"")
                    parts = tok.split("/v1/gateway/", 1)
                    segs = parts[1].split("/")
                    if len(segs) >= 2:
                        base = parts[0] + "/v1/gateway/" + "/".join(segs[:2])
        if not (name and isinstance(base, str) and "/v1/gateway/" in base):
            continue
        base = base.rstrip("/")
        key_src = name + " " + base.lower()
        if "pinecone" in key_src:
            found.setdefault("pinecone", base)
        elif "ollama" in key_src:
            found.setdefault("ollama", base)
        elif "gemini" in key_src or "google" in key_src:
            found.setdefault("gemini", base)
        else:
            found.setdefault(name, base)
    return found


_discovered: dict[str, str] | None = None


def discover_gateways(cfg: Config, force: bool = False) -> dict[str, str]:
    """Ask the Sprites API gateway which connectors this Sprite may use."""
    global _discovered
    if _discovered is not None and not force:
        return _discovered
    _discovered = {}
    if not cfg.discover or not in_sprite():
        return _discovered
    try:
        _discovered = parse_gateway_list(request_json("GET", SPRITES_GATEWAY_LIST, timeout=8))
    except Exception:  # discovery is best effort
        _discovered = {}
    return _discovered


def pinecone_gateway(cfg: Config) -> str:
    return cfg.pinecone_gateway.rstrip("/") or discover_gateways(cfg).get("pinecone", "")


def ollama_gateway(cfg: Config) -> str:
    return cfg.ollama_gateway.rstrip("/") or discover_gateways(cfg).get("ollama", "")


def gemini_gateway(cfg: Config) -> str:
    return cfg.gemini_gateway.rstrip("/") or discover_gateways(cfg).get("gemini", "")
