"""Ingest + ask orchestration: parse -> chunk -> embed -> vector store; question -> stats + retrieval -> LLM."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .analyze import Stats, analyze
from .config import Config, parse_now
from .embed import get_embedder
from .llm import get_llm
from .parse import parse_lines
from .store import Store, build_chunks
from .timeparse import TimeRange, parse_time_range
from .vector import get_vector_store

LOG_SUFFIXES = {".log", ".jsonl", ".json", ".txt", ".ndjson", ""}
_CITE_LINE_RE = re.compile(r"^\[([^\]:]+):(\d+)\] (.*)$")
_INTERESTING_RE = re.compile(r"ERROR|FATAL|WARN|timeout|timed out|exhaust|refused|duration: \d{4,}|\" 5\d\d ", re.I)
RULE_BASED = "rule-based (no LLM)"
# models sometimes cite prompt section names ("[change events]") instead of E-numbers; drop those
_BAD_CITE_RE = re.compile(r"\s*\[(?:change[ _-]?events?|evidence(?: log lines)?|anomaly|baseline|window)\]", re.I)


def clean_answer(text: str) -> str:
    return _BAD_CITE_RE.sub("", text).strip()


@dataclass
class Investigation:
    """Everything computed locally for one question, before the LLM is involved."""
    question: str
    tr: TimeRange
    stats: Stats
    evidence: list[dict]
    hits: list[dict]
    retrieval_error: str | None
    t0: float


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
        if self.vs.name == "pinecone" and total_ch:
            try:
                log(f"pinecone namespace '{self.cfg.pinecone_namespace}': {self.vs.wait_until(total_ch)} vectors")
            except Exception as e:  # stats are informational only
                log(f"pinecone stats unavailable: {str(e)[:120]}")
        return {"events": total_ev, "chunks": total_ch, "vector": self.vs.name, "embed": self.emb.name,
                "seconds": round(time.time() - t0, 2)}

    # ------------------------------------------------------------ ask
    def prepare(self, question: str, now: datetime | None = None) -> Investigation:
        """Phase 1 (local, fast): time range, baseline stats, retrieval and evidence. Touches SQLite."""
        t0 = time.time()
        now = now or parse_now(self.cfg.now_override, self.cfg.tz) or datetime.now(self.cfg.tz)
        tr = parse_time_range(question, now, self.cfg.tz, self.store.time_range())
        stats = analyze(self.store, tr.start_ts, tr.end_ts)

        query = question + "\n" + "\n".join(s["sig"] for s in stats.signatures[:5])
        lo, hi = tr.start_ts - 1800, tr.end_ts
        retrieval_error = None
        try:
            qv = self.emb.embed([query], "query")[0]
            hits = self.vs.query(qv, top_k=8, start=lo, end=hi)
        except Exception as e:  # embedder or vector store unreachable: rank this window's chunks locally
            retrieval_error = f"{self.vs.name}/{self.emb.name}: {str(e)[:200]}"
            hits = local_rerank(self.store.chunks_between(lo, hi), query, top_k=8)
        return Investigation(question, tr, stats, self._evidence(stats, hits), hits, retrieval_error, t0)

    def _llm(self, use_llm: bool):
        try:
            return (get_llm(self.cfg) if use_llm else None), None
        except Exception as e:  # misconfigured backend: still answer with the rule-based report
            return None, str(e)[:300]

    def answer(self, inv: Investigation, use_llm: bool = True) -> dict:
        """Phase 2 (network, slow): ask the LLM; fall back to the rule-based report. No SQLite access."""
        llm, err = self._llm(use_llm)
        if llm is not None:
            try:
                text = llm.chat(SYSTEM_PROMPT, self._prompt(inv))
                if text:
                    return self.result(inv, text, llm.name, None)
            except Exception as e:  # keep the demo alive if the model is unreachable
                err = str(e)[:300]
        return self.result(inv, heuristic_report(inv.question, inv.stats, inv.evidence, self.cfg.tz),
                           RULE_BASED, err)

    def answer_stream(self, inv: Investigation, use_llm: bool = True) -> Iterator[tuple[str, object]]:
        """Like answer(), but yields ("delta", text) chunks as the LLM streams, then ("done", result)."""
        llm, err = self._llm(use_llm)
        parts: list[str] = []
        if llm is not None:
            try:
                for chunk in llm.stream(SYSTEM_PROMPT, self._prompt(inv)):
                    parts.append(chunk)
                    yield "delta", chunk
            except Exception as e:
                err = str(e)[:300]
            if parts:  # a partial answer is still better than switching to a different text mid-way
                yield "done", self.result(inv, "".join(parts).strip(), llm.name, err)
                return
        text = heuristic_report(inv.question, inv.stats, inv.evidence, self.cfg.tz)
        yield "delta", text
        yield "done", self.result(inv, text, RULE_BASED, err)

    def result(self, inv: Investigation, answer: str, llm_name: str, llm_error: str | None) -> dict:
        tr = inv.tr
        return {
            "question": inv.question,
            "range": {"start": tr.start.isoformat(), "end": tr.end.isoformat(), "label": tr.label,
                      "explicit": tr.explicit},
            "answer": clean_answer(answer), "llm": llm_name, "llm_error": llm_error, "retrieval_error": inv.retrieval_error,
            "vector": self.vs.name, "embed": self.emb.name,
            "stats": inv.stats.to_dict(), "evidence": inv.evidence,
            "retrieved": [{"id": h["id"], "score": round(h["score"], 3), "source": h["metadata"].get("source"),
                           "ts_start": h["metadata"].get("ts_start")} for h in inv.hits],
            "elapsed_ms": int((time.time() - inv.t0) * 1000),
        }

    def ask(self, question: str, now: datetime | None = None, use_llm: bool = True) -> dict:
        """One-shot investigation: prepare() + answer()."""
        return self.answer(self.prepare(question, now), use_llm)

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
                if _INTERESTING_RE.search(m.group(3)):
                    r = db.execute("SELECT ts, raw FROM events WHERE source=? AND lineno=?",
                                   (m.group(1), int(m.group(2)))).fetchone()
                    add(m.group(1), m.group(2), r["ts"] if r else md.get("ts_start"),
                        r["raw"].splitlines()[0] if r else m.group(3), f"retrieved chunk (score {h['score']:.2f})")
                    picked += 1
        ev.sort(key=lambda e: e["ts"] or 0)
        for i, e in enumerate(ev, 1):
            e["cite"] = f"E{i}"
        return ev

    def _prompt(self, inv: Investigation) -> str:
        question, tr, stats, evidence = inv.question, inv.tr, inv.stats, inv.evidence
        tz = self.cfg.tz
        fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S") if ts else "?"  # noqa: E731
        w, b = stats.window, stats.baseline
        lang = "Korean" if re.search(r"[가-힣]", question) else "English"
        lines = [f"QUESTION: {question}", f"ANSWER LANGUAGE: {lang}",
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
        if stats.changes:
            lines.append("CHANGE EVENTS (deploys/jobs/restarts, from 30 min before the window):")
            for c in stats.changes[:8]:
                lines.append(f"- {fmt(c['ts'])} {c['source']}: {c['msg'][:160]}")
        lines.append("EVIDENCE LOG LINES (cite as [E#]):")
        for e in evidence:
            lines.append(f"[{e['cite']}] {e['source']}:{e['lineno']} {e['line']}")
        return "\n".join(lines)


def local_rerank(chunks, query: str, top_k: int = 8) -> list[dict]:
    """Fallback retrieval: hash-embed the window's chunks on the fly and rank by cosine."""
    from .embed import HashEmbedder
    if not chunks:
        return []
    emb = HashEmbedder()
    q = emb.embed([query])[0]
    vecs = emb.embed([c.text for c in chunks])
    scored = sorted(((sum(a * b for a, b in zip(q, v)), c) for v, c in zip(vecs, chunks)), key=lambda x: -x[0])
    return [{"id": c.id, "score": sc, "metadata": {"source": c.source, "ts_start": c.ts_start, "ts_end": c.ts_end,
                                                   "n_errors": c.n_errors, "text": c.text}} for sc, c in scored[:top_k]]


SYSTEM_PROMPT = """You are Log Detective, a senior SRE investigating an incident from log evidence.
Write the whole answer in the language given by ANSWER LANGUAGE (natural, fluent prose).
Use ONLY the statistics, change events and evidence provided; never invent log lines, numbers or services.
Cite evidence inline as [E1], [E2] using only the E-numbers listed under EVIDENCE (never cite section names). Every claim about the cause must cite at least one line.
Reasoning rules:
- Correlate: the trigger usually starts shortly BEFORE the first anomalous minute, and recovery follows when it ends.
- Prefer a change event whose timing matches both onset and recovery; explicitly dismiss changes that do not (red herrings).
- If ANOMALY is "none detected", say so plainly and do not force a root cause.
Format (markdown, under 220 words), with these four bold headings translated into the answer language:
**Summary** - one sentence: what happened, when, impact with numbers.
**Timeline** - 3-6 bullets "HH:MM - event [E#]".
**Root cause** - hypothesis, mechanism, confidence (high/medium/low).
**Next steps** - 2-3 concrete bullets to confirm or fix.
For Korean use the headings 요약 / 타임라인 / 원인 가설 / 다음 조치."""


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
