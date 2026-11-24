"""`logdet` command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

from .config import Config, in_sprite, parse_now


def _now(cfg: Config, s: str | None) -> datetime | None:
    return parse_now(s, cfg.tz)


def cmd_ingest(args, cfg):
    from .agent import Detective
    if args.follow_state:
        from .ingest import follow_files
        det = Detective(cfg)
        res = follow_files(det, args.paths)
        pruned = det.prune() if cfg.retention_days else None
        print(f"incremental: +{res['events']} new events from {res['files']} file(s), "
              f"{res['chunks']} windows upserted" + (f"; pruned {pruned['events']} old events" if pruned and
                                                       pruned["events"] else ""))
        return
    print(f"ingesting {', '.join(args.paths)} ...")
    res = Detective(cfg).ingest(args.paths, reset=args.reset)
    print(f"done: {res['events']} events, {res['chunks']} chunks -> vector={res['vector']} "
          f"embed={res['embed']} ({res['seconds']}s)")


def cmd_ask(args, cfg):
    from .agent import Detective
    res = Detective(cfg).ask(args.question, now=_now(cfg, args.now), use_llm=not args.no_llm)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return
    st = res["stats"]
    print(f"\n🔎 window: {res['range']['label']}  ({res['range']['start']} → {res['range']['end']})")
    print(f"   vector={res['vector']} embed={res['embed']} llm={res['llm']} · {res['elapsed_ms']} ms")
    if res.get("llm_error"):
        print(f"   (LLM failed, used heuristic: {res['llm_error']})")
    if st["timeline"]:
        print("   p95/min " + _spark([m["p95"] or 0 for m in st["timeline"]]))
    print("\n" + res["answer"] + "\n")
    if args.evidence:
        print("evidence:")
        for e in res["evidence"]:
            print(f"  [{e['cite']}] {e['source']}:{e['lineno']} ({e['why']})\n       {e['line'][:200]}")


def _spark(vals: list[float], width: int = 60) -> str:
    if not vals:
        return ""
    step = max(1, len(vals) // width)
    vals = [max(vals[i:i + step]) for i in range(0, len(vals), step)]
    hi = max(vals) or 1
    return "".join("▁▂▃▄▅▆▇█"[min(7, int(v / hi * 7.99))] for v in vals)


def cmd_sample(args, cfg):
    from .sample import generate
    base = _now(cfg, cfg.now_override) or datetime.now(cfg.tz)
    date = datetime.fromisoformat(args.date) if args.date else base - timedelta(days=1)
    files = generate(args.out, date, cfg.tz, seed=args.seed)
    print(f"sample incident dataset for {date:%Y-%m-%d} ({cfg.tz.key}):")
    for f in files:
        print(f"  {f}")


def cmd_demo(args, cfg):
    args.date, args.seed = getattr(args, "date", None), 7
    cmd_sample(args, cfg)
    args.paths, args.reset = [args.out], True
    cmd_ingest(args, cfg)
    print('\nnow try:  logdet ask "어제 새벽 3시에 왜 느려졌어?"')


def cmd_tail(args, cfg):
    from .agent import Detective
    from .ingest import tail
    print(f"tailing {', '.join(args.paths)} (flush every {cfg.flush_seconds:g}s, Ctrl-C to stop)", flush=True)
    tail(Detective(cfg), args.paths, cfg.flush_seconds, log=lambda m: print(m, flush=True))


def cmd_prune(args, cfg):
    from .agent import Detective
    res = Detective(cfg).prune(days=args.days, dry_run=args.dry_run)
    if res["cutoff"] is None:
        print("retention is 0 days (keep forever): nothing to prune")
        return
    verb = "would delete" if args.dry_run else "deleted"
    print(f"retention {res['days']:g} days, cutoff {res['cutoff_iso']}: {verb} {res['events']} events, "
          f"{res['chunks']} chunks, {res['vectors']} vectors")


def cmd_serve(args, cfg):
    from .web import serve
    serve(cfg, args.host, args.port)


def cmd_doctor(args, cfg):
    from .config import auth_file_path
    from .net import discover_gateways, gemini_gateway, ollama_gateway, pinecone_gateway
    print(f"in_sprite        : {in_sprite()}")
    print(f"home / tz        : {cfg.home} / {cfg.tz.key}")
    gws = discover_gateways(cfg)
    print(f"gateway discovery: {gws or '(none)'}")
    print(f"pinecone         : gateway={pinecone_gateway(cfg) or '-'} api_key={'set' if cfg.pinecone_api_key else '-'}"
          f" index={cfg.pinecone_index} ns={cfg.pinecone_namespace}")
    print(f"auth file        : {auth_file_path()} ({'found' if os.path.isfile(auth_file_path()) else 'missing'})")
    print(f"gemini           : gateway={gemini_gateway(cfg) or '-'} api_key={'set' if cfg.gemini_api_key else '-'}")
    print(f"ollama           : gateway={ollama_gateway(cfg) or '-'} api_key={'set' if cfg.ollama_api_key else '-'}"
          f" local={cfg.ollama_host}")
    from .agent import Detective
    d = Detective(cfg)
    from .llm import get_llm

    def llm_name() -> str:
        llm = get_llm(cfg)
        return llm.name if llm else "none (rule-based reports)"

    probes = (("vector store", lambda: d.vs.name + (f" via {d.vs.via}" if hasattr(d.vs, "via") else "")),
              ("embedder", lambda: f"{d.emb.name} dim={len(d.emb.embed(['probe'])[0])}"),
              ("llm", llm_name))
    for label, fn in probes:
        try:
            print(f"{label:<17}: {fn()}")
        except Exception as e:
            print(f"{label:<17}: ERROR {e}")
    if getattr(d.vs, "name", "") == "pinecone":
        try:
            print(f"pinecone vectors : {d.vs.count()} in namespace '{cfg.pinecone_namespace}'")
        except Exception as e:
            print(f"pinecone vectors : ERROR {str(e)[:160]}")
    ev, ch = d.store.counts()
    print(f"local store      : {ev} events, {ch} chunks")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="logdet", description="Ask your logs why things broke.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="parse, chunk, embed and index log files/dirs")
    p.add_argument("paths", nargs="+")
    p.add_argument("--reset", action="store_true", help="clear local store and vector namespace first")
    p.add_argument("--follow-state", action="store_true",
                   help="incremental: read only lines appended since the last run (checkpoints in LOGDET_HOME)")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("tail", help="watch files and ingest new lines every LOGDET_FLUSH_SECONDS (Sprite service)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("ask", help="ask a question in natural language")
    p.add_argument("question")
    p.add_argument("--now", help="pretend current time (ISO 8601), for relative phrases like 'yesterday'")
    p.add_argument("--no-llm", action="store_true", help="skip the LLM, use the heuristic report")
    p.add_argument("--json", action="store_true")
    p.add_argument("-e", "--evidence", action="store_true", help="print cited evidence lines")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("sample", help="generate the sample incident dataset")
    p.add_argument("--out", default="data/sample")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(fn=cmd_sample)

    p = sub.add_parser("demo", help="generate sample data for yesterday and ingest it (resets the index)")
    p.add_argument("--out", default="data/sample")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("prune", help="delete data older than the retention period (LOGDET_RETENTION_DAYS)")
    p.add_argument("--dry-run", action="store_true", help="only count what would be deleted")
    p.add_argument("--days", type=float, help="override LOGDET_RETENTION_DAYS for this run (0 = keep forever)")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("serve", help="serve the web UI + JSON API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("doctor", help="show which connectors/backends are active")
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        args.fn(args, Config())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
