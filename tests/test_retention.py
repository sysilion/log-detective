"""Data retention: cutoff math, local store/vector pruning, dry-run, LOGDET_NOW and auto-prune on ingest."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logdet.agent import Detective
from logdet.config import Config, retention_cutoff
from logdet.sample import generate

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 23, 5, 0, tzinfo=KST)  # sample data covers 00:00-06:00 on 2026-11-23
TWO_HOURS = 2 / 24


def test_cutoff_math():
    now = 1_800_000_000.0
    assert retention_cutoff(now, 0) is None
    assert retention_cutoff(now, 30) == now - 30 * 86400
    assert retention_cutoff(now, 0.5) == now - 43200
    with pytest.raises(ValueError):
        retention_cutoff(now, -1)


@pytest.fixture()
def det(tmp_path):
    generate(tmp_path / "logs", datetime(2026, 11, 23), KST, hours=6)
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm, cfg.retention_days = \
        tmp_path / "home", KST, "local", "hash", "none", 0
    d = Detective(cfg)
    d.ingest([str(tmp_path / "logs")], reset=True, log=lambda *_: None)
    return d


def test_dry_run_counts_without_deleting(det):
    before = det.store.counts(), len(det.vs.items)
    res = det.prune(days=TWO_HOURS, dry_run=True, now=NOW)
    assert res["events"] > 0 and res["chunks"] > 0 and res["vectors"] == res["chunks"]
    assert (det.store.counts(), len(det.vs.items)) == before


def test_prune_deletes_only_expired_windows(det):
    dry = det.prune(days=TWO_HOURS, dry_run=True, now=NOW)
    res = det.prune(days=TWO_HOURS, now=NOW)
    assert (res["events"], res["chunks"], res["vectors"]) == (dry["events"], dry["chunks"], dry["vectors"])
    cutoff = NOW.timestamp() - 7200
    lo, _ = det.store.time_range()
    assert lo >= cutoff
    assert all(it["metadata"]["ts_end"] >= cutoff for it in det.vs.items.values())
    assert det.prune(days=TWO_HOURS, now=NOW)["events"] == 0  # idempotent
    # the incident (03:00) is still answerable after pruning older data
    assert det.ask("why slow at 3am yesterday", now=datetime(2026, 11, 24, 9, 0, tzinfo=KST))["stats"]["anomaly"]


def test_zero_days_keeps_everything(det):
    before = det.store.counts()
    res = det.prune(days=0, now=datetime(2030, 1, 1, tzinfo=KST))
    assert res["cutoff"] is None and det.store.counts() == before


def test_prune_honours_pinned_demo_clock(det):
    det.cfg.now_override = "2026-11-24T09:00"  # demo: data is 1 day old -> within 30 days
    assert det.prune(days=30)["events"] == 0
    det.cfg.now_override = "2026-11-23T05:00"
    assert det.prune(days=TWO_HOURS, dry_run=True)["events"] > 0  # LOGDET_NOW is used as "now"


def test_ingest_prunes_automatically(tmp_path):
    generate(tmp_path / "logs", datetime(2026, 11, 23), KST, hours=6)
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm = tmp_path / "home", KST, "local", "hash", "none"
    cfg.retention_days, cfg.now_override = TWO_HOURS, "2026-11-23T05:00"
    d = Detective(cfg)
    res = d.ingest([str(tmp_path / "logs")], reset=True, log=lambda *_: None)
    assert res["pruned"]["events"] > 0 and d.store.time_range()[0] >= NOW.timestamp() - 7200
