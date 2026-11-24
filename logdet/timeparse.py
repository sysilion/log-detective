"""Extract a time range from a natural-language question (Korean + English)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

POINT_BEFORE = timedelta(minutes=30)
POINT_AFTER = timedelta(minutes=60)


@dataclass
class TimeRange:
    start: datetime
    end: datetime
    label: str
    explicit: bool = True  # False when we fell back to a default window

    @property
    def start_ts(self) -> float:
        return self.start.timestamp()

    @property
    def end_ts(self) -> float:
        return self.end.timestamp()


_DAY_WORDS = [
    (re.compile(r"그저께|그제|day before yesterday"), -2),
    (re.compile(r"어제|어젯밤|yesterday|last night"), -1),
    (re.compile(r"오늘|금일|today|this morning|tonight"), 0),
]
_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})월\s*(\d{1,2})일|\b(\d{1,2})/(\d{1,2})\b")
_REL_RE = re.compile(
    r"(?:지난|최근|last|past)\s*(\d+)\s*(분|시간|일|min(?:ute)?s?|h(?:ou)?rs?|hours?|days?)", re.I)
_KO_TIME_RE = re.compile(r"(새벽|오전|오후|아침|저녁|밤|낮)?\s*(\d{1,2})\s*시(?!간)\s*(?:(\d{1,2})\s*분|(반))?")
# "30분 전", "2시간 전", "45 minutes ago", "2 hours ago" -> a point in time relative to now
_AGO_RE = re.compile(r"(\d+)\s*(분|시간|min(?:ute)?s?|h(?:ou)?rs?|hours?)\s*(?:전|ago)", re.I)
_EN_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", re.I)
_CLOCK_RE = re.compile(r"(?<![\d/:-])(\d{1,2}):(\d{2})(?![\d:])")


def _find_time(q: str) -> tuple[int, int] | None:
    m = _KO_TIME_RE.search(q)
    if m:
        period, h, mm, half = m.groups()
        h, mm = int(h), (30 if half else int(mm or 0))
        if period in ("오후", "저녁", "밤", "낮") and h < 12:
            h += 12
        if period in ("새벽", "오전", "아침") and h == 12:
            h = 0
        if h <= 24:
            return h % 24, mm
    m = _EN_TIME_RE.search(q)
    if m:
        h, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if ap.startswith("p") and h < 12:
            h += 12
        if ap.startswith("a") and h == 12:
            h = 0
        return h, mm
    m = _CLOCK_RE.search(q)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_time_range(question: str, now: datetime, tz: tzinfo,
                     data_range: tuple[float, float] | None = None) -> TimeRange:
    q = question.lower()
    now = now.astimezone(tz)

    m = _REL_RE.search(q)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = (timedelta(minutes=n) if unit.startswith(("분", "min")) else
                 timedelta(days=n) if unit.startswith(("일", "day")) else timedelta(hours=n))
        return TimeRange(now - delta, now, f"last {m.group(1)}{m.group(2)}")

    m = _AGO_RE.search(q)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        point = now - (timedelta(minutes=n) if unit.startswith(("분", "min")) else timedelta(hours=n))
        return TimeRange(point - POINT_BEFORE, min(point + POINT_AFTER, now), f"{m.group(0)} ({point:%H:%M})")

    day: datetime | None = None
    dm = _DATE_RE.search(q)
    if dm:
        g = dm.groups()
        if g[0]:
            y, mo, d = int(g[0]), int(g[1]), int(g[2])
        elif g[3]:
            y, mo, d = now.year, int(g[3]), int(g[4])
        else:
            y, mo, d = now.year, int(g[5]), int(g[6])
        try:
            day = datetime(y, mo, d, tzinfo=tz)
        except ValueError:
            day = None
    if day is None:
        for rx, off in _DAY_WORDS:
            if rx.search(q):
                day = (now + timedelta(days=off)).replace(hour=0, minute=0, second=0, microsecond=0)
                break

    # strip dates before looking for clock times so "2026-11-23" isn't read as a time
    t = _find_time(_DATE_RE.sub(" ", q))
    if t is not None:
        h, mm = t
        if day is None:
            point = now.replace(hour=h, minute=mm, second=0, microsecond=0)
            if point > now:
                point -= timedelta(days=1)
        else:
            point = day.replace(hour=h, minute=mm)
        return TimeRange(point - POINT_BEFORE, point + POINT_AFTER, point.strftime("%Y-%m-%d %H:%M"))

    if day is not None:
        return TimeRange(day, day + timedelta(days=1), day.strftime("%Y-%m-%d"))

    # no time in the question: last 24h of data (or of wall clock)
    end = datetime.fromtimestamp(data_range[1] + 1, tz) if data_range else now
    return TimeRange(end - timedelta(hours=24), end, "latest 24h of data", explicit=False)
