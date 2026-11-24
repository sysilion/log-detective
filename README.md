# 🔎 log-detective

**Ask your logs why things broke, in plain language.** A personal SRE agent that lives on a Fly.io Sprite.

```
$ logdet ask "어제 새벽 3시에 왜 느려졌어?"     # (abbreviated example output)
🔎 window: 2026-11-23 03:00  (02:30 → 04:00)
   vector=pinecone embed=hash llm=gemini(sprites-connector):gemini-3.5-flash-lite
   p95/min ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁████████████████████████████▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁

02:59~03:27 사이 p95 지연이 141ms → 3.2s(약 23배)로 치솟았고 5xx 비율은 최대 38%였습니다.
가설: 02:57 'scheduler: job nightly_sales_report started (parallelism=48, datasource=HikariPool-1 shared)' [E1]
이 공유 커넥션 풀(50개)을 점유해 checkout 요청이 커넥션을 3초간 기다리다 타임아웃 [E4] ...
```

Built for **Fly.io Hacksgiving 2026**.

**Live demo:** https://log-detective-cekg.sprites.app (a public Sprite; the demo clock is pinned so "yesterday 3am" means the sample incident).
**Intro video:** build it with `demo/build.mjs` (see [Demo video](#demo-video)). It uses Sprites connectors: **Pinecone** (vector search over log chunks) and **Google Gemini** (free-tier `gemini-3.5-flash-lite` writes the root-cause hypothesis). **Ollama Cloud** is supported as an alternative LLM.

> **Submission blurb:** log-detective is a log-investigation agent living on a Fly.io Sprite. Ask "why did checkout slow down at 3am yesterday?" in English or Korean and it computes latency/error spikes against a baseline, retrieves the relevant log chunks from Pinecone, and has an LLM (Gemini, or Ollama Cloud) write a root-cause hypothesis that cites the exact log lines, all through Sprites connectors, so no API keys live on the box.

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
 │   5. prompt (stats + evidence) ─► api.sprites.dev/v1/gateway/gemini/.. ─┼──► Gemini (or Ollama Cloud)
 │   6. answer with citations (heuristic fallback if no LLM)              │
 └────────────────────────────────────────────────────────────────────────┘
```

- **Connectors, no secrets:** on a Sprite the Pinecone, Gemini and Ollama endpoints are auto-discovered from `https://api.sprites.dev/v1/gateway/list`, and the gateway injects credentials. Nothing to put in `.env`.
- **Logs stay on the box:** raw events and stats live in local SQLite. The LLM sees only the compact stats plus about 14 cited lines.
- **Degrades gracefully:** Pinecone unreachable → local re-ranking of the window's chunks. Model missing → auto-picked from `/api/tags`. LLM down → deterministic heuristic report.
- **No dependencies:** Python 3.10+ standard library only. Works fully offline (local vector store + heuristic report) for development and tests.

## Quick start (local, offline)

```sh
git clone https://github.com/sysilion/log-detective && cd log-detective
pip install -e .            # or: alias logdet="python3 -m logdet.cli"
export LOGDET_TZ=Asia/Seoul # timezone used for "yesterday 3am"
logdet demo                 # generate the sample incident for *yesterday* and ingest it
logdet ask "어제 새벽 3시에 왜 느려졌어?" -e
logdet ask "Why did checkout slow down at 3am yesterday?"
logdet serve --port 8080    # web UI at http://localhost:8080
```

Use your own logs: `logdet ingest /var/log/nginx/access.log ./app.jsonl --reset`. The file name is the source id, so two files with the same name in different directories overwrite each other.

Local dev against the real services (optional): keys are read from an auth file (default `~/proj/auth.json`, override with
`LOGDET_AUTH_FILE`; format `{"pinecone": {"type": "api", "key": "..."}, "google": {...}, "ollama": {...}}`) or from the env vars
`PINECONE_API_KEY`, `GEMINI_API_KEY` and `OLLAMA_API_KEY`. Never commit that file.

## Deploy on a Sprite

1. One-time setup (owner):
   ```sh
   curl -fsSL https://sprites.dev/install.sh | sh   # installs ~/.local/bin/sprite
   sprite login
   ```
   In **Fly.io dashboard → Sprites → Connectors**:
   - **Pinecone:** add your Pinecone API key. In the Pinecone console create index `log-detective` (dimension **384**, cosine, serverless).
   - **Google Gemini:** add a Gemini API key from https://aistudio.google.com (free tier works).
   - **Ollama Cloud** (optional): add a key from https://ollama.com/settings/keys. It needs usage credits.
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
| `LOGDET_LLM` | `auto` | `auto` (Gemini → Ollama Cloud → local ollama → rule-based) / `gemini` / `ollama` / `ollama-local` / `none` |
| `LOGDET_GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini model (stable, free tier); 503/429 → one retry on the default |
| `LOGDET_GEMINI_GATEWAY` | auto-discovered | override the Gemini gateway base URL |
| `LOGDET_AUTH_FILE` | `~/proj/auth.json` | local key file (direct-API mode only) |
| `LOGDET_OLLAMA_GATEWAY` | auto-discovered | override the Ollama Cloud gateway base URL |
| `LOGDET_MODEL` | `gemma4:31b` (Ollama Cloud) / `qwen2.5:3b` (local) | model name; if missing, auto-picked from `/api/tags` |
| `LOGDET_WINDOW` | `120` | chunk window (seconds) |
| `LOGDET_RETENTION_DAYS` | `30` | prune data older than N days after each ingest (`0` = keep forever) |

## Continuous ingestion

All four modes share one incremental core. Per-source checkpoints live in `LOGDET_HOME/checkpoints.json`: for files, the inode, byte offset and line counter; for Elasticsearch, the last timestamp. Only new events are stored, embedded and upserted. A time window that is still open is re-upserted under the same chunk id until it closes, and retention pruning keeps running.

| mode | command | when to use |
|---|---|---|
| 1. incremental re-runs | `logdet ingest --follow-state /var/log/app/` | cron / Orca automation every N minutes; reads only lines appended since the last run |
| 2. tail | `logdet tail /var/log/nginx/access.log app.jsonl` | long-running Sprite service; polls and flushes every `LOGDET_FLUSH_SECONDS` (30) |
| 3. HTTP push | `POST /api/ingest?source=nginx` with `Authorization: Bearer $LOGDET_INGEST_TOKEN` | Fluent Bit, Vector, or `scripts/ship.py` pushing from other hosts |
| 4. Elasticsearch | `logdet pull-es [--loop --interval 60]` | your logs already live in ES / Elastic Cloud |

Rotation and truncation restart a file from byte 0, and a trailing partial line waits for the next read. In follow modes, line numbers are per-source sequence numbers, so citations stay unique across rotations.

```sh
# 2. run tail as a Sprite service (survives cold boots; see "Sleep and background loops")
sprite-env services create tail --cmd /home/sprite/.local/bin/logdet --args "tail,/var/log/app"
```

### HTTP push (`POST /api/ingest`)

The route is disabled unless `LOGDET_INGEST_TOKEN` is set, and **it stays disabled on the public demo Sprite**. The body can be NDJSON, a JSON array (Fluent Bit `format json`) or plain lines. Lines without a timestamp are stamped with the arrival time.

- **Idempotent.** Events are deduplicated by a hash of source, timestamp and raw line, and an optional `Idempotency-Key` header replays the stored response for 7 days. A retried batch is never double counted, in the stats store or in the vectors.
- **Atomic.** If the vector upsert fails, nothing from the batch is kept and the response is `503` + `Retry-After`.
- **Status codes.** `200` ok · `401` bad token · `411` no Content-Length · `413` body over `LOGDET_INGEST_MAX_BYTES` (1 MB) · `429` + `Retry-After` over `LOGDET_INGEST_PER_MINUTE` (120) · `503` + `Retry-After` when busy or a backend is down. **Senders should retry 5xx/429 and never other 4xx.**

A deeply cold Sprite can take a while to answer the first request after a long idle, so give senders a timeout of at least 30 s and a disk buffer:

```ini
# Fluent Bit (classic config)
[SERVICE]
    storage.path              /var/lib/fluent-bit/buffer
    storage.backlog.mem_limit 16M
[INPUT]
    Name          tail
    Path          /var/log/nginx/access.log
    storage.type  filesystem          # buffer on disk while the Sprite wakes
[OUTPUT]
    Name                 http
    Match                *
    Host                 log-detective-xxxx.sprites.app
    Port                 443
    tls                  On
    URI                  /api/ingest?source=nginx
    Format               json_lines
    json_date_key        date
    json_date_format     epoch
    Header               Authorization Bearer ${LOGDET_INGEST_TOKEN}
    Retry_Limit          no_limits    # retries 5xx/429 with backoff
    net.connect_timeout  30
    storage.total_limit_size 1G
```

```toml
# Vector
[sources.nginx]
type = "file"
include = ["/var/log/nginx/access.log"]

[sinks.logdet]
type = "http"
inputs = ["nginx"]
uri = "https://log-detective-xxxx.sprites.app/api/ingest?source=nginx"
method = "post"
encoding.codec = "json"
framing.method = "newline_delimited"
auth.strategy = "bearer"
auth.token = "${LOGDET_INGEST_TOKEN}"
batch.max_bytes = 900000               # stay under LOGDET_INGEST_MAX_BYTES
batch.timeout_secs = 10
request.timeout_secs = 60              # >= 30 s for cold wakes
request.retry_initial_backoff_secs = 2
request.retry_max_duration_secs = 120  # Vector retries 5xx/429, not other 4xx
buffer.type = "disk"
buffer.max_size = 268435488
buffer.when_full = "block"
```

There is also a minimal, dependency-free shipper. It uses exponential backoff, honours `Retry-After`, and sends an `Idempotency-Key` per batch:

```sh
LOGDET_INGEST_TOKEN=... python3 scripts/ship.py https://<sprite>.sprites.app nginx < access.log
tail -F app.log | LOGDET_INGEST_TOKEN=... python3 scripts/ship.py https://<sprite>.sprites.app app --batch 200
```

### Elasticsearch (`logdet pull-es`)

It pages with a point-in-time + `search_after` scan sorted by the timestamp field. Clusters without PIT fall back to timestamp-range pages. The checkpoint is the last timestamp, and documents fetched again at the boundary are dropped by the idempotent event hash, which includes the ES `_id`.

```sh
export LOGDET_ES_URL=https://my-deployment.es.us-east-1.aws.elastic.cloud
export LOGDET_ES_INDEX='logs-*' LOGDET_ES_TS_FIELD=@timestamp
export LOGDET_ES_API_KEY=...            # or LOGDET_ES_USER / LOGDET_ES_PASSWORD, or "elasticsearch" in the auth file
export LOGDET_ES_QUERY='service.name:checkout'                # Lucene string or query-DSL JSON
export LOGDET_ES_FIELDS='{"latency": "event.duration"}' LOGDET_ES_LATENCY_UNIT=ns   # ECS durations are ns
logdet pull-es            # one pass (cron-friendly), or --loop --interval 60
```

Default field names: `message|log|msg`, `log.level|level|severity`, `http.response.status_code|status`, `latency_ms|duration_ms|event.duration`, `service.name|service`, `url.path|path`.

## Data retention

Old data is pruned so the vector index and the local store don't grow forever:

- `LOGDET_RETENTION_DAYS` (default **30**, `0` = keep forever). Events, chunks and Pinecone vectors whose time window ended before `now - N days` are deleted. "now" honours `LOGDET_NOW`.
- Pruning runs automatically at the end of every `logdet ingest`. You can also run it by hand: `logdet prune --dry-run` counts what would go, `logdet prune [--days N]` deletes.
- Pinecone deletion queries the expired IDs with a metadata filter (`ts_end < cutoff`) and deletes them by ID in batches of 1,000. This works through the Sprites gateway and with a direct key. Delete-by-ID is supported on serverless indexes and gives exact counts for `--dry-run`.
- The public demo Sprite runs with `LOGDET_RETENTION_DAYS=0` and a pinned `LOGDET_NOW`, so its fixed sample dataset (442 vectors) is never pruned.

## Sample dataset

A pre-generated copy is bundled in `data/sample-2026-11-23/` (Asia/Seoul; ask with `--now 2026-11-24T09:00`). `logdet sample --date YYYY-MM-DD` (or `logdet demo` for yesterday) writes three files (nginx access log, checkout-service JSON lines, Postgres log). They cover six hours of traffic with an injected incident:
a `nightly_sales_report` batch job starts at 02:57 with parallelism 48 on the **shared** HikariCP pool (max 50). From 03:00 to 03:24 the pool is exhausted, so checkout/cart/login wait 3 s for a connection, p95 goes from about 140 ms to 3.2 s, and 503/504s appear. Postgres logs 40–60 s report queries.
There are red herrings too: a routine deploy at 01:10, slow payment-provider warnings at 01:30, and an hourly cron.

## Demo script (1–2 min video)

**Before recording:** run `SPRITE=log-detective ./scripts/sprite-deploy.sh` on the day of recording, so the sample incident is dated *yesterday*. Then warm it up once with `sprite exec -s log-detective -- ~/.local/bin/logdet ask "어제 새벽 3시에 왜 느려졌어?"`, which wakes the Sprite and the model. Open the Sprite URL in a browser. Use a large terminal font.

| time | screen | action / narration |
|---|---|---|
| 0:00–0:10 | browser, empty UI | "It's 9am. Checkout was slow at 3am. Instead of grepping three log files, I ask the agent that lives on my Sprite." |
| 0:10–0:25 | terminal | `sprite list` then `sprite exec -s log-detective -- ~/.local/bin/logdet doctor`. Point at `pinecone via gateway` and `gemini(sprites-connector)`: "two Sprites connectors, and no API keys on the box." |
| 0:25–0:35 | terminal | `sprite exec -s log-detective -- ~/.local/bin/logdet demo`. "30k log lines from nginx, the app and Postgres, chunked and embedded into Pinecone." |
| 0:35–1:05 | browser | Click **어제 새벽 3시에 왜 느려졌어?** Pause on the p95 chart (shaded anomaly), the tiles (141 ms → 3.2 s, 5xx 38%), then the answer naming the **02:57 nightly_sales_report job** exhausting the shared connection pool, with [E#] citations. Scroll the evidence list. |
| 1:05–1:20 | browser | Click **어제 새벽 1시 반쯤 결제 경고는 뭐였어?** It says there was no real impact, so it doesn't chase red herrings. |
| 1:20–1:30 | terminal | `sprite exec -s log-detective -- ~/.local/bin/logdet ask "Why did checkout slow down at 3am yesterday?"`: the same investigation in English, from the CLI. |
| 1:30–1:40 | README / repo | "log-detective on a Fly.io Sprite, Pinecone + Gemini via Sprites connectors. Code on GitHub. #Hacksgiving @flydotio" |

Post texts and the form description are in [docs/SUBMISSION.md](docs/SUBMISSION.md).

## Demo video

`demo/build.mjs` records a 1080p intro video (`demo/out/intro.mp4`, about 90 s). It narrates with macOS `say`, captures scenes with Playwright, burns in captions, and muxes with ffmpeg:
```sh
cd demo && npm i
SPRITE=log-detective UI_URL=https://log-detective-cekg.sprites.app node build.mjs   # record the live Sprite
node build.mjs                                                                        # or a local run
```

## Public demo safeguards

The public URL serves only `GET /`, `GET /api/health`, `POST /api/ask` and `POST /api/ask/stream`. The stream route returns NDJSON: `meta` with stats and evidence first, then answer `delta`s, then `done`. There is no ingest or admin endpoint over HTTP.
`/api/ask` is capped per visitor (`LOGDET_ASK_PER_IP_PER_HOUR`, default 20), has a global daily budget (`LOGDET_ASK_DAILY_BUDGET`, default 300) and caches identical questions for 15 minutes, so free-tier quotas can't be drained. Upstream error details are never returned (only `llm_fallback` / `retrieval_fallback` flags), degraded answers are not cached, and malformed bodies get a 400.

## Development

```sh
uv run --with pytest pytest -q    # 55 tests: parsers, ko/en time parsing, offline e2e, fake-gateway connector, streaming and fallback tests, HTTP endpoint tests
```

Notes on Sprites and the connector research are in [docs/SPRITES.md](docs/SPRITES.md).

MIT license.
