// Build demo/out/intro.mp4: narration (macOS `say`) + Playwright screen recordings + burned-in captions, muxed with ffmpeg.
// v1 runs log-detective locally: real Pinecone retrieval + Gemini answers using direct keys from the auth file.
// Re-record on a Sprite with the Pinecone + Ollama Cloud connectors later.
//   usage: cd demo && npm i && node build.mjs        (env: VOICE=Samantha RATE=180 PORT=8799)
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

const DEMO = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(DEMO, "..");
const OUT = path.join(DEMO, "out");
const VOICE = process.env.VOICE || "Samantha";
const RATE = process.env.RATE || "180";
const PORT = Number(process.env.PORT || 8799);
const W = 1920, H = 1080, FPS = 30;
// SPRITE=<name> UI_URL=<sprite url>: record the live Sprite (connectors) instead of a local run
const SPRITE = process.env.SPRITE || "";
const UI_URL = (process.env.UI_URL || "").replace(/\/$/, "");
const BADGE = process.env.BADGE || (UI_URL ? `live on a Fly.io Sprite · ${UI_URL.replace("https://", "")}`
  : "recorded locally · real Pinecone + Gemini (direct keys)");
const ENV = { ...process.env, LOGDET_HOME: path.join(OUT, "home"), LOGDET_TZ: "Asia/Seoul",
  // real Pinecone (direct API key from ../auth.json, never printed); falls back to local if no key
  LOGDET_VECTOR: process.env.LOGDET_VECTOR || "auto", LOGDET_PINECONE_NAMESPACE: "demo-video",
  ...(process.env.LOGDET_AUTH_FILE ? { LOGDET_AUTH_FILE: process.env.LOGDET_AUTH_FILE } : {}),
  LOGDET_EMBED: "hash", LOGDET_LLM: process.env.LOGDET_LLM || "auto", LOGDET_DISCOVER: "0", PYTHONPATH: ROOT };

// caption line -> optional pronunciation override for TTS
const SCENES = [
  { id: "hook", type: "card", lines: [
    "It's 9 AM. Checkout was slow at 3 in the morning.",
    "The answer is buried somewhere in 30,000 log lines, across nginx, the app, and Postgres."] },
  { id: "title", type: "card", lines: [
    ["Meet log-detective: a personal SRE agent that lives on a Fly.io Sprite.",
     "Meet log detective: a personal S R E agent that lives on a Fly dot I O Sprite."],
    "You just ask your logs what happened."] },
  { id: "term", type: "term", badge: true, lines: SPRITE ? [
    "This is the agent, running on a Fly.io Sprite.",
    "Pinecone and Gemini are reached through Sprites connectors, so no API keys live on the box.",
    "Ask in plain English, and it answers in a few seconds."] : [
    "From the command line, ingest your logs.",
    "They're parsed, chunked into two-minute windows, and embedded into Pinecone.",
    "Then ask, in plain English."] },
  { id: "ui1", type: "ui", badge: true, q: "Why did checkout slow down at 3 AM yesterday?", lines: [
    "Same question in the web UI.",
    "It compares latency and errors against a baseline,",
    "pulls the matching log chunks from Pinecone, and asks Gemini for a root cause.",
    ["p95 latency jumped about 23 times, with up to 38% server errors.",
     "P 95 latency jumped about 23 times, with up to 38 percent server errors."],
    ["The trigger: a nightly report job that started at 2:57 and exhausted the shared database connection pool.",
     "The trigger: a nightly report job that started at 2 57, and exhausted the shared database connection pool."],
    "Every claim cites the exact log lines."] },
  { id: "ui2", type: "ui", badge: true, q: "What were the payment warnings around 1:30 AM yesterday?", lines: [
    "And it doesn't chase red herrings:",
    ["the payment warnings at 1:30 had no real impact.", "the payment warnings at one thirty had no real impact."]] },
  { id: "arch", type: "card", lines: [
    "Under the hood, raw logs stay on the Sprite.",
    "Pinecone handles time-filtered vector search, and Gemini writes the hypothesis.",
    "Both are reached through Sprites connectors, so no API keys live on the box."] },
  { id: "outro", type: "card", lines: [
    ["log-detective. Built for Fly.io Hacksgiving.", "log detective. Built for Fly dot I O Hacksgiving."],
    "The code is on GitHub."] },
];

const sh = (cmd, args, opts = {}) => {
  const r = spawnSync(cmd, args, { encoding: "utf8", ...opts });
  if (r.status !== 0) throw new Error(`${cmd} ${args.join(" ")}\n${r.stderr}`);
  return r.stdout;
};
const dur = (f) => parseFloat(sh("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const GAP = 0.35;

function narrate(scene) {
  const dir = path.join(OUT, "audio", scene.id);
  fs.mkdirSync(dir, { recursive: true });
  const sil = path.join(dir, "gap.wav");
  sh("ffmpeg", ["-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", String(GAP), sil]);
  const list = [], cues = [];
  let t = 0.4; // lead-in
  const lead = path.join(dir, "lead.wav");
  sh("ffmpeg", ["-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.4", lead]);
  list.push(lead);
  scene.lines.forEach((ln, i) => {
    const [text, spoken] = Array.isArray(ln) ? ln : [ln, ln];
    const aiff = path.join(dir, `${i}.aiff`), wav = path.join(dir, `${i}.wav`);
    sh("say", ["-v", VOICE, "-r", RATE, "-o", aiff, spoken]);
    sh("ffmpeg", ["-y", "-v", "error", "-i", aiff, "-ar", "48000", "-ac", "1", wav]);
    cues.push({ t, text });
    t += dur(wav) + GAP;
    list.push(wav, sil);
  });
  const listFile = path.join(dir, "list.txt");
  fs.writeFileSync(listFile, list.map((f) => `file '${f}'`).join("\n"));
  const wav = path.join(OUT, `${scene.id}.wav`);
  sh("ffmpeg", ["-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listFile, "-c", "copy", wav]);
  return { wav, cues, audio: t };
}

function captureTerminal() {
  if (SPRITE) {  // real output from the Sprite; mask connection ids
    const mask = (t) => t.replace(/(\/v1\/gateway\/[a-z_]+\/)[A-Za-z0-9_-]+/g, "$1conn_…").trim().split("\n");
    const run = (args) => mask(sh("sprite", ["exec", "-s", SPRITE, "--no-stdin", "--", "/home/sprite/.local/bin/logdet", ...args]));
    const doctor = run(["doctor"]).filter((l) => !/^(home|auth file|local store)/.test(l));
    const ask = run(["ask", "Why did checkout slow down at 3am yesterday?"]);
    return [
      { cmd: "logdet doctor", out: doctor, lineDelay: 55, pause: 600 },
      { cmd: 'logdet ask "Why did checkout slow down at 3am yesterday?"', out: ask, lineDelay: 22, pause: 300 },
    ];
  }
  const run = (args) => sh("python3", ["-m", "logdet.cli", ...args], { cwd: OUT, env: ENV });
  const demo = run(["demo", "--out", "data/sample"]).trim().split("\n");
  const ask = run(["ask", "Why did checkout slow down at 3am yesterday?"]).trim().split("\n");
  return [
    { cmd: "logdet demo", out: demo, lineDelay: 70, pause: 500 },
    { cmd: 'logdet ask "Why did checkout slow down at 3am yesterday?"', out: ask, lineDelay: 45, pause: 400 },
  ];
}

async function waitHttp(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try { if ((await fetch(url)).ok) return; } catch {}
    await sleep(250);
  }
  throw new Error("server did not start: " + url);
}

async function record(browser, scene, narr, termSteps) {
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1,
    recordVideo: { dir: path.join(OUT, "raw"), size: { width: W, height: H } },
    timezoneId: "Asia/Seoul", locale: "en-US", colorScheme: "dark" });
  await ctx.addInitScript({ path: path.join(DEMO, "captions.js") });
  const page = await ctx.newPage();
  const t0 = Date.now();
  if (scene.type === "ui") {
    await page.goto(UI_URL ? UI_URL + "/" : `http://127.0.0.1:${PORT}/`);
    await page.addStyleTag({ content: "main{zoom:1.5}" });
    await page.waitForSelector("#health:not(:empty)");
  } else {
    await page.goto(pathToFileURL(path.join(DEMO, "stage.html")).href + "#" + scene.id);
  }
  await sleep(300);
  const tReady = Date.now();
  if (scene.type === "term" && SPRITE) await page.evaluate(() => {
    document.querySelector(".term .bar").lastChild.textContent = " sprite console — log-detective";
    window.__prompt = "sprite@log-detective:~$";
  });
  await page.evaluate(([cues, end, badge]) => { window.__captions(cues, end); window.__badge(badge); },
    [narr.cues, narr.audio, scene.badge ? BADGE : ""]);

  let action = Promise.resolve();
  if (scene.type === "term") action = page.evaluate((steps) => window.playTerm(steps), termSteps);
  if (scene.type === "ui") action = (async () => {
    await sleep(500);
    await page.click("#q");
    await page.keyboard.type(scene.q, { delay: 40 });
    await sleep(350);
    await page.click("#b");
    await page.waitForSelector(".answer", { timeout: 90000 });
    // wait until the streamed answer is complete (meta line shows the model)
    await page.waitForFunction(() => (document.querySelector("#ansmeta, .answer + .meta")?.textContent || "").includes("llm:"),
      null, { timeout: 90000 });
    await sleep(scene.id === "ui1" ? 1800 : 500);
    await page.evaluate(() => window.scrollTo({ top: document.querySelector(".answer").getBoundingClientRect().top
      + window.scrollY - 140, behavior: "smooth" }));
    if (scene.id === "ui1") {
      await sleep(8500);
      const cite = await page.$('a.cite[data-e="E1"]') || await page.$("a.cite");
      if (cite) await cite.click();  // jumps to the cited evidence line and highlights it
      else await page.evaluate(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }));
    } else {
      await sleep(1800);
    }
  })();
  const minEnd = narr.audio + 0.6;
  await Promise.all([action, sleep(minEnd * 1000)]);
  await sleep(500);
  const total = (Date.now() - tReady) / 1000;
  await ctx.close();
  const video = await page.video().path();
  return { video, offset: (tReady - t0) / 1000, duration: Math.max(total, minEnd) };
}

function encode(scene, rec, wav) {
  const out = path.join(OUT, "seg", `${scene.id}.mp4`);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  const D = rec.duration.toFixed(2);
  sh("ffmpeg", ["-y", "-v", "error", "-ss", rec.offset.toFixed(2), "-i", rec.video, "-i", wav,
    "-t", D, "-map", "0:v", "-map", "1:a",
    "-vf", `fps=${FPS},scale=${W}:${H},format=yuv420p,fade=t=in:st=0:d=0.25,fade=t=out:st=${(rec.duration - 0.3).toFixed(2)}:d=0.3`,
    "-af", "apad", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", String(FPS),
    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", out]);
  return out;
}

async function main() {
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });
  console.log("1/5 sample data + terminal capture");
  const termSteps = captureTerminal();
  console.log("2/5 narration (" + VOICE + ")");
  const narr = Object.fromEntries(SCENES.map((s) => [s.id, narrate(s)]));
  console.log("3/5 web UI server");
  const srv = spawn("python3", ["-m", "logdet.cli", "serve", "--host", "127.0.0.1", "--port", String(PORT)],
    { cwd: OUT, env: ENV, stdio: "ignore" });
  try {
    await waitHttp(`http://127.0.0.1:${PORT}/api/health`);
    console.log("4/5 recording scenes");
    const browser = await chromium.launch();
    const segs = [];
    for (const s of SCENES) {
      const rec = await record(browser, s, narr[s.id], termSteps);
      segs.push(encode(s, rec, narr[s.id].wav));
      console.log(`   ${s.id}: ${rec.duration.toFixed(1)}s`);
    }
    await browser.close();
    console.log("5/5 concat");
    const list = path.join(OUT, "segments.txt");
    fs.writeFileSync(list, segs.map((f) => `file '${f}'`).join("\n"));
    const final = path.join(OUT, "intro.mp4");
    sh("ffmpeg", ["-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list, "-c", "copy", "-movflags", "+faststart", final]);
    console.log(`done: ${final} (${dur(final).toFixed(1)}s)`);
  } finally {
    srv.kill();
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
