"""Generate a realistic sample dataset with an injected incident.

Story (all times local to --tz, on --date, default: yesterday):
  00:00-06:00  normal traffic for a small shop (nginx -> checkout-service -> Postgres)
  01:10        routine deploy of checkout-service v2.14.1 (red herring, no impact)
  01:30        a few slow payment-provider responses (red herring)
  02:57:03     scheduler starts `nightly_sales_report` with parallelism=48 on the
               SHARED datasource HikariPool-1 (maxPoolSize=50)  <-- root cause
  03:00-03:24  pool exhausted: checkout/cart requests wait 3s for a connection,
               p95 latency 120ms -> ~3s, 503/504s, Postgres logs 40s+ report queries
  03:24:10     job finishes, pool drains, latency recovers by ~03:27
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

PATHS = [("GET", "/api/products/{id}", 0.45, 35), ("GET", "/api/cart", 0.22, 60),
         ("POST", "/api/checkout", 0.13, 120), ("POST", "/api/login", 0.10, 80), ("GET", "/healthz", 0.10, 2)]
DB_PATHS = {"/api/cart", "/api/checkout", "/api/login"}
UAS = ["Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X)", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
       "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_1)", "okhttp/4.12.0", "kube-probe/1.31"]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def generate(out_dir: str | Path, date: datetime, tz: tzinfo, seed: int = 7, hours: int = 6) -> list[Path]:
    rnd = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    day = datetime(date.year, date.month, date.day, tzinfo=tz)
    at = lambda h, m, s=0: day + timedelta(hours=h, minutes=m, seconds=s)  # noqa: E731
    job_start, inc_start, job_end, recovered = at(2, 57, 3), at(3, 0, 0), at(3, 24, 10), at(3, 27, 0)
    end = day + timedelta(hours=hours)

    def severity(t: datetime) -> float:
        """0 = healthy, 1 = fully exhausted pool."""
        if job_start <= t < inc_start:
            return 0.15 * (t - job_start).total_seconds() / (inc_start - job_start).total_seconds()
        if inc_start <= t < job_end:
            return 1.0
        if job_end <= t < recovered:
            return 1.0 - (t - job_end).total_seconds() / (recovered - job_end).total_seconds()
        return 0.0

    nginx, app, pg = [], [], []
    t = day
    req_id = 0
    while t < end:
        t += timedelta(seconds=rnd.expovariate(1.2))
        r, acc = rnd.random(), 0.0
        for method, tmpl, w, base in PATHS:
            acc += w
            if r <= acc:
                break
        path = tmpl.replace("{id}", str(rnd.randint(1000, 9999)))
        lat = rnd.lognormvariate(0, 0.35) * base
        status = 200 if method == "GET" else (201 if tmpl == "/api/checkout" else 200)
        if rnd.random() < 0.01 and tmpl.startswith("/api/products"):
            status = 404
        sev = severity(t)
        if tmpl in DB_PATHS and sev > 0:
            if rnd.random() < sev * 0.9:
                lat = 3000 + rnd.uniform(5, 250)
                status = 503 if rnd.random() < 0.6 * sev else (504 if rnd.random() < 0.3 * sev else status)
            else:
                lat *= 1 + 6 * sev
        elif tmpl.startswith("/api/products") and sev > 0:
            lat *= 1 + 1.5 * sev
        if rnd.random() < 0.0005:
            status = 502
        req_id += 1
        rid = f"{rnd.getrandbits(64):016x}"
        ts_n = t.strftime("%d/%b/%Y:%H:%M:%S %z")
        ip = f"10.{rnd.randint(0, 3)}.{rnd.randint(0, 255)}.{rnd.randint(1, 254)}"
        ua = UAS[4] if tmpl == "/healthz" else rnd.choice(UAS[:4])
        nginx.append((t, f'{ip} - - [{ts_n}] "{method} {path} HTTP/1.1" {status} {rnd.randint(120, 4800)} '
                         f'"-" "{ua}" rt={lat / 1000:.3f}'))
        if tmpl in DB_PATHS:
            done = t + timedelta(milliseconds=lat)
            if status in (503, 504):
                app.append((done, {"ts": _iso(done), "level": "ERROR", "service": "checkout-service",
                                   "msg": "HikariPool-1 - Connection is not available, request timed out after 3000ms.",
                                   "path": tmpl, "request_id": rid,
                                   "exception": "java.sql.SQLTransientConnectionException"}))
            elif tmpl == "/api/checkout":
                app.append((done, {"ts": _iso(done), "level": "INFO", "service": "checkout-service",
                                   "msg": "order placed", "path": tmpl, "status": status,
                                   "latency_ms": round(lat, 1), "order_id": f"ord_{rnd.getrandbits(40):010x}",
                                   "request_id": rid}))

    def a(dt, level, msg, **kw):
        app.append((dt, {"ts": _iso(dt), "level": level, "service": kw.pop("service", "checkout-service"),
                         "msg": msg, **kw}))

    def p(dt, pid, level, msg):
        pg.append((dt, f"{dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {dt.strftime('%z')[:3]}:"
                       f"{dt.strftime('%z')[3:]} [{pid}] {level}:  {msg}"))

    # routine background noise
    for h in range(hours):
        a(at(h, 15), "INFO", "cron job cleanup_expired_sessions started", service="scheduler")
        a(at(h, 15, 4), "INFO", "cron job cleanup_expired_sessions finished in 3.8s (deleted=412)", service="scheduler")
    for m in range(0, hours * 60, 5):
        p(at(0, m, 7), 61, "LOG", f"checkpoint complete: wrote {rnd.randint(80, 900)} buffers "
                                  f"({rnd.uniform(0.1, 1.5):.1f}%); write={rnd.uniform(1, 30):.3f} s")
    for m in range(0, hours * 60, 1):
        if not (job_start <= at(0, m) < job_end):
            a(at(0, m, 30), "DEBUG", "HikariPool-1 - pool stats (total=50, active=%d, idle=%d, waiting=0)"
              % ((k := rnd.randint(3, 9)), 50 - k))
    for m in range(0, hours * 60, 7):
        if rnd.random() < 0.5:
            a(at(0, m, rnd.randint(0, 59)), "WARN", "cache miss ratio high for product_catalog (0.%02d)"
              % rnd.randint(30, 45), service="catalog-cache")

    # red herrings
    a(at(1, 10, 2), "INFO", "deployed checkout-service v2.14.1 (commit 8f3a2c1) rollout complete 3/3 pods",
      service="deployer")
    for s in (5, 37, 52):
        a(at(1, 30, s), "WARN", f"payment provider slow response provider=pg-mock latency_ms={rnd.randint(1500, 2100)}")

    # the incident
    a(job_start, "INFO", "scheduler: job nightly_sales_report started (parallelism=48, datasource=HikariPool-1 shared, "
                         "range=2026-10-01..now)", service="scheduler")
    for i in range(48):
        p(job_start + timedelta(milliseconds=40 * i + rnd.randint(0, 30)), 4100 + i, "LOG",
          "connection authorized: user=report_ro database=shop application_name=nightly_sales_report")
    t = inc_start - timedelta(seconds=60)
    while t < job_end:
        t += timedelta(seconds=rnd.uniform(15, 40))
        p(t, rnd.randint(4100, 4147), "LOG",
          f"duration: {rnd.uniform(38000, 61000):.3f} ms  statement: SELECT o.id, o.total, li.sku, li.qty "
          f"FROM orders o JOIN line_items li ON li.order_id = o.id WHERE o.created_at >= '2026-10-01' "
          f"ORDER BY o.created_at")
    t = inc_start
    while t < job_end:
        a(t + timedelta(seconds=rnd.uniform(0, 3)), "WARN",
          "HikariPool-1 - pool stats (total=50, active=50, idle=0, waiting=%d)" % rnd.randint(18, 44))
        t += timedelta(seconds=30)
    a(inc_start + timedelta(seconds=41), "WARN",
      "HikariPool-1 - Thread starvation or clock leap detected (housekeeper delta=31s).")
    a(job_end, "INFO", "scheduler: job nightly_sales_report finished in 1627s (rows=1284331)", service="scheduler")
    for i in range(48):
        p(job_end + timedelta(milliseconds=30 * i), 4100 + i, "LOG",
          "disconnection: session time: 0:27:07.%03d user=report_ro database=shop" % rnd.randint(0, 999))

    files = []
    for name, rows, as_json in (("nginx_access.log", nginx, False), ("checkout-service.jsonl", app, True),
                                ("postgres.log", pg, False)):
        rows.sort(key=lambda x: x[0])
        f = out / name
        with open(f, "w") as fh:
            for _, row in rows:
                fh.write((json.dumps(row, ensure_ascii=False) if as_json else row) + "\n")
        files.append(f)
    return files
