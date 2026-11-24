#!/usr/bin/env python3
"""Minimal log shipper for log-detective's POST /api/ingest (stdlib only).

  LOGDET_INGEST_TOKEN=... python3 scripts/ship.py https://<sprite>.sprites.app nginx < access.log
  tail -F app.log | python3 scripts/ship.py https://<sprite>.sprites.app app --batch 200

Retries 5xx, 429 and network errors with exponential backoff (honouring Retry-After) so a push survives a
cold Sprite wake; never retries other 4xx. Each batch carries an Idempotency-Key, so a retried batch is
never double counted.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import time
import urllib.error
import urllib.request


def send(url: str, token: str, body: bytes, max_wait: float = 300.0) -> dict | None:
    key = hashlib.sha1(body).hexdigest()
    delay, waited = 1.0, 0.0
    while True:
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "text/plain; charset=utf-8",
            "Idempotency-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=35) as r:  # >= 30 s: a cold wake can be slow
                return __import__("json").loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                print(f"giving up on batch: HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
                return None
            retry_after = float(e.headers.get("Retry-After") or 0)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            retry_after = 0.0
            print(f"network error ({e}), retrying", file=sys.stderr)
        wait = max(retry_after, delay) + random.uniform(0, 0.5)
        if waited + wait > max_wait:
            print("giving up on batch after retries", file=sys.stderr)
            return None
        time.sleep(wait)
        waited += wait
        delay = min(delay * 2, 30.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("base_url")
    ap.add_argument("source")
    ap.add_argument("--batch", type=int, default=500, help="lines per request")
    ap.add_argument("--flush", type=float, default=5.0, help="max seconds to hold a partial batch")
    a = ap.parse_args()
    token = os.environ.get("LOGDET_INGEST_TOKEN") or sys.exit("set LOGDET_INGEST_TOKEN")
    url = f"{a.base_url.rstrip('/')}/api/ingest?source={a.source}"
    buf, last = [], time.time()
    for line in sys.stdin:
        buf.append(line)
        if len(buf) >= a.batch or time.time() - last >= a.flush:
            res = send(url, token, "".join(buf).encode())
            print(res, file=sys.stderr)
            buf, last = [], time.time()
    if buf:
        print(send(url, token, "".join(buf).encode()), file=sys.stderr)


if __name__ == "__main__":
    main()
