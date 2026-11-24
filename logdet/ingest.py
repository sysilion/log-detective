"""Incremental ingestion shared by every continuous mode.

- `Checkpoints`: per-source progress in LOGDET_HOME/checkpoints.json
  (files: inode + byte offset + line counter; Elasticsearch: last timestamp + ids seen at it).
- `IncrementalIngestor.add()`: store only the new events, then rebuild and re-upsert just the
  time windows they touched. Chunk ids are deterministic (source|window start), so a window that is
  still open is overwritten in place until it closes.
- `follow_files()`: read only lines appended since the last run; handles rotation and truncation.
- `parse_payload()`: NDJSON / JSON array / plain-text bodies (HTTP ingest, Fluent Bit, Vector).

Line numbers in follow modes are per-source sequence numbers (they keep counting across rotations),
so citations stay unique.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Iterable

from .parse import Event, parse_line, parse_lines
from .store import build_chunks

CHECKPOINT_FILE = "checkpoints.json"


class Checkpoints:
    """Small JSON key/value file; writes are atomic (tmp + rename)."""

    def __init__(self, home: Path):
        self.path = Path(home) / CHECKPOINT_FILE
        self.lock = threading.Lock()
        try:
            self.data: dict = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> dict:
        return dict(self.data.get(key) or {})

    def set(self, key: str, value: dict) -> None:
        with self.lock:
            self.data[key] = value
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            os.replace(tmp, self.path)


class IncrementalIngestor:
    """Adds events for one source and refreshes only the windows they touch."""

    def __init__(self, det):
        self.det = det
        self.cfg = det.cfg

    def add(self, source: str, events: list[Event]) -> dict:
        if not events:
            return {"events": 0, "chunks": 0}
        st = self.det.store
        st.add_events(events)
        w = self.cfg.window_seconds
        buckets = sorted({int(e.ts // w) for e in events})
        chunks = []
        for b in buckets:
            chunks += build_chunks(st.events_for(source, b * w, (b + 1) * w), w)
        st.add_chunks(chunks)
        self.det.upsert_chunks(chunks)
        return {"events": len(events), "chunks": len(chunks)}

    def renumber(self, source: str, events: list[Event]) -> list[Event]:
        """Give events per-source sequence numbers continuing after what is already stored."""
        base = self.det.store.max_lineno(source)
        for i, e in enumerate(events, 1):
            e.lineno = base + i
            e.source = source
        return events


def read_new_lines(path: Path, ck: dict) -> tuple[list[str], dict]:
    """Return complete lines appended since checkpoint `ck`, and the new checkpoint.

    Rotation (inode changed) and truncation (file smaller than the offset) restart from byte 0.
    A trailing partial line is left for the next read.
    """
    st = path.stat()
    offset = int(ck.get("offset", 0))
    if ck.get("inode") != st.st_ino or st.st_size < offset:
        offset = 0
    if st.st_size == offset:
        return [], {"inode": st.st_ino, "offset": offset}
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], {"inode": st.st_ino, "offset": offset}
    complete = data[:end + 1]
    lines = complete.decode("utf-8", errors="replace").splitlines(keepends=True)
    return lines, {"inode": st.st_ino, "offset": offset + len(complete)}


def follow_files(det, paths: list[str], ckpts: Checkpoints | None = None, log=print) -> dict:
    """One incremental pass over `paths` (files or directories)."""
    from .agent import _iter_files
    ckpts = ckpts or Checkpoints(det.cfg.home)
    ing = IncrementalIngestor(det)
    total = {"events": 0, "chunks": 0, "files": 0}
    for f in _iter_files(paths):
        key = "file:" + str(f.resolve())
        lines, new_ck = read_new_lines(f, ckpts.get(key))
        if lines:
            events = ing.renumber(f.name, list(parse_lines(lines, f.name, det.cfg.tz)))
            res = ing.add(f.name, events)
            total["events"] += res["events"]
            total["chunks"] += res["chunks"]
            total["files"] += 1
            log(f"  {f.name}: +{res['events']} events, {res['chunks']} windows refreshed")
        ckpts.set(key, new_ck)
    return total


def tail(det, paths: list[str], flush_seconds: float, log=print, stop: threading.Event | None = None,
         max_cycles: int | None = None) -> None:
    """Poll files and flush new lines every `flush_seconds` (no dependencies). Ctrl-C/SIGTERM to stop."""
    ckpts = Checkpoints(det.cfg.home)
    last_prune, cycles = 0.0, 0
    while not (stop and stop.is_set()):
        try:
            res = follow_files(det, paths, ckpts, log=lambda *_: None)
            if res["events"]:
                log(f"[tail] +{res['events']} events from {res['files']} file(s), {res['chunks']} windows upserted")
        except Exception as e:  # keep tailing through transient errors (vector store hiccups etc.)
            log(f"[tail] error: {str(e)[:200]}")
        if det.cfg.retention_days and time.time() - last_prune > 3600:
            last_prune = time.time()
            try:
                det.prune()
            except Exception as e:
                log(f"[tail] prune error: {str(e)[:160]}")
        cycles += 1
        if max_cycles and cycles >= max_cycles:
            return
        if stop:
            stop.wait(flush_seconds)
        else:
            time.sleep(flush_seconds)


def parse_payload(body: bytes, source: str, tz) -> list[Event]:
    """Parse an HTTP ingest body: NDJSON, a JSON array of objects (Fluent Bit `format json`) or plain lines."""
    text = body.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    lines: Iterable[str]
    if stripped.startswith("["):
        try:
            arr = json.loads(stripped)
            lines = [json.dumps(o) if isinstance(o, dict) else str(o) for o in arr]
        except ValueError:
            lines = text.splitlines()
    else:
        lines = text.splitlines()
    events, now = [], time.time()
    for i, line in enumerate(lines, 1):
        ev = parse_line(line, source, i, tz)
        if ev is None and line.strip():  # no timestamp: accept it as "now" rather than dropping it
            ev = Event(source, i, now, "INFO", line.strip()[:2000], line.strip()[:4000])
        if ev:
            events.append(ev)
    return events
