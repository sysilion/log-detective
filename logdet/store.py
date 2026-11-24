"""Local SQLite store for parsed events and time-window chunks.

Raw events stay on the box (stats are computed from them locally); only chunk
embeddings + chunk text go to the vector store.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .parse import Event, signature

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  source TEXT, lineno INTEGER, ts REAL, level TEXT, status INTEGER,
  latency_ms REAL, path TEXT, service TEXT, msg TEXT, sig TEXT, is_error INTEGER, raw TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_src ON events(source, lineno);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, source TEXT, ts_start REAL, ts_end REAL, n_lines INTEGER,
  n_errors INTEGER, text TEXT
);
CREATE INDEX IF NOT EXISTS chunks_ts ON chunks(ts_start);
"""

MAX_CHUNK_LINES = 40
MAX_CHUNK_CHARS = 6000


@dataclass
class Chunk:
    id: str
    source: str
    ts_start: float
    ts_end: float
    n_lines: int
    n_errors: int
    text: str


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release (idempotent)."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        if "ehash" not in cols:
            self.db.execute("ALTER TABLE events ADD COLUMN ehash TEXT")
        self.db.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS events_ehash ON events(ehash);
            CREATE TABLE IF NOT EXISTS ingest_keys (key TEXT PRIMARY KEY, ts REAL, response TEXT);
        """)
        self.db.commit()

    def reset(self) -> None:
        self.db.executescript("DELETE FROM events; DELETE FROM chunks;")
        self.db.commit()

    def remove_source(self, source: str) -> None:
        self.db.execute("DELETE FROM events WHERE source=?", (source,))
        self.db.execute("DELETE FROM chunks WHERE source=?", (source,))
        self.db.commit()

    def add_events(self, events: Iterable[Event], commit: bool = True, hashes: list[str] | None = None) -> int:
        events = list(events)
        hashes = hashes or [None] * len(events)
        rows = [(e.source, e.lineno, e.ts, e.level, e.status, e.latency_ms, e.path, e.service,
                 e.msg, signature(e), int(e.is_error), e.raw, h) for e, h in zip(events, hashes)]
        self.db.executemany(
            "INSERT INTO events(source,lineno,ts,level,status,latency_ms,path,service,msg,sig,is_error,raw,ehash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if commit:
            self.db.commit()
        return len(rows)

    def known_hashes(self, hashes: list[str]) -> set[str]:
        """Which of these event hashes are already stored (for idempotent push ingest)."""
        found: set[str] = set()
        for i in range(0, len(hashes), 500):
            part = hashes[i:i + 500]
            q = ",".join("?" * len(part))
            found.update(r[0] for r in self.db.execute(f"SELECT ehash FROM events WHERE ehash IN ({q})", part))
        return found

    def idempotent_response(self, key: str) -> str | None:
        r = self.db.execute("SELECT response FROM ingest_keys WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def remember_key(self, key: str, response: str, ts: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO ingest_keys VALUES (?,?,?)", (key, ts, response))
        self.db.execute("DELETE FROM ingest_keys WHERE ts < ?", (ts - 7 * 86400,))  # keys live 7 days
        self.db.commit()

    def add_chunks(self, chunks: list[Chunk], commit: bool = True) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?)",
            [(c.id, c.source, c.ts_start, c.ts_end, c.n_lines, c.n_errors, c.text) for c in chunks])
        if commit:
            self.db.commit()

    def events(self, start: float, end: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)).fetchall()

    def time_range(self) -> tuple[float, float] | None:
        r = self.db.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
        return (r[0], r[1]) if r and r[0] is not None else None

    def counts(self) -> tuple[int, int]:
        return (self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def prune(self, cutoff: float, dry_run: bool = False) -> tuple[int, int]:
        """Delete events older than `cutoff` and chunks whose window ended before it. Returns (events, chunks)."""
        ev = self.db.execute("SELECT COUNT(*) FROM events WHERE ts < ?", (cutoff,)).fetchone()[0]
        ch = self.db.execute("SELECT COUNT(*) FROM chunks WHERE ts_end < ?", (cutoff,)).fetchone()[0]
        if not dry_run and (ev or ch):
            self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM chunks WHERE ts_end < ?", (cutoff,))
            self.db.commit()
        return ev, ch

    def events_for(self, source: str, start: float, end: float) -> list[Event]:
        """Events of one source in [start, end) as Event objects (used to rebuild a touched window)."""
        rows = self.db.execute(
            "SELECT source, lineno, ts, level, msg, raw, status, latency_ms, path, service FROM events "
            "WHERE source=? AND ts>=? AND ts<? ORDER BY ts, lineno", (source, start, end)).fetchall()
        return [Event(r["source"], r["lineno"], r["ts"], r["level"], r["msg"], r["raw"], r["status"],
                      r["latency_ms"], r["path"], r["service"]) for r in rows]

    def max_lineno(self, source: str) -> int:
        r = self.db.execute("SELECT MAX(lineno) FROM events WHERE source=?", (source,)).fetchone()
        return int(r[0] or 0)

    def chunks_between(self, start: float, end: float) -> list[Chunk]:
        rows = self.db.execute("SELECT * FROM chunks WHERE ts_start < ? AND ts_end > ? ORDER BY ts_start",
                               (end, start)).fetchall()
        return [Chunk(**dict(r)) for r in rows]



def _interesting(e: Event) -> int:
    """Rank lines so busy windows keep the informative ones."""
    if e.is_error:
        return 0
    if e.level in ("WARN", "WARNING") or (e.status and e.status >= 400):
        return 1
    if e.latency_ms is not None and e.latency_ms >= 1000:
        return 1
    return 2


def build_chunks(events: list[Event], window_seconds: int) -> list[Chunk]:
    """Group a single source's events into fixed time windows."""
    buckets: dict[tuple[str, int], list[Event]] = {}
    for e in events:
        buckets.setdefault((e.source, int(e.ts // window_seconds)), []).append(e)
    chunks = []
    for (source, b), evs in sorted(buckets.items()):
        picked = sorted(evs, key=lambda e: (_interesting(e), e.ts))[:MAX_CHUNK_LINES]
        picked.sort(key=lambda e: e.ts)
        n_err = sum(e.is_error for e in evs)
        lat = [e.latency_ms for e in evs if e.latency_ms is not None]
        header = f"source={source} lines={len(evs)} errors={n_err}"
        if lat:
            lat.sort()
            header += f" p95_ms={lat[int(0.95 * (len(lat) - 1))]:.0f}"
        body, size = [], len(header)
        for e in picked:
            line = f"[{source}:{e.lineno}] {e.raw.splitlines()[0][:400]}"
            if size + len(line) > MAX_CHUNK_CHARS:
                break
            body.append(line)
            size += len(line) + 1
        start = b * window_seconds
        cid = hashlib.sha1(f"{source}|{start}".encode()).hexdigest()[:24]
        chunks.append(Chunk(cid, source, float(start), float(start + window_seconds), len(evs), n_err,
                            header + "\n" + "\n".join(body)))
    return chunks
