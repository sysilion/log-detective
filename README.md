# 🔎 log-detective

**Ask your logs why things broke, in plain language.** A personal SRE agent that lives on a Fly.io Sprite.

```
$ logdet ask "어제 새벽 3시에 왜 느려졌어?"     # (abbreviated example output)
🔎 window: 2026-11-23 03:00  (02:30 → 04:00)
   vector=pinecone embed=hash llm=ollama-cloud(sprites-connector):gpt-oss:20b
   p95/min ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁████████████████████████████▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁

02:59~03:27 사이 p95 지연이 141ms → 3.2s(약 23배)로 치솟았고 5xx 비율은 최대 38%였습니다.
가설: 02:57 'scheduler: job nightly_sales_report started (parallelism=48, datasource=HikariPool-1 shared)' [E1]
이 공유 커넥션 풀(50개)을 점유해 checkout 요청이 커넥션을 3초간 기다리다 타임아웃 [E4] ...
```

Built for **Fly.io Hacksgiving 2026**. It uses two Sprites connectors: **Pinecone** (vector search over log chunks) and **Ollama Cloud** (the LLM that writes the root-cause hypothesis).

> **Submission blurb:** log-detective is a log-investigation agent living on a Fly.io Sprite. Ask "why did checkout slow down at 3am yesterday?" in English or Korean and it computes latency/error spikes against a baseline, retrieves the relevant log chunks from Pinecone, and has an Ollama model write a root-cause hypothesis that cites the exact log lines, all through Sprites connectors, so no API keys live on the box.

## How it works

```
 log files (nginx / JSON lines / plain text)
        │  logdet ingest
        ▼
 ┌─────────────── Sprite (Fly.io microVM) ────────────────────────────────┐
 │ parse ─► SQLite (raw events, stays local) ─► 2-min chunks per source   │
 │                                      │ embed (384-d hashing / Pinecone │
 │                                      ▼  Inference / Ollama)            │
 │                     api.sprites.dev/v1/gateway/pinecone/<conn>/...  ───┼──► Pinecone index
 │                                                                        │    (vectors + chunk text,
 │ logdet ask "어제 새벽 3시에 왜 느려졌어?"  /  web UI (:8080, Sprite URL) │     ts_start/ts_end metadata)
 │   1. time-range parser (ko/en: 어제 새벽 3시, 3am yesterday, 지난 2시간)  │
 │   2. stats vs baseline: p50/p95/p99, 5xx rate, per-minute anomaly,     │
 │      NEW error signatures, slow endpoints, change events (deploy/job)  │
 │   3. vector search with time-overlap filter  ◄─────────────────────────┼──► Pinecone query
 │   4. evidence lines [E1..E14] (first occurrences, changes, slow ops)   │
 │   5. prompt (stats + evidence) ─► api.sprites.dev/v1/gateway/ollama/.. ─┼──► Ollama Cloud /api/chat
 │   6. answer with citations (heuristic fallback if no LLM)              │
 └────────────────────────────────────────────────────────────────────────┘
```

- **Connectors, no secrets:** on a Sprite the Pinecone and Ollama endpoints are auto-discovered from `https://api.sprites.dev/v1/gateway/list`, and the gateway injects credentials. Nothing to put in `.env`.
- **Logs stay on the box:** raw events and stats live in local SQLite. The LLM sees only the compact stats plus about 14 cited lines.
- **No dependencies:** Python 3.10+ standard library only. Works fully offline (local vector store + heuristic report) for development and tests.

## Quick start (local, offline)

```sh
git clone https://github.com/sysilion-ib/log-detective && cd log-detective
pip install -e .            # or: alias logdet="python3 -m logdet.cli"
export LOGDET_TZ=Asia/Seoul # timezone used for "yesterday 3am"
logdet demo                 # generate the sample incident for *yesterday* and ingest it
logdet ask "어제 새벽 3시에 왜 느려졌어?" -e
logdet ask "Why did checkout slow down at 3am yesterday?"
logdet serve --port 8080    # web UI at http://localhost:8080
```

Use your own logs: `logdet ingest /var/log/nginx/access.log ./app.jsonl --reset`.

Local dev against the real services (optional): `PINECONE_API_KEY=...` (direct Pinecone, index auto-created) and
`OLLAMA_API_KEY=...` (Ollama Cloud), or a local `ollama serve` (auto-detected; `LOGDET_MODEL=qwen2.5:3b`).

## Deploy on a Sprite

1. One-time setup (owner):
   ```sh
   curl -fsSL https://sprites.dev/install.sh | sh   # installs ~/.local/bin/sprite
   sprite login
   ```
   In **Fly.io dashboard → Sprites → Connectors**:
   - **Pinecone:** add your Pinecone API key. In the Pinecone console create index `log-detective` (dimension **384**, cosine, serverless).
   - **Ollama Cloud:** add a key from https://ollama.com/settings/keys.
   - Access policy: allow sprites named `log-detective`, or allow all.
2. Deploy:
   ```sh
   SPRITE=log-detective ./scripts/sprite-deploy.sh   # uploads code, ingests sample data, starts the web service
   sprite url update -s log-detective --auth public  # optional: public demo URL
   sprite exec -s log-detective -- ~/.local/bin/logdet ask "어제 새벽 3시에 왜 느려졌어?"
   ```
3. `sprite exec -s log-detective -- ~/.local/bin/logdet doctor` shows which connectors were discovered.

## Configuration

| env | default | meaning |
|---|---|---|
| `LOGDET_TZ` | system tz | timezone for parsing questions / naive timestamps |
| `LOGDET_HOME` | `.logdet` | local SQLite + local vector file |
| `LOGDET_VECTOR` | `auto` | `pinecone` (connector or `PINECONE_API_KEY`) / `local` |
| `LOGDET_PINECONE_INDEX` / `_NAMESPACE` | `log-detective` / `logs` | Pinecone index and namespace |
| `LOGDET_PINECONE_GATEWAY` | auto-discovered | override, e.g. `https://api.sprites.dev/v1/gateway/pinecone/conn_xxx` |
| `LOGDET_EMBED` | `hash` | `hash` (384-d, local) / `pinecone` (Inference, 1024-d) / `ollama` |
| `LOGDET_LLM` | `auto` | `auto` (Ollama connector → `OLLAMA_API_KEY` → local ollama → heuristic) / `ollama` / `ollama-cloud` / `none` |
| `LOGDET_OLLAMA_GATEWAY` | auto-discovered | override the Ollama Cloud gateway base URL |
| `LOGDET_MODEL` | `gpt-oss:20b` (cloud) / `qwen2.5:3b` (local) | model name |
| `LOGDET_WINDOW` | `120` | chunk window (seconds) |

## Sample dataset

A pre-generated copy is bundled in `data/sample-2026-11-23/` (Asia/Seoul; ask with `--now 2026-11-24T09:00`). `logdet sample --date YYYY-MM-DD` (or `logdet demo` for yesterday) writes three files (nginx access log, checkout-service JSON lines, Postgres log). They cover six hours of traffic with an injected incident:
a `nightly_sales_report` batch job starts at 02:57 with parallelism 48 on the **shared** HikariCP pool (max 50). From 03:00 to 03:24 the pool is exhausted, so checkout/cart/login wait 3 s for a connection, p95 goes from about 140 ms to 3.2 s, and 503/504s appear. Postgres logs 40–60 s report queries.
There are red herrings too: a routine deploy at 01:10, slow payment-provider warnings at 01:30, and an hourly cron.

## Demo script (1–2 min video)

1. **(0:00) Hook.** Terminal + browser side by side. Say: "It's 9am, checkout was slow at 3am. Instead of grepping three log files, I ask my Sprite."
2. **(0:10) It lives on a Sprite.** Run `sprite list`, then `sprite exec -s log-detective -- ~/.local/bin/logdet doctor`. Point at `pinecone via gateway` and `ollama-cloud(sprites-connector)`, and note that no API keys are on the box.
3. **(0:25) Ingest.** Run `sprite exec -s log-detective -- ~/.local/bin/logdet demo`: 30k lines → 442 chunks → Pinecone.
4. **(0:40) Ask in Korean.** In the web UI (Sprite URL) click "어제 새벽 3시에 왜 느려졌어?". Show the p95 chart with the shaded anomaly, the tiles (141 ms → 3.2 s, 5xx 38%), and the answer that names the 02:57 batch job with [E#] citations.
5. **(1:10) Ask in English, different angle.** "What happened yesterday?" or "어제 새벽 1시 반쯤 결제 경고는 뭐였어?" It correctly says there was no major impact (red herring).
6. **(1:30) Close.** "Pinecone and Ollama through Sprites connectors, logs stay on the box. Code: github.com/sysilion-ib/log-detective #Hacksgiving @flydotio"

## Development

```sh
uv run --with pytest pytest -q    # 21 tests: parsers, ko/en time parsing, offline e2e, fake-gateway connector wire tests
```

Notes on Sprites and the connector research are in [docs/SPRITES.md](docs/SPRITES.md).

MIT license.
