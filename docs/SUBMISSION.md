# Hacksgiving submission kit

Google Form: https://docs.google.com/forms/d/e/1FAIpQLSfZ_xearoDOSRXI3VjAbM4BJlObUdxSFO0SDwyZ8G6M2lapRQ/viewform
Deadline: **before midnight PST, Sun Nov 29 2026** (= Mon Nov 30, 17:00 KST)

## Form: description (1–2 sentences)
log-detective is a personal SRE agent on a Fly.io Sprite that watches your logs and pings Slack or Discord before you ask, and answers questions like "why did checkout slow down at 3am yesterday?" in English or Korean. It compares latency and errors against a baseline, retrieves the relevant log chunks from Pinecone and has Gemini write a root-cause hypothesis citing the exact log lines, all through Sprites connectors, so no API keys live on the box.

## Form: links
- Code: https://github.com/sysilion/log-detective
- Live demo: https://log-detective-cekg.sprites.app
- Demo video: <link to the X / Bluesky / LinkedIn / Reddit post with demo/out/intro.mp4>

## Social post (X / Bluesky, short)
Built log-detective for #Hacksgiving: an SRE agent on a @flydotio Sprite 🔎

It watches my logs, pings Slack/Discord before I ask, and answers "why was checkout slow at 3am?" with cited log lines. Pinecone + Gemini via Sprites connectors.

github.com/sysilion/log-detective

(X: @flydotio. On Bluesky/LinkedIn, check Fly.io's handle there before posting.)

## LinkedIn / Reddit (longer)
For Fly.io's Hacksgiving I built **log-detective**, a natural-language log investigation agent that runs on a Fly.io Sprite.

You ask "어제 새벽 3시에 왜 느려졌어?" / "Why did checkout slow down at 3am yesterday?". It parses the time range, compares p95 latency, 5xx rate and new error signatures against a baseline, retrieves the relevant log chunks from **Pinecone**, and asks **Gemini** (or Ollama Cloud) to write a root-cause hypothesis with cited log lines. In the demo it traces a 23x latency spike to a nightly batch job that exhausted a shared DB connection pool, and it ignores the red herrings.

It also **watches** your logs: scheduled checks run cheap statistics first and call the LLM only for a new incident, then alert Slack or Discord (deduplicated). Logs stream in by tailing files, HTTP push (Fluent Bit / Vector), or pulling from Elasticsearch.

Pinecone and Gemini are reached through **Sprites connectors**: the Sprite never holds an API key, raw logs stay on the box, and it's zero-dependency Python.
Code: https://github.com/sysilion/log-detective @flydotio #Hacksgiving
