"""`logdet pull-es`: incremental pull from Elasticsearch (stdlib urllib only).

Paging: a point-in-time (PIT) + `search_after` scan, sorted by the timestamp field (ES adds the implicit
`_shard_doc` tiebreaker), so deep pages are consistent. Clusters without PIT (old ES, OpenSearch) fall back
to timestamp-range pages.

Checkpoint: the last timestamp seen (epoch ms). The next run starts *at* that timestamp (inclusive) and
documents seen twice at the boundary are dropped by the idempotent event hash, which includes the ES `_id`.

Configuration (env vars, or "elasticsearch" in the auth file for credentials):
  LOGDET_ES_URL, LOGDET_ES_INDEX (default logs-*), LOGDET_ES_TS_FIELD (@timestamp),
  LOGDET_ES_QUERY (query DSL JSON or a Lucene query string), LOGDET_ES_FIELDS (JSON field-name overrides),
  LOGDET_ES_LATENCY_UNIT (ms|s|us|ns), LOGDET_ES_BATCH (1000), LOGDET_ES_START (ISO start when no checkpoint),
  LOGDET_ES_API_KEY or LOGDET_ES_USER + LOGDET_ES_PASSWORD.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

from .config import _env, auth_file_path
from .ingest import Checkpoints, IncrementalIngestor
from .net import HTTPError, request_json
from .parse import ERROR_LEVELS, Event

DEFAULT_FIELDS = {
    "message": ["message", "log", "msg", "event.original"],
    "level": ["log.level", "level", "severity", "loglevel"],
    "status": ["http.response.status_code", "status", "status_code"],
    "latency": ["latency_ms", "duration_ms", "event.duration", "response_time"],
    "service": ["service.name", "service", "app", "kubernetes.container.name"],
    "path": ["url.path", "path", "route", "http.request.path"],
}
UNIT_TO_MS = {"ms": 1.0, "s": 1000.0, "us": 0.001, "ns": 0.000001}


def _auth_entry() -> dict:
    try:
        with open(auth_file_path()) as fh:
            e = json.load(fh).get("elasticsearch") or {}
        return e if isinstance(e, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass
class ESConfig:
    url: str = field(default_factory=lambda: _env("LOGDET_ES_URL").rstrip("/"))
    index: str = field(default_factory=lambda: _env("LOGDET_ES_INDEX", "logs-*"))
    ts_field: str = field(default_factory=lambda: _env("LOGDET_ES_TS_FIELD", "@timestamp"))
    query: str = field(default_factory=lambda: _env("LOGDET_ES_QUERY"))
    fields_json: str = field(default_factory=lambda: _env("LOGDET_ES_FIELDS"))
    latency_unit: str = field(default_factory=lambda: _env("LOGDET_ES_LATENCY_UNIT", "ms"))
    batch: int = field(default_factory=lambda: int(_env("LOGDET_ES_BATCH", "1000")))
    start: str = field(default_factory=lambda: _env("LOGDET_ES_START"))
    api_key: str = field(default_factory=lambda: _env("LOGDET_ES_API_KEY") or str(_auth_entry().get("key") or ""))
    user: str = field(default_factory=lambda: _env("LOGDET_ES_USER") or str(_auth_entry().get("user") or ""))
    password: str = field(default_factory=lambda: _env("LOGDET_ES_PASSWORD") or str(_auth_entry().get("password") or ""))

    def headers(self) -> dict:
        if self.api_key:
            return {"Authorization": f"ApiKey {self.api_key}"}
        if self.user:
            tok = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            return {"Authorization": f"Basic {tok}"}
        return {}

    def field_map(self) -> dict:
        m = {k: list(v) for k, v in DEFAULT_FIELDS.items()}
        if self.fields_json:
            for k, v in json.loads(self.fields_json).items():
                m[k] = [v] if isinstance(v, str) else list(v)
        return m

    def user_query(self) -> dict | None:
        q = self.query.strip()
        if not q:
            return None
        if q.startswith("{"):
            return json.loads(q)
        return {"query_string": {"query": q}}


def _get(src: dict, path: str):
    """Read 'a.b.c' from nested _source, also accepting flat dotted keys."""
    if path in src:
        return src[path]
    cur = src
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first(src: dict, paths: list[str]):
    for p in paths:
        v = _get(src, p)
        if v not in (None, ""):
            return v
    return None


def hit_to_event(hit: dict, ts_ms: float, fmap: dict, unit: str) -> Event:
    src = hit.get("_source") or {}
    msg = _first(src, fmap["message"])
    msg = msg if isinstance(msg, str) else json.dumps(msg) if msg is not None else ""
    status = _first(src, fmap["status"])
    status = int(status) if isinstance(status, (int, float)) or (isinstance(status, str) and status.isdigit()) else None
    lat = _first(src, fmap["latency"])
    lat = float(lat) * UNIT_TO_MS.get(unit, 1.0) if isinstance(lat, (int, float)) else None
    level = str(_first(src, fmap["level"]) or ("ERROR" if status and status >= 500 else "INFO")).upper()
    service = _first(src, fmap["service"])
    path = _first(src, fmap["path"])
    source = str(service or hit.get("_index") or "es")[:64].replace("/", "_")
    raw = json.dumps({"_id": hit.get("_id"), "@ts": datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
                      .isoformat(timespec="milliseconds"), "level": level, "service": service,
                      "status": status, "latency_ms": lat, "path": path, "message": msg[:1500]},
                     ensure_ascii=False)
    if level not in ERROR_LEVELS and status and status >= 500:
        level = "ERROR"
    return Event(source, 0, ts_ms / 1000, level, msg[:2000], raw, status, lat,
                 str(path) if path else None, str(service) if service else None)


def _iso_to_ms(s: str) -> float:
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000


def pull_once(det, es: ESConfig | None = None, log=print) -> dict:
    """Fetch documents newer than the checkpoint and ingest them idempotently."""
    es = es or ESConfig()
    if not es.url:
        raise RuntimeError("LOGDET_ES_URL is not set")
    ckpts = Checkpoints(det.cfg.home)
    key = f"es:{es.url}|{es.index}|{es.ts_field}"
    ck = ckpts.get(key)
    lower = ck.get("ts") or (_iso_to_ms(es.start) if es.start else (time.time() - 86400) * 1000)
    hdr, fmap = es.headers(), es.field_map()
    filters = [{"range": {es.ts_field: {"gte": int(lower), "format": "epoch_millis"}}}]
    uq = es.user_query()
    if uq:
        filters.append(uq)
    base_body = {"size": es.batch, "query": {"bool": {"filter": filters}},
                 "sort": [{es.ts_field: {"order": "asc"}}], "track_total_hits": False}

    pit = None
    try:
        pit = request_json("POST", f"{es.url}/{quote(es.index, safe='*,')}/_pit?keep_alive=2m",
                           None, hdr, timeout=30).get("id")
    except (HTTPError, AttributeError):
        pit = None  # no PIT support: timestamp-range pages

    ing = IncrementalIngestor(det)
    stats = {"fetched": 0, "accepted": 0, "duplicates": 0, "pages": 0, "mode": "pit" if pit else "range"}
    max_ts, search_after, range_lower = lower, None, lower
    try:
        while True:
            body = dict(base_body)
            if pit:
                body["pit"] = {"id": pit, "keep_alive": "2m"}
                if search_after:
                    body["search_after"] = search_after
                url = f"{es.url}/_search"
            else:
                body["query"] = {"bool": {"filter": [
                    {"range": {es.ts_field: {"gte": int(range_lower), "format": "epoch_millis"}}}] + filters[1:]}}
                url = f"{es.url}/{quote(es.index, safe='*,')}/_search"
            res = request_json("POST", url, body, hdr, timeout=60)
            if pit and res.get("pit_id"):
                pit = res["pit_id"]
            hits = (res.get("hits") or {}).get("hits") or []
            stats["pages"] += 1
            if not hits:
                break
            by_source: dict[str, list[Event]] = {}
            for h in hits:
                ts_ms = float((h.get("sort") or [None])[0] or _iso_to_ms(str(_get(h["_source"], es.ts_field))))
                ev = hit_to_event(h, ts_ms, fmap, es.latency_unit)
                by_source.setdefault(ev.source, []).append(ev)
                max_ts = max(max_ts, ts_ms)
            for source, evs in by_source.items():
                r = ing.add_new(source, evs)
                stats["accepted"] += r["events"]
                stats["duplicates"] += r["duplicates"]
            stats["fetched"] += len(hits)
            if len(hits) < es.batch:
                break
            if pit:
                search_after = hits[-1]["sort"]
            else:
                page_max = float(hits[-1]["sort"][0])
                if page_max <= range_lower:  # a full page with one timestamp: can't advance without PIT
                    log(f"[pull-es] warning: more than {es.batch} docs share timestamp {page_max}; "
                        "raise LOGDET_ES_BATCH or use a cluster with PIT")
                    break
                range_lower = page_max
    finally:
        if pit:
            try:
                request_json("DELETE", f"{es.url}/_pit", {"id": pit}, hdr, timeout=15)
            except Exception:
                pass
    ckpts.set(key, {"ts": max_ts, "updated": time.time()})
    stats["checkpoint_ms"] = max_ts
    return stats
