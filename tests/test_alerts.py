"""Slack / Discord / generic webhook payloads and delivery (mocked HTTP layer)."""
import io
import json
import urllib.error
from zoneinfo import ZoneInfo

from logdet.alerts import build_discord, build_generic, build_slack, notify, post_json, targets

KST = ZoneInfo("Asia/Seoul")
F = {"id": "f1", "type": "alert", "watch": "checkout", "severity": "critical", "created": 1795370520,
     "window": [1795369620, 1795370520], "reasons": ["5xx rate 38.0% ≥ 5%", "p95 3091ms is 22.0x baseline"],
     "summary": "**Summary** pool exhausted by nightly_sales_report [E1]", "llm": "gemini:x",
     "evidence": [{"cite": f"E{i}", "source": "app.log", "lineno": i, "line": f"line {i} " + "x" * 300}
                  for i in range(1, 6)]}


def test_slack_blocks():
    p = build_slack(F, KST, "https://demo")
    types = [b["type"] for b in p["blocks"]]
    assert types == ["header", "section", "section", "section", "context"]
    assert p["blocks"][0]["text"]["text"].startswith("🚨 Critical: checkout")
    code = p["blocks"][3]["text"]["text"]
    assert code.startswith("```") and code.count("[E") == 3  # top 3 cited lines only
    assert "<https://demo|" in p["blocks"][4]["elements"][0]["text"]
    long = build_slack({**F, "summary": "y" * 10000}, KST)
    assert len(long["blocks"][2]["text"]["text"]) <= 3000 and long["blocks"][-1]["type"] != "context"


def test_discord_embed_limits():
    p = build_discord({**F, "summary": "z" * 9000}, KST, "https://demo")
    e = p["embeds"][0]
    assert len(p["embeds"]) <= 10 and len(e["description"]) == 4096 and e["color"] == 0xDC2626
    assert all(len(f["value"]) <= 1024 for f in e["fields"]) and e["url"] == "https://demo"
    assert build_discord({**F, "type": "resolved", "severity": "resolved"}, KST)["embeds"][0]["color"] == 0x16A34A


def test_generic_payload():
    assert build_generic(F, KST, "u")["finding"]["id"] == "f1"


class Resp:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_post_json_retries_discord_429():
    calls, sleeps = [], []

    def opener(req, timeout):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, io.BytesIO(b'{"retry_after": 0.25}'))
        return Resp()
    assert post_json("https://discord/x", {"a": 1}, opener=opener, sleep=sleeps.append) == 204
    assert sleeps == [0.25] and len(calls) == 2


def test_post_json_no_retry_on_4xx():
    calls = []

    def opener(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, io.BytesIO(b""))
    assert post_json("https://x", {}, opener=opener, sleep=lambda s: None) == 404 and calls == [1]


def test_notify_targets_and_never_logs_urls(monkeypatch):
    monkeypatch.setenv("LOGDET_SLACK_WEBHOOK", "https://hooks.slack.com/services/SECRET1")
    monkeypatch.setenv("LOGDET_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/SECRET2")
    monkeypatch.delenv("LOGDET_ALERT_WEBHOOK", raising=False)
    assert set(targets()) == {"slack", "discord"}
    sent, logs = {}, []

    def post(url, payload):
        sent[url] = payload
        return 500 if "discord" in url else 200
    res = notify(F, KST, {}, "https://demo", log=logs.append, post=post)
    assert res == {"slack": 200, "discord": 500}
    assert "blocks" in sent["https://hooks.slack.com/services/SECRET1"]
    assert "embeds" in sent["https://discord.com/api/webhooks/SECRET2"]
    assert logs and all("SECRET" not in m and "https://" not in m for m in logs)
