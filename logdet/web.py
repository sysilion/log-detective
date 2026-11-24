"""Minimal web UI + JSON API (stdlib http.server), served from the Sprite URL."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import Detective
from .config import Config

STATIC = Path(__file__).parent / "static"


def serve(cfg: Config, host: str = "0.0.0.0", port: int = 8080) -> None:
    det = Detective(cfg)
    lock = threading.Lock()

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
                self._json(200, {"ok": True, "events": ev, "chunks": ch, "tz": cfg.tz.key,
                                 "range": [datetime.fromtimestamp(t, cfg.tz).isoformat() for t in rng] if rng else None})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/ask":
                return self._send(404, b"not found", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                q = str(req.get("q", "")).strip()[:500]
                if not q:
                    return self._json(400, {"error": "empty question"})
                now = None
                if req.get("now"):
                    now = datetime.fromisoformat(req["now"])
                    now = now if now.tzinfo else now.replace(tzinfo=cfg.tz)
                with lock:
                    res = det.ask(q, now=now, use_llm=not req.get("no_llm"))
                self._json(200, res)
            except Exception as e:
                self._json(500, {"error": str(e)[:500]})

        def log_message(self, fmt, *args):
            print(f"[web] {self.address_string()} {fmt % args}")

    print(f"log-detective web UI on http://{host}:{port}")
    ThreadingHTTPServer((host, port), H).serve_forever()
