"""Unit tests for baseline/anomaly statistics on small synthetic event streams."""
from logdet.analyze import analyze, pct
from logdet.parse import Event
from logdet.store import Store

T0 = 1_800_000_000.0  # arbitrary epoch, minute-aligned


def req(ts, lat, status=200, path="/api/cart"):
    return Event("nginx.log", int(ts), ts, "ERROR" if status >= 500 else "INFO", f"GET {path} {status}",
                 f"raw {ts}", status, lat, path, "nginx")


def app(ts, level, msg):
    return Event("app.log", int(ts), ts, level, msg, f"{ts} {level} {msg}")


def build(tmp_path, incident=True):
    evs = []
    for s in range(0, 3600, 2):  # 1h baseline, 0.5 rps, ~100 ms
        evs.append(req(T0 + s, 90 + (s % 20)))
        if s % 600 == 0:
            evs.append(app(T0 + s + 1, "WARN", f"cache miss ratio high ({s % 7})"))
    evs.append(app(T0 + 3600 - 120, "INFO", "scheduler: job nightly_report started (parallelism=48)"))
    for s in range(3600, 3600 + 1200, 2):  # 20 min window
        bad = incident and 3600 + 300 <= s < 3600 + 900  # minutes 5..15 broken
        evs.append(req(T0 + s, 3100 if bad else 95, 503 if bad and s % 6 == 0 else 200))
        if bad and s % 10 == 0:
            evs.append(app(T0 + s, "ERROR", f"Connection is not available, request timed out after 3000ms id={s}"))
        if s % 300 == 0:
            evs.append(app(T0 + s + 1, "WARN", f"cache miss ratio high ({s % 7})"))
    st = Store(tmp_path / "t.sqlite3")
    st.add_events(evs)
    return st


def test_pct():
    assert pct([], .5) is None and pct([5], .95) == 5 and pct(list(range(1, 101)), .95) == 95


def test_detects_anomaly_window_and_new_signature(tmp_path):
    st = analyze(build(tmp_path), T0 + 3600, T0 + 4800)
    a = st.anomaly
    assert a and a["start"] == T0 + 3900 and a["end"] == T0 + 4500
    assert a["peak_p95"] >= 3000 and st.baseline.p95 < 200
    new = [s for s in st.signatures if s["new"]]
    conn = next(s for s in new if s["sig"].startswith("Connection is not available"))
    assert conn["first_ts"] == T0 + 3900 and conn["count"] == 60
    assert any(s["sig"] == "GET /api/cart -> 5xx" for s in new)
    cache = next(s for s in st.signatures if s["sig"].startswith("cache miss"))
    assert not cache["new"] and cache["ratio"] is not None  # seen in baseline too -> not new
    assert any("nightly_report started" in c["msg"] for c in st.changes)
    assert st.paths[0]["path"] == "/api/cart" and st.paths[0]["p95"] > st.paths[0]["base_p95"]


def test_quiet_window_has_no_anomaly(tmp_path):
    st = analyze(build(tmp_path, incident=False), T0 + 3600, T0 + 4800)
    assert st.anomaly is None and not [s for s in st.signatures if s["new"]]


def test_baseline_falls_back_to_data_outside_window(tmp_path):
    store = build(tmp_path)
    # a window at the very start has no history before it -> baseline uses the rest of the data
    st = analyze(store, T0, T0 + 600)
    assert st.baseline.requests > 1000 and st.anomaly is None
