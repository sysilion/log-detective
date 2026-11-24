"""Parse log lines (JSON lines, nginx access logs, plain timestamped text) into events."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Iterator

ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL", "PANIC", "EMERG", "ALERT", "CRIT"}
WARN_LEVELS = {"WARN", "WARNING"}


@dataclass
class Event:
    source: str
    lineno: int
    ts: float
    level: str
    msg: str
    raw: str
    status: int | None = None
    latency_ms: float | None = None
    path: str | None = None
    service: str | None = None

    @property
    def is_error(self) -> bool:
        return (self.status is not None and self.status >= 500) or self.level in ERROR_LEVELS


# ---------------------------------------------------------------- timestamps
_ISO_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,](\d{1,9}))?\s?(Z|[+-]\d{2}:?\d{2}|UTC)?")
_NGINX_TS_RE = re.compile(r"\[(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}) ([+-]\d{4})\]")


def parse_iso(text: str, default_tz: tzinfo) -> float | None:
    m = _ISO_RE.search(text)
    if not m:
        return None
    date, clock, frac, zone = m.groups()
    s = f"{date}T{clock}"
    if frac:
        s += "." + frac[:6].ljust(6, "0")
    if zone in (None, ""):
        dt = datetime.fromisoformat(s).replace(tzinfo=default_tz)
    elif zone in ("Z", "UTC"):
        dt = datetime.fromisoformat(s + "+00:00")
    else:
        if ":" not in zone:
            zone = zone[:3] + ":" + zone[3:]
        dt = datetime.fromisoformat(s + zone)
    return dt.timestamp()


def _ts_from_value(v, default_tz: tzinfo) -> float | None:
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e11 else 1.0)
    if isinstance(v, str):
        return parse_iso(v, default_tz)
    return None


# ---------------------------------------------------------------- formats
_NGINX_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
    r'(?P<status>\d{3}) (?P<bytes>\d+|-)(?: "[^"]*" "[^"]*")?(?P<rest>.*)$')
_RT_RE = re.compile(r"(?:rt=|request_time=)?(\d+\.\d+)\s*$")
_LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|FATAL|CRITICAL|PANIC|LOG)\b")

_TS_KEYS = ("ts", "time", "timestamp", "@timestamp", "datetime", "date")
_MSG_KEYS = ("msg", "message", "event", "log", "error")
_LAT_KEYS = ("latency_ms", "duration_ms", "elapsed_ms", "response_time_ms", "took_ms")
_LAT_SEC_KEYS = ("latency", "duration", "request_time", "elapsed")


def _parse_json(line: str, source: str, lineno: int, tz: tzinfo) -> Event | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    ts = next((t for k in _TS_KEYS if k in obj for t in [_ts_from_value(obj[k], tz)] if t), None)
    if ts is None:
        return None
    level = str(obj.get("level") or obj.get("severity") or obj.get("lvl") or "INFO").upper()
    msg = next((str(obj[k]) for k in _MSG_KEYS if obj.get(k)), "")
    lat = next((float(obj[k]) for k in _LAT_KEYS if isinstance(obj.get(k), (int, float))), None)
    if lat is None:
        lat = next((float(obj[k]) * 1000 for k in _LAT_SEC_KEYS if isinstance(obj.get(k), (int, float))), None)
    status = obj.get("status") or obj.get("status_code") or obj.get("http_status")
    status = int(status) if isinstance(status, (int, str)) and str(status).isdigit() else None
    path = obj.get("path") or obj.get("route") or obj.get("endpoint") or obj.get("url")
    service = obj.get("service") or obj.get("app") or obj.get("logger")
    return Event(source, lineno, ts, level, msg, line, status, lat,
                 str(path) if path else None, str(service) if service else None)


def _parse_nginx(line: str, source: str, lineno: int, tz: tzinfo) -> Event | None:
    m = _NGINX_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m["ts"], "%d/%b/%Y:%H:%M:%S %z").timestamp()
    except ValueError:
        return None
    status = int(m["status"])
    rt = _RT_RE.search(m["rest"] or "")
    lat = float(rt.group(1)) * 1000 if rt else None
    level = "ERROR" if status >= 500 else ("WARN" if status >= 400 else "INFO")
    path = m["path"].split("?", 1)[0]
    msg = f'{m["method"]} {path} {status}'
    return Event(source, lineno, ts, level, msg, line, status, lat, path, "nginx")


def _parse_plain(line: str, source: str, lineno: int, tz: tzinfo) -> Event | None:
    ts = parse_iso(line[:40], tz)
    if ts is None:
        return None
    lm = _LEVEL_RE.search(line)
    level = (lm.group(1) if lm else "INFO").upper()
    if level == "LOG":
        level = "INFO"
    msg = line[lm.end():].strip(" :-]") if lm else _ISO_RE.sub("", line, count=1).strip()
    lat = None
    dm = re.search(r"duration[:=]\s*(\d+(?:\.\d+)?)\s*ms", line)
    if dm:
        lat = float(dm.group(1))
    return Event(source, lineno, ts, level, msg, line, None, lat, None, None)


def parse_line(line: str, source: str, lineno: int, tz: tzinfo) -> Event | None:
    line = line.rstrip("\n")
    if not line.strip():
        return None
    if line.lstrip().startswith("{"):
        ev = _parse_json(line, source, lineno, tz)
        if ev:
            return ev
    if _NGINX_TS_RE.search(line[:120]):
        ev = _parse_nginx(line, source, lineno, tz)
        if ev:
            return ev
    return _parse_plain(line, source, lineno, tz)


def parse_lines(lines, source: str, tz: tzinfo) -> Iterator[Event]:
    """Yield events; un-timestamped lines (e.g. stack traces) are folded into the previous event."""
    prev: Event | None = None
    for i, line in enumerate(lines, 1):
        ev = parse_line(line, source, i, tz)
        if ev is None:
            if prev is not None and line.strip():
                prev.raw += "\n" + line.rstrip("\n")
            continue
        if prev is not None:
            yield prev
        prev = ev
    if prev is not None:
        yield prev


# ---------------------------------------------------------------- signatures
_SIG_SUBS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"(?<![a-z])\d+(?:\.\d+)?(?!xx)"), "<n>"),
]


def signature(ev: Event) -> str:
    """Normalize a message into a stable signature (numbers, ids, quoted strings removed)."""
    text = ev.msg or ev.raw
    if ev.service == "nginx" and ev.path:
        text = f"{ev.msg.split()[0]} {ev.path} -> {(ev.status or 0) // 100}xx"
    for rx, rep in _SIG_SUBS:
        text = rx.sub(rep, text)
    return re.sub(r"\s+", " ", text).strip()[:200]
