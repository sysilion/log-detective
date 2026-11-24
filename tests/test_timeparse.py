from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logdet.timeparse import parse_time_range

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=KST)


@pytest.mark.parametrize("q,start,end", [
    ("어제 새벽 3시에 왜 느려졌어?", "2026-11-23 02:30", "2026-11-23 04:00"),
    ("Why did checkout slow down at 3am yesterday?", "2026-11-23 02:30", "2026-11-23 04:00"),
    ("yesterday at 03:00 errors", "2026-11-23 02:30", "2026-11-23 04:00"),
    ("어제 오후 3시 반", "2026-11-23 15:00", "2026-11-23 16:30"),
    ("2026-11-20 14:10 spike?", "2026-11-20 13:40", "2026-11-20 15:10"),
    ("11월 22일에 무슨 일?", "2026-11-22 00:00", "2026-11-23 00:00"),
    ("what happened yesterday", "2026-11-23 00:00", "2026-11-24 00:00"),
    ("지난 2시간 에러", "2026-11-24 08:00", "2026-11-24 10:00"),
    ("last 30 minutes", "2026-11-24 09:30", "2026-11-24 10:00"),
    ("오전 9시에 왜 에러?", "2026-11-24 08:30", "2026-11-24 10:00"),
    ("at 11pm", "2026-11-23 22:30", "2026-11-24 00:00"),  # future today -> yesterday
    ("3시간 전에 왜 느렸어?", "2026-11-24 06:30", "2026-11-24 08:00"),  # "3 hours ago", not 3 o'clock
    ("30분 전 에러", "2026-11-24 09:00", "2026-11-24 10:00"),
    ("errors 2 hours ago", "2026-11-24 07:30", "2026-11-24 09:00"),
    ("지난 3시간 동안", "2026-11-24 07:00", "2026-11-24 10:00"),
    ("새벽 12시 반", "2026-11-24 00:00", "2026-11-24 01:30"),
    ("밤 11시", "2026-11-23 22:30", "2026-11-24 00:00"),
])
def test_ranges(q, start, end):
    tr = parse_time_range(q, NOW, KST)
    assert tr.start.strftime("%Y-%m-%d %H:%M") == start
    assert tr.end.strftime("%Y-%m-%d %H:%M") == end


def test_default_uses_data_range():
    last = datetime(2026, 11, 23, 6, 0, tzinfo=KST).timestamp()
    tr = parse_time_range("why is it slow?", NOW, KST, (last - 3600, last))
    assert not tr.explicit and tr.end.timestamp() == last + 1
