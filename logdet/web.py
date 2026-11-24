"""Minimal web UI + JSON API (stdlib http.server), served from the Sprite URL."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import Detective
from .config import Config

STATIC = Path(__file__).parent / "static"


class AskGuard:
    """Protect free-tier quotas on a public URL: per-IP rate limit, global daily budget, answer cache."""

    def __init__(self, per_ip_per_hour: int, daily_budget: int, cache_seconds: int = 900):
        self.per_ip, self.budget, self.cache_s = per_ip_per_hour, daily_budget, cache_seconds
        self.hits: dict[str, deque] = {}
        self.day, self.used = time.strftime("%Y-%m-%d"), 0
        self.cache: dict[str, tuple[float, dict]] = {}
        self.lock = threading.Lock()

    def cached(self, key: str) -> dict | None:
        with self.lock:
            hit = self.cache.get(key)
            return hit[1] if hit and time.time() - hit[0] < self.cache_s else None

    def store(self, key: str, res: dict) -> None:
        with self.lock:
            if len(self.cache) > 500:
                self.cache.clear()
            self.cache[key] = (time.time(), res)

    def admit(self, ip: str) -> str | None:
        """Return None if allowed, else a reason."""
        now = time.time()
        with self.lock:
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.used = today, 0
            q = self.hits.setdefault(ip, deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_ip:
                return f"rate limit: {self.per_ip} questions per hour per visitor"
            if self.used >= self.budget:
                return "daily demo budget used up, please try again tomorrow"
            q.append(now)
            self.used += 1
            return None


def serve(cfg: Config, host: str = "0.0.0.0", port: int = 8080) -> None:
    det = Detective(cfg)
    lock = threading.Lock()
    guard = AskGuard(int(os.environ.get("LOGDET_ASK_PER_IP_PER_HOUR", "20")),
                     int(os.environ.get("LOGDET_ASK_DAILY_BUDGET", "300")))

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/health":
                ev, ch = det.store.counts()
                rng = det.store.time_range()
                self._json(200, {"ok": True, "events": ev, "chunks": ch, "tz": cfg.tz.key, "now": cfg.now_override or None,
                                 "range": [datetime.fromtimestamp(t, cfg.tz).isoformat() for t in rng] if rng else None})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/ask":
                return self._send(404, b"not found", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                q = " ".join(str(req.get("q", "")).split())[:300]
                if not q:
                    return self._json(400, {"error": "empty question"})
                key = q.lower()
                res = guard.cached(key)
                if res is None:
                    ip = (self.headers.get("Fly-Client-IP") or (self.headers.get("X-Forwarded-For") or "")
                          .split(",")[0].strip() or self.client_address[0])
                    reason = guard.admit(ip)
                    if reason:
                        return self._json(429, {"error": reason})
                    with lock:
                        res = det.ask(q)
                    guard.store(key, res)
                self._json(200, res)
            except Exception as e:
                print(f"[web] ask failed: {e}")
                self._json(500, {"error": "internal error"})

        def log_message(self, fmt, *args):
            print(f"[web] {self.address_string()} {fmt % args}")

    print(f"log-detective web UI on http://{host}:{port}")
    ThreadingHTTPServer((host, port), H).serve_forever()
