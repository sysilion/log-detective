from datetime import datetime
from zoneinfo import ZoneInfo

from logdet.parse import parse_line, parse_lines, signature

KST = ZoneInfo("Asia/Seoul")


def test_json_line():
    ev = parse_line('{"ts":"2026-11-23T03:00:01.250+09:00","level":"error","msg":"boom 42","status":503,'
                    '"latency_ms":3001.5,"path":"/api/cart"}', "app.jsonl", 1, KST)
    assert ev.level == "ERROR" and ev.status == 503 and ev.latency_ms == 3001.5 and ev.path == "/api/cart"
    assert ev.is_error
    assert ev.ts == datetime(2026, 11, 23, 3, 0, 1, 250000, tzinfo=KST).timestamp()


def test_nginx_line():
    ev = parse_line('10.0.0.1 - - [23/Nov/2026:03:00:01 +0900] "GET /api/products/1234?x=1 HTTP/1.1" 504 12 '
                    '"-" "curl/8" rt=3.120', "nginx.log", 7, KST)
    assert ev.status == 504 and abs(ev.latency_ms - 3120) < 1e-6 and ev.path == "/api/products/1234"
    assert signature(ev) == "GET /api/products/<n> -> 5xx"


def test_plain_line_and_multiline_fold():
    lines = ["2026-11-23 03:00:01,123 ERROR [main] Connection refused to 10.1.2.3:5432\n",
             "    at com.example.Db.connect(Db.java:42)\n",
             "2026-11-23 03:00:02 INFO ok\n"]
    evs = list(parse_lines(lines, "x.log", KST))
    assert len(evs) == 2
    assert evs[0].level == "ERROR" and "Db.java" in evs[0].raw
    assert signature(evs[0]) == "[main] Connection refused to <ip>"


def test_postgres_duration():
    ev = parse_line("2026-11-23 03:00:10.491 +09:00 [4105] LOG:  duration: 44451.482 ms  statement: SELECT 1",
                    "postgres.log", 1, KST)
    assert ev.latency_ms == 44451.482 and ev.level == "INFO"
