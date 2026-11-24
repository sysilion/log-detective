"""Alert delivery for watch findings: Slack (blocks), Discord (embeds) and a generic JSON webhook.

Targets come from env vars (LOGDET_SLACK_WEBHOOK, LOGDET_DISCORD_WEBHOOK, LOGDET_ALERT_WEBHOOK), the
[alerts] table of the watch config, or the auth file ("slack" / "discord" / "webhook" entries, key = URL).
Webhook URLs are secrets: they are never printed or logged; errors name the target type only.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from .config import _env, auth_key

SEVERITY_EMOJI = {"critical": "🚨", "warning": "⚠️", "resolved": "✅", "info": "ℹ️"}
SEVERITY_COLOR = {"critical": 0xDC2626, "warning": 0xF59E0B, "resolved": 0x16A34A, "info": 0x64748B}
SLACK_TEXT_MAX = 2900      # section text limit is 3000
DISCORD_DESC_MAX = 4096
DISCORD_FIELD_MAX = 1024


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def targets(file_cfg: dict | None = None) -> dict[str, str]:
    """Configured webhook URLs by type: env > watch-config [alerts] > auth file."""
    fc = file_cfg or {}
    out = {}
    for kind, env, fkey, akey in (("slack", "LOGDET_SLACK_WEBHOOK", "slack_webhook", "slack"),
                                  ("discord", "LOGDET_DISCORD_WEBHOOK", "discord_webhook", "discord"),
                                  ("webhook", "LOGDET_ALERT_WEBHOOK", "webhook", "webhook")):
        url = _env(env) or str(fc.get(fkey) or "") or auth_key(akey)
        if url:
            out[kind] = url
    return out


def _title(f: dict) -> str:
    sev = f.get("severity", "warning")
    head = "Resolved" if f.get("type") == "resolved" else sev.capitalize()
    return f"{SEVERITY_EMOJI.get(sev, '')} {head}: {f.get('watch', 'watch')}"


def _window(f: dict, tz) -> str:
    try:
        a, b = (datetime.fromtimestamp(t, tz) for t in f["window"])
        return f"{a:%Y-%m-%d %H:%M} → {b:%H:%M} {getattr(tz, 'key', '')}".strip()
    except (KeyError, TypeError, ValueError):
        return "-"


def _cited(f: dict, n: int = 3) -> str:
    return "\n".join(f"[{e.get('cite', '')}] {e.get('source')}:{e.get('lineno')} {e.get('line', '')[:220]}"
                     for e in (f.get("evidence") or [])[:n])


def build_slack(f: dict, tz, ui_url: str = "") -> dict:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": _cut(_title(f), 150), "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Severity*\n{f.get('severity', '-')}"},
            {"type": "mrkdwn", "text": f"*Window*\n{_window(f, tz)}"},
            {"type": "mrkdwn", "text": f"*Why*\n{_cut('; '.join(f.get('reasons') or []) or '-', 900)}"},
            {"type": "mrkdwn", "text": f"*Model*\n{f.get('llm') or '-'}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut(f.get("summary") or "-", SLACK_TEXT_MAX)}},
    ]
    cited = _cited(f)
    if cited:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": "```" + _cut(cited, SLACK_TEXT_MAX - 6) + "```"}})
    if ui_url:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{ui_url}|Open log-detective>"}]})
    return {"text": _cut(f"{_title(f)}: {(f.get('summary') or '').splitlines()[0] if f.get('summary') else ''}", 300),
            "blocks": blocks}


def build_discord(f: dict, tz, ui_url: str = "") -> dict:
    sev = "resolved" if f.get("type") == "resolved" else f.get("severity", "warning")
    embed = {
        "title": _cut(_title(f), 256),
        "description": _cut(f.get("summary") or "-", DISCORD_DESC_MAX),
        "color": SEVERITY_COLOR.get(sev, 0x64748B),
        "fields": [
            {"name": "Severity", "value": _cut(str(f.get("severity", "-")), DISCORD_FIELD_MAX), "inline": True},
            {"name": "Window", "value": _cut(_window(f, tz), DISCORD_FIELD_MAX), "inline": True},
            {"name": "Why", "value": _cut("; ".join(f.get("reasons") or []) or "-", DISCORD_FIELD_MAX)},
        ],
    }
    cited = _cited(f)
    if cited:
        embed["fields"].append({"name": "Cited lines", "value": _cut("```" + cited[: DISCORD_FIELD_MAX - 7] + "```",
                                                                    DISCORD_FIELD_MAX)})
    if ui_url:
        embed["url"] = ui_url
    if f.get("created"):
        embed["timestamp"] = datetime.fromtimestamp(f["created"], timezone.utc).isoformat()
    return {"username": "log-detective", "embeds": [embed][:10]}


def build_generic(f: dict, tz, ui_url: str = "") -> dict:
    return {"source": "log-detective", "ui_url": ui_url or None, "finding": f}


BUILDERS: dict[str, Callable] = {"slack": build_slack, "discord": build_discord, "webhook": build_generic}


def post_json(url: str, payload: dict, opener=urllib.request.urlopen, sleep=time.sleep, attempts: int = 4) -> int:
    """POST with retries on 429 (Retry-After header or Discord's retry_after body) and 5xx. Returns status."""
    data = json.dumps(payload).encode()
    status = 0
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "log-detective"})
        try:
            with opener(req, timeout=15) as r:
                return getattr(r, "status", 200)
        except urllib.error.HTTPError as e:
            status = e.code
            if e.code != 429 and e.code < 500:
                return status
            wait = 1.0 * (i + 1)
            try:
                body = json.loads(e.read() or b"{}")
                wait = float(body.get("retry_after", wait))  # Discord: seconds (float)
            except (ValueError, AttributeError):
                pass
            wait = float(e.headers.get("Retry-After") or wait) if e.headers else wait
            sleep(min(wait, 30.0))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            status = 0
            sleep(1.0 * (i + 1))
    return status


def notify(finding: dict, tz, file_cfg: dict | None = None, ui_url: str = "", log=print,
           post=post_json) -> dict[str, int]:
    """Send a finding to every configured target. Returns {target_type: http_status}. URLs are never logged."""
    results = {}
    for kind, url in targets(file_cfg).items():
        try:
            results[kind] = post(url, BUILDERS[kind](finding, tz, ui_url))
        except Exception as e:  # never let alerting break the watch loop
            results[kind] = 0
            log(f"[alerts] {kind}: delivery error {type(e).__name__}")
        if not 200 <= results[kind] < 300:
            log(f"[alerts] {kind}: delivery failed (HTTP {results[kind]})")
    return results
