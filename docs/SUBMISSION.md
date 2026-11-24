# Hacksgiving submission kit

Google Form: https://docs.google.com/forms/d/e/1FAIpQLSfZ_xearoDOSRXI3VjAbM4BJlObUdxSFO0SDwyZ8G6M2lapRQ/viewform
Deadline: **before midnight PST, Sun Nov 29 2026** (= Mon Nov 30, 17:00 KST)

## Form: description (1–2 sentences)
log-detective is a personal SRE agent that lives on a Fly.io Sprite. Ask it "why did checkout slow down at 3am yesterday?" in English or Korean and it finds the latency/error spike against a baseline, pulls the relevant log chunks from Pinecone, and has an Ollama Cloud model write a root-cause hypothesis that cites the exact log lines, all through Sprites connectors, so no API keys live on the box.

## Form: links
- Code: https://github.com/sysilion-ib/log-detective
- Demo: <link to the X / Bluesky / LinkedIn / Reddit post with the video>

## Social post (X / Bluesky, short)
Built log-detective for #Hacksgiving: an agent living on a @flydotio Sprite that answers "why was checkout slow at 3am?" 🔎

It spots the spike, searches logs in Pinecone, and an Ollama model names the culprit with cited log lines. All via Sprites connectors, zero keys on the box.

github.com/sysilion-ib/log-detective

(X: @flydotio. On Bluesky/LinkedIn, check Fly.io's handle there before posting.)

## LinkedIn / Reddit (longer)
For Fly.io's Hacksgiving I built **log-detective**, a natural-language log investigation agent that runs on a Fly.io Sprite.

You ask "어제 새벽 3시에 왜 느려졌어?" / "Why did checkout slow down at 3am yesterday?". It parses the time range, compares p95 latency, 5xx rate and new error signatures against a baseline, retrieves the relevant log chunks from **Pinecone**, and asks an **Ollama** model to write a root-cause hypothesis with cited log lines. In the demo it traces a 23x latency spike to a nightly batch job that exhausted a shared DB connection pool, and it ignores the red herrings.

Both services are reached through **Sprites connectors**: the Sprite never holds an API key, raw logs stay on the box, and it's zero-dependency Python.
Code: https://github.com/sysilion-ib/log-detective @flydotio #Hacksgiving
