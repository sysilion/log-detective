"""Incremental ingestion: checkpoints, rotation/truncation, partial lines, open-window re-upsert, tail."""
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logdet.agent import Detective
from logdet.config import Config
from logdet.ingest import Checkpoints, follow_files, parse_payload, read_new_lines, tail

KST = ZoneInfo("Asia/Seoul")


def line(h, m, s, msg, level="INFO"):
    return f"2026-11-23 {h:02d}:{m:02d}:{s:02d}.000 +09:00 [1] {level}:  {msg}\n"


@pytest.fixture()
def det(tmp_path):
    cfg = Config()
    cfg.home, cfg.tz, cfg.vector, cfg.embed, cfg.llm, cfg.retention_days = \
        tmp_path / "home", KST, "local", "hash", "none", 0
    return Detective(cfg)


def test_read_new_lines_partial_rotation_truncation(tmp_path):
    f = tmp_path / "app.log"
    f.write_text("a\nb\npartial")
    lines, ck = read_new_lines(f, {})
    assert lines == ["a\n", "b\n"] and ck["offset"] == 4  # trailing partial line waits
    with open(f, "a") as fh:
        fh.write(" done\nc\n")
    lines, ck = read_new_lines(f, ck)
    assert lines == ["partial done\n", "c\n"]
    f.write_text("x\n")  # truncated (smaller than offset) -> restart
    lines, ck2 = read_new_lines(f, ck)
    assert lines == ["x\n"]
    os.rename(f, tmp_path / "app.log.1")  # rotated: new inode at the same path
    f.write_text("new1\n")
    lines, _ = read_new_lines(f, ck2)
    assert lines == ["new1\n"]


def test_follow_only_reads_new_lines_and_reupserts_open_window(det, tmp_path):
    f = tmp_path / "logs" / "db.log"
    f.parent.mkdir()
    f.write_text(line(3, 0, 1, "ok") + line(3, 0, 30, "ok"))
    r1 = follow_files(det, [str(f.parent)], log=lambda *_: None)
    assert r1["events"] == 2 and r1["chunks"] == 1
    cid = next(iter(det.vs.items))
    with open(f, "a") as fh:  # same 2-minute window (03:00-03:02) gets a new error line
        fh.write(line(3, 1, 10, "connection refused", "ERROR"))
    r2 = follow_files(det, [str(f.parent)], log=lambda *_: None)
    assert r2["events"] == 1 and r2["chunks"] == 1
    assert list(det.vs.items) == [cid]  # same chunk id, overwritten in place
    md = det.vs.items[cid]["metadata"]
    assert md["n_errors"] == 1 and "connection refused" in md["text"]
    assert follow_files(det, [str(f.parent)], log=lambda *_: None)["events"] == 0  # nothing new
    assert det.store.counts()[0] == 3
    # line numbers keep counting per source after rotation
    os.rename(f, f.with_name("db.log.1"))
    f.write_text(line(3, 5, 0, "after rotate"))
    follow_files(det, [str(f)], log=lambda *_: None)
    assert det.store.max_lineno("db.log") == 4


def test_checkpoints_persist(tmp_path):
    c = Checkpoints(tmp_path)
    c.set("file:/x", {"inode": 1, "offset": 10})
    assert Checkpoints(tmp_path).get("file:/x") == {"inode": 1, "offset": 10}


def test_tail_flushes_appended_lines(det, tmp_path):
    f = tmp_path / "t.log"
    f.write_text(line(4, 0, 0, "start"))
    stop, logs = threading.Event(), []
    th = threading.Thread(target=tail, args=(det, [str(f)], 0.05), kwargs={"log": logs.append, "stop": stop})
    th.start()
    try:
        for _ in range(100):
            if det.store.counts()[0] == 1:
                break
            stop.wait(0.02)
        with open(f, "a") as fh:
            fh.write(line(4, 0, 5, "more") + line(4, 0, 6, "boom", "ERROR"))
        for _ in range(200):
            if det.store.counts()[0] == 3:
                break
            stop.wait(0.02)
    finally:
        stop.set()
        th.join(2)
    assert det.store.counts()[0] == 3 and any("+2 events" in m for m in logs)


def test_parse_payload_formats():
    nd = b'{"date": 1795370400.5, "log": "GET /x 500", "level": "error"}\n{"timestamp": "2026-11-23T03:00:01Z", "message": "ok"}\n'
    evs = parse_payload(nd, "fluentbit", KST)
    assert len(evs) == 2 and evs[0].ts == 1795370400.5 and evs[0].level == "ERROR" and evs[1].msg == "ok"
    arr = b'[{"date": 1795370400, "log": "a"}, {"date": 1795370401, "log": "b"}]'
    assert [e.msg for e in parse_payload(arr, "fb", KST)] == ["a", "b"]
    plain = b"2026-11-23 03:00:00 ERROR boom\nno timestamp here\n"
    evs = parse_payload(plain, "txt", KST)
    assert len(evs) == 2 and evs[0].level == "ERROR" and evs[1].msg == "no timestamp here"
