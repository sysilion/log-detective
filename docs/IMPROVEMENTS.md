# Self-review and improvement plan (2026-11-24)

Scope: `logdet/` (about 1,900 lines, stdlib only), the public web endpoint on the Sprite, and the tests (30 at the start, 55 after).
Findings are ranked by impact on the public demo, then on correctness, then on maintainability.
Status: ✅ done in this pass · ⏭️ deliberately not done (reason given).

## Robustness and correctness
1. ✅ **The web server serializes every request behind one global lock, including the 3–4 s LLM call.** Two visitors asking at once wait for each other.
   Fix: hold the lock only for the SQLite/stats/retrieval phase and call the LLM outside it.
2. ✅ **Degraded answers get cached.** If Gemini returns a transient 503, the rule-based fallback is cached for 15 minutes, so every later visitor gets the degraded answer.
   Fix: cache only answers without `llm_error`/`retrieval_error`.
3. ✅ **A query-embedding failure crashes `ask`.** `emb.embed()` runs outside the retrieval `try`. With `LOGDET_EMBED=pinecone` and a hiccup, the request returns 500.
   Fix: treat it like a vector-store failure and fall back to local re-ranking.
4. ✅ **Time parsing misreads "3시간 전" ("3 hours ago") as "3 o'clock".** "N분/시간 전" and "N minutes/hours ago" were not supported at all.
   Fix: add a negative lookahead and an "ago" rule.
5. ✅ **Evidence lines from retrieved chunks carry the chunk's start time, not the line's own time.** This makes the evidence order and the timestamps shown to the LLM slightly wrong.
   Fix: look up the real event (ts, raw) by `source:lineno`.
6. ✅ **The baseline rate is skewed when the "outside the window" fallback is used.** The baseline length ignored the part after the window.
   Fix: compute the covered span correctly.

## Public endpoint security (quota abuse, info leaks)
7. ✅ **Raw upstream error text** (`llm_error`, `retrieval_error`) is returned to anonymous visitors. It holds no secrets, but it does reveal provider URLs and messages.
   Fix: the public API returns booleans/short codes. Full details go to the service log.
8. ✅ **Malformed JSON returns 500.** It should be 400. There was also no socket timeout, so slow clients could hold threads open.
   Fix: return 400 for non-JSON, non-object, empty or oversized (>4 KB) bodies. Also set a 15 s handler timeout and `nosniff`.
9. ✅ **The per-IP limiter trusts the left-most `X-Forwarded-For` entry**, which the client controls. Its IP table also grows without bound.
   Fix: prefer `Fly-Client-IP`, else use the right-most XFF hop. Prune idle IPs.
   ⏭️ We can't fully verify which header the Sprites proxy overwrites. The global daily budget (300) remains the hard cap.
10. ⏭️ **Prompt injection through log lines.** Logs are untrusted text that we pass to the LLM. The model has no tools and the output is rendered escaped (no HTML), so the impact is limited to a misleading answer. We documented it rather than adding filtering.

## Code quality
11. ✅ Duplicated constants (`PINECONE_API`, `PINECONE_VERSION` in two modules) and duplicated "parse `now`" logic (CLI, agent). Moved them into one place.
12. ✅ Dead code: `Store.chunks_by_ids`, `Store.all_chunks`. Removed.
13. ✅ `ask()` mixed three phases in one long function. Split it into `prepare()` (stats + retrieval + evidence), `answer()`/`answer_stream()` (LLM) and `result()`. This also enables streaming (item 15).
14. ⏭️ Full type-hint coverage and a `mypy` gate. We added hints and docstrings to the new and changed public functions. A strict type pass across sqlite `Row` objects isn't worth it before the deadline.

## Demo-visible wins
15. ✅ **Progressive answer.** `POST /api/ask/stream` returns NDJSON: stats, chart and evidence arrive after about 1 s, then the answer streams in token chunks (Gemini `streamGenerateContent` through the Sprites gateway). The UI renders each part as it arrives.
16. ✅ **Clickable citations.** Clicking `[E3]` in the answer scrolls to and highlights the evidence line. Evidence rows show local time and source.
17. ⏭️ A separate timeline widget. The LLM answer already contains a cited timeline, and the per-minute chart covers the visual part.

19. ✅ **Pseudo-citations.** Gemini occasionally cites prompt section names ("[change events]") instead of E-numbers. These are stripped from the final answer.

## Tests
18. ✅ Added tests for: HTTP handler behaviour (400 / 404 / 429 / spoofed XFF / caching / degraded answers not cached / no error leakage / streaming framing), relative time phrases, synthetic baseline/anomaly stats, query-embedding failure fallback, evidence timestamps, Gemini SSE and Ollama NDJSON streaming, rule-based stream fallback. **30 → 55 tests.**

## Not changed
- File names are used as the source id, so two files with the same name in different directories overwrite each other. This is acceptable for the demo, and the README now says it.
- The hash embedder stays the default. It is deterministic, offline-testable, and its dimension matches the existing 384-d index. Pinecone Inference embeddings remain opt-in.

## Measured before/after (public URL, Sprite + Pinecone/Gemini connectors, uncached questions)
| | before | after |
|---|---|---|
| first content on screen (stats, chart, evidence) | 3.4–4.3 s (everything arrives at the end) | **1.35–2.3 s** |
| first answer token | n/a (no streaming) | 2.7–3.6 s |
| full answer | 3.4–4.3 s | 3.3–4.9 s (unchanged; Gemini variance) |
| server-side `elapsed_ms` | 3.0–3.5 s | 2.8–4.1 s |

Total time is dominated by Gemini generation, so it did not change. What changed is perceived latency: the page now shows the numbers and evidence about 2 s earlier and streams the answer.
