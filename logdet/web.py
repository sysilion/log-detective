"""Minimal web UI + JSON API (stdlib http.server), served from the Sprite URL.

Public surface (nothing else is routed):
  GET  /                 the single-page UI
  GET  /api/health       counts, time range, demo clock
  POST /api/ask          {"q": "..."} -> full result JSON
  POST /api/ask/stream   {"q": "..."} -> NDJSON: {"type":"meta"} then {"type":"delta"}* then {"type":"done"}
There is no ingest/admin endpoint over HTTP. /api/ask* is guarded by a per-visitor limit, a global daily
budget and an answer cache, so a public URL can't drain the free-tier quotas.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .agent import Detective
from .config import Config
from .ingest import IncrementalIngestor, parse_payload
from .watch import Watcher, recent_findings

STATIC = Path(__file__).parent / "static"
MAX_BODY = 4096
MAX_QUESTION = 300


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
        """Cache only clean answers, so a transient upstream failure doesn't stick for 15 minutes."""
        if res.get("llm_fallback") or res.get("retrieval_fallback"):
            return
        with self.lock:
            if len(self.cache) > 500:
                self.cache.clear()
            self.cache[key] = (time.time(), res)

    def admit(self, ip: str) -> str | None:
        """Return None if allowed, else a human-readable reason."""
        now = time.time()
        with self.lock:
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.used = today, 0
            if len(self.hits) > 5000:  # drop visitors idle for an hour
                self.hits = {k: q for k, q in self.hits.items() if q and now - q[-1] < 3600}
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


def public_view(res: dict) -> dict:
    """Strip upstream error details (provider URLs/messages) from what anonymous visitors see."""
    out = dict(res)
    llm_err, ret_err = out.pop("llm_error", None), out.pop("retrieval_error", None)
    out["llm_fallback"] = bool(llm_err)
    out["retrieval_fallback"] = bool(ret_err)
    if llm_err or ret_err:
        print(f"[web] degraded answer: llm_error={llm_err!r} retrieval_error={ret_err!r}")
    return out


def public_finding(f: dict) -> dict:
    """Findings are shown publicly; drop nothing secret (they hold no URLs/keys) but keep them compact."""
    return {k: f.get(k) for k in ("id", "type", "watch", "severity", "incident", "created", "at", "window",
                                  "first_ts", "replay", "reasons", "summary", "llm", "evidence")}


class BadRequest(ValueError):
    pass


class MinuteLimiter:
    """Per-key sliding one-minute window (used for the authenticated ingest route)."""

    def __init__(self, per_minute: int):
        self.per_minute, self.hits, self.lock = per_minute, {}, threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def bearer_ok(header: str | None, token: str) -> bool:
    """Constant-time check of 'Authorization: Bearer <token>'. An empty configured token never matches."""
    if not token or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), token.encode())


def make_handler(det: Detective, cfg: Config, guard: AskGuard):
    db_lock = threading.Lock()  # SQLite connection is shared; the LLM call runs outside this lock
    ingest_limiter = MinuteLimiter(cfg.ingest_per_minute)
    ingestor = IncrementalIngestor(det)
    ingest_slots = threading.BoundedSemaphore(2)
    run_lock = threading.Lock()
    _watcher: list = []

    def watcher() -> Watcher:
        if not _watcher:
            _watcher.append(Watcher(det, log=lambda m: print(m, flush=True)))
        return _watcher[0]

    class Handler(BaseHTTPRequestHandler):
        timeout = 15  # seconds; slow or idle clients can't pin threads
        server_version = "log-detective"
        sys_version = ""

        # ------------------------------------------------------------ helpers
        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj, headers: dict | None = None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json", headers)

        def _client_ip(self) -> str:
            # Fly's proxy sets Fly-Client-IP; otherwise trust only the right-most X-Forwarded-For hop
            # (appended by the nearest proxy), never the client-controlled left-most entry.
            fly = (self.headers.get("Fly-Client-IP") or "").strip()
            if fly:
                return fly
            xff = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
            return xff[-1] if xff else self.client_address[0]

        def _question(self) -> str:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest("bad Content-Length") from None
            if n <= 0 or n > MAX_BODY:
                raise BadRequest("body required (max 4 KB)")
            try:
                req = json.loads(self.rfile.read(n))
            except (ValueError, UnicodeDecodeError):
                raise BadRequest("body must be JSON") from None
            if not isinstance(req, dict):
                raise BadRequest("body must be a JSON object")
            q = " ".join(str(req.get("q", "")).split())[:MAX_QUESTION]
            if not q:
                raise BadRequest("empty question")
            return q

        # ------------------------------------------------------------ routes
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/alerts":
                self._json(200, {"findings": [public_finding(f) for f in recent_findings(cfg.home, 30)],
                                 "watches": [{"name": x.name, "interval": x.interval, "lookback": x.lookback}
                                             for x in watcher().wcfg.watches]})
            elif self.path == "/api/health":
                with db_lock:
                    ev, ch = det.store.counts()
                    rng = det.store.time_range()
                self._json(200, {"ok": True, "events": ev, "chunks": ch, "tz": cfg.tz.key,
                                 "now": cfg.now_override or None,
                                 "range": [datetime.fromtimestamp(t, cfg.tz).isoformat() for t in rng] if rng else None})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/api/ingest" and cfg.ingest_token:
                return self._ingest()
            if path in ("/api/run/watch", "/api/run/pull-es") and cfg.trigger_token:
                return self._run(path)
            if path not in ("/api/ask", "/api/ask/stream"):
                return self._send(404, b"not found", "text/plain")
            try:
                q = self._question()
            except BadRequest as e:
                return self._json(400, {"error": str(e)})
            key = q.lower()
            cached = guard.cached(key)
            if cached is None:
                reason = guard.admit(self._client_ip())
                if reason:
                    return self._json(429, {"error": reason})
            try:
                if self.path == "/api/ask":
                    res = cached
                    if res is None:
                        with db_lock:
                            inv = det.prepare(q)
                        res = public_view(det.answer(inv))
                        guard.store(key, res)
                    return self._json(200, res)
                self._stream(q, key, cached)
            except (BrokenPipeError, ConnectionResetError):
                pass  # visitor went away
            except Exception as e:
                print(f"[web] ask failed: {e!r}")
                try:
                    self._json(500, {"error": "internal error"})
                except OSError:
                    pass

        def _run(self, path: str) -> None:
            """External-scheduler trigger: one watch cycle or one Elasticsearch pull (bearer LOGDET_TRIGGER_TOKEN)."""
            if not bearer_ok(self.headers.get("Authorization"), cfg.trigger_token):
                return self._json(401, {"error": "missing or invalid bearer token"})
            if not run_lock.acquire(blocking=False):
                return self._json(409, {"error": "a run is already in progress"}, {"Retry-After": "30"})
            try:
                if path == "/api/run/watch":
                    found = watcher().run_once(db_lock=db_lock)
                    return self._json(200, {"new_findings": [public_finding(f) for f in found]})
                from .es import pull_once
                with db_lock:
                    res = pull_once(det, log=lambda *_: None)
                return self._json(200, res)
            except Exception as e:
                print(f"[web] run {path} failed: {e!r}")
                return self._json(503, {"error": "run failed, retry later"}, {"Retry-After": "30"})
            finally:
                run_lock.release()

        def _ingest(self) -> None:
            """Authenticated push ingest: NDJSON, JSON array or plain lines; ?source=<name>."""
            if not bearer_ok(self.headers.get("Authorization"), cfg.ingest_token):
                return self._json(401, {"error": "missing or invalid bearer token"})
            if not ingest_limiter.allow(self._client_ip()):
                return self._json(429, {"error": f"rate limit: {cfg.ingest_per_minute} requests per minute"},
                                  {"Retry-After": "60"})
            qs = parse_qs(urlsplit(self.path).query)
            source = (qs.get("source") or [self.headers.get("X-Logdet-Source") or "http"])[0]
            if not _SOURCE_RE.match(source):
                return self._json(400, {"error": "source must match [A-Za-z0-9._-]{1,64}"})
            try:
                n = int(self.headers.get("Content-Length") or -1)
            except ValueError:
                n = -1
            if n < 0:
                return self._json(411, {"error": "Content-Length required"})
            if n > cfg.ingest_max_bytes:
                return self._json(413, {"error": f"body too large (max {cfg.ingest_max_bytes} bytes)"})
            body = self.rfile.read(n)
            key = (self.headers.get("Idempotency-Key") or "").strip()[:200]
            if key:
                with db_lock:
                    prev = det.store.idempotent_response(f"{source}:{key}")
                if prev:
                    return self._send(200, prev.encode(), "application/json")
            if not ingest_slots.acquire(timeout=10):  # overloaded: tell the sender to come back
                return self._json(503, {"error": "busy, retry later"}, {"Retry-After": "5"})
            try:
                events = parse_payload(body, source, cfg.tz)
                with db_lock:
                    res = ingestor.add_new(source, events)
                    out = json.dumps({"accepted": res["events"], "duplicates": res["duplicates"],
                                      "windows_upserted": res["chunks"], "source": source})
                    if key:
                        det.store.remember_key(f"{source}:{key}", out, time.time())
            except Exception as e:  # nothing was kept (rolled back): safe to retry the same batch
                print(f"[web] ingest failed: {e!r}")
                return self._json(503, {"error": "ingest temporarily unavailable, retry"}, {"Retry-After": "10"})
            finally:
                ingest_slots.release()
            self._send(200, out.encode(), "application/json")

        def _stream(self, q: str, key: str, cached: dict | None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(obj) -> None:
                self.wfile.write(json.dumps(obj, ensure_ascii=False, default=str).encode() + b"\n")
                self.wfile.flush()

            if cached is not None:
                emit({"type": "meta", **{k: v for k, v in cached.items() if k != "answer"}, "cached": True})
                emit({"type": "delta", "text": cached["answer"]})
                emit({"type": "done", **cached, "cached": True})
                return
            with db_lock:
                inv = det.prepare(q)
            meta = public_view(det.result(inv, "", None, None))
            meta.pop("answer", None)
            emit({"type": "meta", **meta})
            for kind, payload in det.answer_stream(inv):
                if kind == "delta":
                    emit({"type": "delta", "text": payload})
                else:
                    res = public_view(payload)
                    guard.store(key, res)
                    emit({"type": "done", **res})

        def log_message(self, fmt, *args):
            print(f"[web] {self._client_ip() if hasattr(self, 'headers') else '-'} {fmt % args}")

    return Handler


def make_server(cfg: Config, host: str = "0.0.0.0", port: int = 8080,
                det: Detective | None = None, guard: AskGuard | None = None) -> ThreadingHTTPServer:
    det = det or Detective(cfg)
    guard = guard or AskGuard(int(os.environ.get("LOGDET_ASK_PER_IP_PER_HOUR", "20")),
                              int(os.environ.get("LOGDET_ASK_DAILY_BUDGET", "300")))
    srv = ThreadingHTTPServer((host, port), make_handler(det, cfg, guard))
    srv.daemon_threads = True
    return srv


def serve(cfg: Config, host: str = "0.0.0.0", port: int = 8080) -> None:
    print(f"log-detective web UI on http://{host}:{port}")
    make_server(cfg, host, port).serve_forever()
