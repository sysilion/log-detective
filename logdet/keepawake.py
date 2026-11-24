"""Opt-in 'stay awake' for background loops on a Sprite (LOGDET_KEEP_AWAKE=1).

A paused Sprite freezes every process, so `logdet tail`, `logdet watch` and `logdet pull-es --loop` stop
while it sleeps (they catch up from checkpoints after the next wake). With LOGDET_KEEP_AWAKE=1 the loop
holds a Sprites Tasks API task (refreshed every 60 s with a 5-minute expiry), which keeps the Sprite
running and billing while the loop runs. The task is released on exit; if the process dies, it expires.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading

API_SOCK = "/.sprite/api.sock"


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 5.0):
        super().__init__("sprite", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def _task_call(method: str, path: str, body: dict | None = None, sock: str = API_SOCK) -> int:
    conn = _UnixHTTPConnection(sock)
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        return conn.getresponse().status
    finally:
        conn.close()


class KeepAwake:
    """Context manager holding a Tasks API task while the block runs (no-op unless enabled + on a Sprite)."""

    def __init__(self, name: str, enabled: bool | None = None, sock: str = API_SOCK, refresh: float = 60.0,
                 expire: str = "5m", log=print):
        self.name, self.sock, self.refresh, self.expire, self.log = name, sock, refresh, expire, log
        if enabled is None:
            enabled = os.environ.get("LOGDET_KEEP_AWAKE", "").strip() in ("1", "true", "yes")
        self.active = enabled and os.path.exists(sock)
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def _beat(self) -> None:
        while not self._stop.is_set():
            try:
                _task_call("PUT", f"/v1/tasks/logdet-{self.name}", {"expire": self.expire}, self.sock)
            except OSError as e:
                self.log(f"[keep-awake] refresh failed: {e}")
            self._stop.wait(self.refresh)

    def __enter__(self):
        if self.active:
            self.log(f"[keep-awake] holding Sprites task 'logdet-{self.name}' (refresh {self.refresh:g}s)")
            self._th = threading.Thread(target=self._beat, daemon=True)
            self._th.start()
        return self

    def __exit__(self, *exc):
        if self.active:
            self._stop.set()
            try:
                _task_call("DELETE", f"/v1/tasks/logdet-{self.name}", None, self.sock)
            except OSError:
                pass
        return False
