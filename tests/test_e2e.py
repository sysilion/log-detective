"""End-to-end on the bundled sample incident, fully offline (hash embeddings + local vectors)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logdet.agent import Detective
from logdet.config import Config
from logdet.sample import generate

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=KST)


@pytest.fixture(scope="module")
def det(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e")
    generate(root / "logs", datetime(2026, 11, 23), KST)
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm = root / "home", KST, "local", "hash", "none"
    d = Detective(cfg)
    res = d.ingest([str(root / "logs")], reset=True, log=lambda *_: None)
    assert res["events"] > 20000 and res["chunks"] > 300
    return d


def test_finds_incident_and_root_cause(det):
    res = det.ask("어제 새벽 3시에 왜 느려졌어?", now=NOW)
    a = res["stats"]["anomaly"]
    assert a is not None
    start = datetime.fromtimestamp(a["start"], KST)
    assert (start.hour, start.minute) in ((2, 58), (2, 59), (3, 0), (3, 1))
    assert a["peak_p95"] > 2500 and res["stats"]["baseline"]["p95"] < 400
    sigs = [s["sig"] for s in res["stats"]["signatures"] if s["new"]]
    assert any("Connection is not available" in s for s in sigs)
    lines = " ".join(e["line"] for e in res["evidence"])
    assert "nightly_sales_report started" in lines
    assert "nightly_sales_report" in res["answer"] and "가설" in res["answer"]
    assert res["llm"].startswith("rule-based")


def test_english_question_and_quiet_window(det):
    res = det.ask("Why did checkout slow down at 3am yesterday?", now=NOW)
    assert res["answer"].startswith("Between")
    quiet = det.ask("what happened at 5am yesterday?", now=NOW)
    assert quiet["stats"]["anomaly"] is None


def test_retrieval_is_time_filtered(det):
    res = det.ask("connection pool timeout yesterday 3am", now=NOW)
    lo, hi = datetime.fromisoformat(res["range"]["start"]).timestamp() - 1800, \
        datetime.fromisoformat(res["range"]["end"]).timestamp()
    assert res["retrieved"] and all(lo - 120 <= r["ts_start"] <= hi for r in res["retrieved"])
