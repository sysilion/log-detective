"""Ingest + ask orchestration: parse -> chunk -> embed -> vector store; question -> stats + retrieval -> LLM."""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from .analyze import Stats, analyze
from .config import Config
from .embed import get_embedder
from .llm import get_llm
from .parse import parse_lines
from .store import Store, build_chunks
from .timeparse import parse_time_range
from .vector import get_vector_store

LOG_SUFFIXES = {".log", ".jsonl", ".json", ".txt", ".ndjson", ""}
_CITE_LINE_RE = re.compile(r"^\[([^\]:]+):(\d+)\] (.*)$")


def _iter_files(paths: list[str]):
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in LOG_SUFFIXES
                              and not f.name.startswith("."))
        elif p.is_file():
            yield p
        else:
            raise FileNotFoundError(p)


class Detective:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.cfg.ensure_home()
        self.store = Store(self.cfg.db_path)
        self._vs = self._emb = None

    @property
    def vs(self):
        if self._vs is None:
            self._vs = get_vector_store(self.cfg)
        return self._vs

    @property
    def emb(self):
        if self._emb is None:
            self._emb = get_embedder(self.cfg)
        return self._emb

    # ------------------------------------------------------------ ingest
    def ingest(self, paths: list[str], reset: bool = False, log=print) -> dict:
        t0 = time.time()
        if reset:
            self.store.reset()
            self.vs.reset()
        if self.vs.name == "pinecone":
            dim = len(self.emb.embed(["dimension probe"])[0])
            log(f"pinecone index '{self.cfg.pinecone_index}': {self.vs.ensure_index(dim)} (via {self.vs.via})")
        total_ev = total_ch = 0
        for f in _iter_files(paths):
            source = f.name
            with open(f, encoding="utf-8", errors="replace") as fh:
                events = list(parse_lines(fh, source, self.cfg.tz))
            self.store.remove_source(source)
            self.store.add_events(events)
            chunks = build_chunks(events, self.cfg.window_seconds)
            self.store.add_chunks(chunks)
            vectors = []
            for i in range(0, len(chunks), 96):
                batch = chunks[i:i + 96]
                for c, v in zip(batch, self.emb.embed([c.text for c in batch], "passage")):
                    vectors.append({"id": c.id, "values": v, "metadata": {
                        "source": c.source, "ts_start": c.ts_start, "ts_end": c.ts_end,
                        "n_errors": c.n_errors, "text": c.text[:3500]}})
            self.vs.upsert(vectors)
            total_ev += len(events)
            total_ch += len(chunks)
            log(f"  {source}: {len(events)} events -> {len(chunks)} chunks")
        return {"events": total_ev, "chunks": total_ch, "vector": self.vs.name, "embed": self.emb.name,
                "seconds": round(time.time() - t0, 2)}

    # ------------------------------------------------------------ ask
    def ask(self, question: str, now: datetime | None = None, use_llm: bool = True) -> dict:
        t0 = time.time()
        now = now or datetime.now(self.cfg.tz)
        tr = parse_time_range(question, now, self.cfg.tz, self.store.time_range())
        stats = analyze(self.store, tr.start_ts, tr.end_ts)

        query = question + "\n" + "\n".join(s["sig"] for s in stats.signatures[:5])
        qv = self.emb.embed([query], "query")[0]
        hits = self.vs.query(qv, top_k=8, start=tr.start_ts - 1800, end=tr.end_ts)
        evidence = self._evidence(stats, hits)

        llm = get_llm(self.cfg) if use_llm else None
        answer, llm_name, llm_error = "", None, None
        if llm is not None:
            try:
                answer = llm.chat(SYSTEM_PROMPT, self._prompt(question, tr, stats, evidence))
                llm_name = llm.name
            except Exception as e:  # keep the demo alive if the model is unreachable
                llm_error = str(e)[:300]
        if not answer:
            answer = heuristic_report(question, stats, evidence, self.cfg.tz)
            llm_name = llm_name or "heuristic (no LLM)"
        return {
            "question": question,
            "range": {"start": tr.start.isoformat(), "end": tr.end.isoformat(), "label": tr.label,
                      "explicit": tr.explicit},
            "answer": answer, "llm": llm_name, "llm_error": llm_error,
            "vector": self.vs.name, "embed": self.emb.name,
            "stats": stats.to_dict(), "evidence": evidence,
            "retrieved": [{"id": h["id"], "score": round(h["score"], 3), "source": h["metadata"].get("source"),
                           "ts_start": h["metadata"].get("ts_start")} for h in hits],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    def _evidence(self, stats: Stats, hits: list[dict], limit: int = 14) -> list[dict]:
        seen, ev = set(), []

        def add(source, lineno, ts, line, why):
            key = (source, int(lineno))
            if key in seen or len(ev) >= limit:
                return
            seen.add(key)
            ev.append({"source": source, "lineno": int(lineno), "ts": ts, "line": line[:400], "why": why})

        db = self.store.db
        for s in stats.signatures[:4]:
            r = db.execute("SELECT * FROM events WHERE id=?", (s["first_event_id"],)).fetchone()
            if r:
                add(r["source"], r["lineno"], r["ts"], r["raw"].splitlines()[0],
                    "first occurrence of " + ("new " if s["new"] else "") + "error signature")
        anchor = stats.anomaly["start"] if stats.anomaly else stats.window.start
        near = sorted((c for c in stats.changes if anchor - 1200 <= c["ts"] <= anchor + 120),
                      key=lambda c: anchor - c["ts"])[:3]
        for c in near:
            r = db.execute("SELECT raw FROM events WHERE id=?", (c["id"],)).fetchone()
            add(c["source"], c["lineno"], c["ts"], r["raw"].splitlines()[0] if r else c["msg"],
                "change event before anomaly")
        for r in db.execute(
                "SELECT * FROM events WHERE ts>=? AND ts<? AND status IS NULL AND latency_ms>=5000 "
                "ORDER BY ts LIMIT 2", (stats.window.start, stats.window.end)):
            add(r["source"], r["lineno"], r["ts"], r["raw"].splitlines()[0], "slow operation (>5s)")
        for h in hits:
            md = h["metadata"]
            picked = 0
            for line in (md.get("text") or "").splitlines()[1:]:
                m = _CITE_LINE_RE.match(line)
                if not m or picked >= 3:
                    continue
                if re.search(r"ERROR|FATAL|WARN|timeout|timed out|exhaust|refused|duration: \d{4,}|\" 5\d\d ", m.group(3), re.I):
                    add(m.group(1), m.group(2), md.get("ts_start"), m.group(3), f"retrieved chunk (score {h['score']:.2f})")
                    picked += 1
        ev.sort(key=lambda e: e["ts"] or 0)
        for i, e in enumerate(ev, 1):
            e["cite"] = f"E{i}"
        return ev

    def _prompt(self, question: str, tr, stats: Stats, evidence: list[dict]) -> str:
        tz = self.cfg.tz
        fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S") if ts else "?"  # noqa: E731
        w, b = stats.window, stats.baseline
        lines = [f"QUESTION: {question}",
                 f"TIME WINDOW: {tr.start:%Y-%m-%d %H:%M} -> {tr.end:%Y-%m-%d %H:%M} ({tz.key})",
                 f"WINDOW: requests={w.requests} errors={w.errors} error_rate={w.error_rate:.1%} "
                 f"p50={_ms(w.p50)} p95={_ms(w.p95)} p99={_ms(w.p99)}",
                 f"BASELINE (preceding period): requests={b.requests} error_rate={b.error_rate:.1%} "
                 f"p50={_ms(b.p50)} p95={_ms(b.p95)}"]
        if stats.anomaly:
            a = stats.anomaly
            lines.append(f"ANOMALY: {fmt(a['start'])} -> {fmt(a['end'])}, peak p95={_ms(a['peak_p95'])} at "
                         f"{fmt(a['peak'])}, peak 5xx rate={a['peak_error_rate']:.0%}")
        else:
            lines.append("ANOMALY: none detected vs baseline")
        lines.append("TOP ERROR SIGNATURES (window vs baseline):")
        for s in stats.signatures[:6]:
            tag = "NEW" if s["new"] else f"x{s['ratio']:.1f}" if s["ratio"] else ""
            lines.append(f"- [{tag}] {s['count']}x first@{fmt(s['first_ts'])} ({s['source']}): {s['sig']}")
        if stats.paths:
            lines.append("SLOWEST ENDPOINTS: " + "; ".join(
                f"{p['path']} p95 {_ms(p['p95'])} (baseline {_ms(p['base_p95'])})" for p in stats.paths[:3]))
        lines.append("EVIDENCE LOG LINES (cite as [E#]):")
        for e in evidence:
            lines.append(f"[{e['cite']}] {e['source']}:{e['lineno']} {e['line']}")
        return "\n".join(lines)


SYSTEM_PROMPT = """You are Log Detective, a senior SRE investigating an incident from log evidence.
Answer in the same language as the QUESTION (Korean question -> Korean answer).
Use ONLY the statistics and evidence provided. Cite evidence lines inline as [E1], [E2]...
Format:
1. One-sentence answer (what happened, when, impact with numbers).
2. Timeline (3-6 bullets, HH:MM).
3. Root-cause hypothesis and why the evidence supports it; mention confidence (high/medium/low).
4. Next steps to confirm/fix (2-3 bullets).
Keep it under 250 words. Do not invent log lines or numbers."""


def _ms(v) -> str:
    return "-" if v is None else (f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms")


def heuristic_report(question: str, stats: Stats, evidence: list[dict], tz) -> str:
    """Template answer used when no LLM is reachable (fully offline mode)."""
    ko = bool(re.search(r"[가-힣]", question))
    fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "?"  # noqa: E731
    cite = {(e["source"], e["lineno"]): e["cite"] for e in evidence}
    w, b, a = stats.window, stats.baseline, stats.anomaly
    out = []
    if a:
        factor = (a["peak_p95"] or 0) / (b.p95 or 1)
        out.append(
            f"{fmt(a['start'])}~{fmt(a['end'])} 사이 p95 지연이 {_ms(b.p95)} → {_ms(a['peak_p95'])}(약 {factor:.0f}배)로 "
            f"치솟았고 5xx 비율은 최대 {a['peak_error_rate']:.0%}였습니다." if ko else
            f"Between {fmt(a['start'])} and {fmt(a['end'])}, p95 latency rose from {_ms(b.p95)} to "
            f"{_ms(a['peak_p95'])} (~{factor:.0f}x) and the 5xx rate peaked at {a['peak_error_rate']:.0%}.")
    else:
        out.append(f"이 구간({fmt(w.start)}~{fmt(w.end)})에서는 기준 대비 뚜렷한 이상이 보이지 않습니다. "
                   f"p95 {_ms(w.p95)}, 에러율 {w.error_rate:.1%}." if ko else
                   f"No clear anomaly vs baseline in {fmt(w.start)}-{fmt(w.end)}: p95 {_ms(w.p95)}, "
                   f"error rate {w.error_rate:.1%}.")
    new = [s for s in stats.signatures if s["new"]][:3]
    if new:
        out.append("\n새로 나타난 에러:" if ko else "\nNew error signatures:")
        for s in new:
            out.append(f"- {fmt(s['first_ts'])} {s['count']}x {s['sig'][:120]}")
    anchor = a["start"] if a else w.start
    before = [c for c in stats.changes if c["ts"] <= anchor + 120]
    if before:
        c = min(before, key=lambda c: anchor - c["ts"])
        ref = cite.get((c["source"], c["lineno"]), "")
        out.append(("\n가설: 이상 직전 " if ko else "\nHypothesis: right before the anomaly, ")
                   + f"{fmt(c['ts'])} '{c['msg'][:120]}' [{ref}] "
                   + ("이벤트가 원인일 가능성이 높습니다(신뢰도: 중간)." if ko else
                      "is the most likely trigger (confidence: medium)."))
    if evidence:
        out.append("\n근거:" if ko else "\nEvidence:")
        for e in evidence[:6]:
            out.append(f"[{e['cite']}] {e['source']}:{e['lineno']} {e['line'][:160]}")
    return "\n".join(out)
