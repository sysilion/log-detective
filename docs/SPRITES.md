# Sprites + connectors: research notes (2026-11-24)

Sources: https://fly.io/hacksgiving/, https://fly.io/sprites/ecosystem/,
https://docs.fly.io/sprites/concepts/connectors, https://docs.fly.io/sprites/quickstart,
https://docs.fly.io/sprites/cli/commands, https://docs.fly.io/sprites/concepts/services

## Hackathon rules (Hacksgiving 2026)
- Build "a personal agent that lives on a Sprite". It must **run on a Sprite** and **use at least one Sprites connector**.
- Runs Mon Nov 23 to Sun Nov 29 2026. Submissions close **before midnight PST, Nov 29**. Winners announced Dec 1 by email.
- Must be built during the week. Fly.io account required. Any AI tools allowed. One entry per developer. Follow the Fly.io ToS.
- Submit: post the agent with a short demo on X / Bluesky / LinkedIn / Reddit tagging **@flydotio**, then send the code link, the demo link, and 1–2 sentences through the Google Form:
  https://docs.google.com/forms/d/e/1FAIpQLSfZ_xearoDOSRXI3VjAbM4BJlObUdxSFO0SDwyZ8G6M2lapRQ/viewform
- Prizes: $2,500 grand, $1,500 second, $1,000 "Weird Uncle Award". No public judging rubric.

## What a Sprite is
- A hardware-isolated Linux VM with persistent disk: 8 vCPU, 100 GB, memory managed by the platform, **no GPU**.
- Sprites go warm (suspended, memory kept) and then cold (VM stopped). The disk persists. Processes come back only if they are registered as **services**.
- Every Sprite has a URL (`sprite url`). The URL is org-only by default. `sprite url update --auth public` makes it public.

## `sprite` CLI (installed locally at `~/.local/bin/sprite`, version 2026-11-03)
```sh
curl -fsSL https://sprites.dev/install.sh | sh     # install (macOS/Linux)
sprite login                                        # browser login with your Fly.io account
sprite create log-detective && sprite use log-detective
sprite exec -s log-detective -- <cmd>               # run a command (--stdin, --env K=V,..., --dir)
sprite console -s log-detective                     # interactive shell
sprite file push -s log-detective -r ./dir /path    # copy files in (file pull to copy out)
sprite url -s log-detective                         # show URL; `sprite url update --auth public`
sprite proxy 8080                                   # forward a port locally
```
Long-lived processes, run **inside** the Sprite:
```sh
sprite-env services create web --cmd python3 --args "-m,http.server,3000" --http-port 3000 [--env K=V,..] [--dir D]
sprite-env services list | get | restart | stop | start | delete <name>
tail -f /.sprite/logs/services/web.log
```
Only one service may own the HTTP port. A request to the Sprite URL starts that service if it is not running.

## Connectors (the key requirement)
A connector is **not** an MCP server and not an env var. It is an authenticated **gateway**:
1. You store a credential (OAuth or API key) for your Fly org in the dashboard. It is encrypted and the API never returns it.
2. An access policy chooses which Sprites may use it. It denies everything by default. Restrict by sprite name prefix and/or labels.
3. From inside an allowed Sprite, call
   `https://api.sprites.dev/v1/gateway/<provider>/<connection_id>/<upstream path>`
   **with no Authorization header**. The gateway identifies the Sprite by Fly request signing and injects the credential. Calls from outside a Sprite get 401.

Discovery from inside a Sprite: `curl -s https://api.sprites.dev/v1/gateway/list` lists connected providers with
`gateway_base_url` / `usage_snippet`. log-detective calls this automatically (`logdet doctor` shows the result).
The dashboard's connector detail page also has an "API Usage" snippet that contains the exact URL.

Setup (dashboard): **Fly.io dashboard → your org → Sprites → Connectors → Add connector** → enter the credential → test → Access:
turn off "Allow all sprites" and allow name prefix `log-detective`, or leave it on for the hackathon.

There are about 50 connectors (S3/Tigris, Neon, Supabase, Upstash, Hugging Face, OpenAI, Anthropic, OpenRouter, Gemini,
GitHub, Slack, Linear, Notion, Box, Pinecone, Ollama Cloud, TypeSafeAI, ...). Partners named on the hackathon page: Pinecone, Ollama, Box, TypeSafe AI, Fly.io.

### Pinecone connector (used: vector store)
- Credential: a Pinecone API key that can list and describe indexes and run vector (and inference) operations.
- Paths look like `/indexes/<index>/query` and `/indexes/<index>/vectors/upsert`. **The gateway resolves the index host for you**, so you never call `*.svc.pinecone.io` directly.
- Not documented: the provider slug in the URL (we auto-discover it), whether `POST /indexes` (create) and `/embed` (inference) pass through.
  → Create the index in the Pinecone console to be safe: **name `log-detective`, dimension 384, metric cosine, serverless**.
- We send `X-Pinecone-API-Version: 2025-04`.

### Ollama connector (used: LLM)
- **The only Ollama connector is "Ollama Cloud"** (key from https://ollama.com/settings/keys). It does *not* reach an Ollama server running on your laptop or in the Sprite.
- We call `<gateway>/api/chat` (Ollama's native API, `stream:false`). Default model `gemma4:31b` (the Ollama Cloud docs' example; direct/gateway calls use bare tags, no `-cloud` suffix). If the model is missing, we list `/api/tags` and pick a preferred one. Override with `LOGDET_MODEL`. Cloud usage is metered in credits on your Ollama account (see ollama.com/pricing).
- Privacy angle: raw logs and stats stay on the Sprite. Only the compact prompt (stats + about 14 cited lines) goes to Ollama Cloud.
  To keep everything on the box, run `ollama serve` inside the Sprite (CPU only, small model such as `qwen2.5:3b`) with `LOGDET_LLM=ollama`.

## Decisions
- **Python 3, standard library only** (urllib, sqlite3, http.server). Pinecone and Ollama are plain REST through the gateway, so we need no SDK, and with zero dependencies setup on the Sprite is just copying the code.
- Embeddings default to a local 384-d feature-hashing embedder (log text is lexical). This keeps it deterministic and offline-testable.
  Optional: `LOGDET_EMBED=pinecone` (Pinecone Inference `multilingual-e5-large`, 1024-d, needs a 1024-d index) or `LOGDET_EMBED=ollama`.
- Everything degrades gracefully: no Pinecone → local JSON vector store; no LLM → deterministic heuristic report.

### Google Gemini connector (used: default LLM, free tier)
- Fly ecosystem entry: "Use Gemini models with your Google API key held outside the Sprite." Credential: a Gemini API key from aistudio.google.com.
  Generation, streaming, token counting and model listing are forwarded. No Vertex AI. Bodies are capped at 8 MB.
- We call `<gateway>/v1beta/models/<model>:generateContent` with `systemInstruction`. Locally the direct API uses `x-goog-api-key`.
- Model `gemini-3.5-flash-lite`: stable, free tier, minimal thinking by default. Measured on the sample incident: **4.3–4.9 s per answer**, high-quality cited answers in Korean and English.
  We also tried `gemini-3.8-flash`: it returned 503 "high demand" after about 31 s (2026-11-24), so we retry once on the default model.
- Ollama Cloud returned **402 "Add usage credits"**, so it is not free. It stays as an optional backend (`LOGDET_LLM=ollama`).
