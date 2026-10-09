"""Offline test of the dashboard server against a weather.sqlite built from the fake Open-Meteo.

Run from the project folder:  python3 -m tests.test_dashboard_offline
"""
from __future__ import annotations

import json
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from rainarchive import dashboard as Dsh
from rainarchive.config import load_config
from rainarchive.decode import RATE_LUT
from rainarchive.store import Store
from rainarchive import weather as W
from rainarchive.openmeteo import OpenMeteo
from tests.test_weather_offline import FakeOM


def get(base: str, path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def test_adaptive_stops():
    rng = np.random.default_rng(0)
    mm = np.concatenate([np.zeros(50_000), rng.lognormal(3.0, 0.8, 200_000), [400.0] * 20]).astype(np.float16)
    stops = Dsh.adaptive_stops(mm, 10)
    assert len(stops) == 10 and stops[0] == Dsh.ACC_MIN_STOP and all(a < b for a, b in zip(stops, stops[1:])), stops
    assert all(Dsh._nice_round(v) == v for v in stops[1:]), stops            # readable legend values
    assert stops[-1] < 100, stops                                          # clutter doesn't stretch it
    wet = mm[mm >= stops[0]].astype(np.float32)
    share = np.histogram(wet, bins=stops + [np.inf])[0] / wet.size
    assert share.max() < 0.18 and share[1:].min() > 0.05, share            # roughly equal-area bands
    # a uniform field still yields strictly increasing stops; tiny wet areas fall back to the fixed scale
    flat = Dsh.adaptive_stops(np.full(1000, 5.0), 10)
    assert all(a < b for a, b in zip(flat, flat[1:])), flat
    assert Dsh.adaptive_stops(np.zeros(1000), 10) == Dsh.ACC_STOPS_DASH


def test_dashboard():
    with tempfile.TemporaryDirectory() as tmp:
        cfgp = Path(tmp) / "c.toml"
        start = (datetime.now(timezone.utc) - timedelta(days=300)).strftime("%Y-%m-%d")
        cfgp.write_text(f'data_dir = "{tmp}"\n[weather]\nera5_start = "{start}"\nukv_start = "{start}"\n'
                        f'daily_call_budget = 100000\nhourly_call_budget = 100000\n'
                        '[[crags]]\nname="Ben Nevis"\nlat=56.801\nlon=-5.006\nelev_m=1150\naspect=20\narea="Lochaber"\n'
                        '[[crags]]\nname="Beinn a\' Bhuird"\nlat=57.095\nlon=-3.473\nelev_m=1000\naspect=20\n'
                        'area="Southern Cairngorms"\n')
        cfg = W.WeatherConfig(str(cfgp))
        srv = Dsh.make_server(cfg, "127.0.0.1", 0, radar_thread=False)
        base = f"http://127.0.0.1:{srv.server_address[1]}/"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # before any data exists: API says so instead of crashing
            code, body, _ = get(base, "api/board")
            assert code == 404 and b"no weather.sqlite" in body, (code, body)

            wdb = W.WeatherDB(cfg)
            W.fetch_all(cfg, wdb, OpenMeteo("test", session=FakeOM(), per_minute_weight=1e9))
            W.compute_features(cfg, wdb)
            (Path(tmp) / "weather_status.json").write_text(json.dumps(
                {"errors": 0, "skipped_budget": 0, "at_utc": datetime.now(timezone.utc).isoformat()}))

            # static page and whitelisted assets
            code, body, ctype = get(base, "")
            assert code == 200 and b"app.js" in body and ctype.startswith("text/html")
            for p in ("app.js", "app.css", "vendor/uPlot.iife.min.js", "vendor/uPlot.min.css"):
                code, body, _ = get(base, p)
                assert code == 200 and len(body) > 100, p
            for p in ("../config.toml", "vendor/LICENSE", "%2e%2e/rainarchive/weather.py", "weather.sqlite"):
                assert get(base, p)[0] == 404, p

            # board: 15 days for every crag, today's cell filled, grouped areas present
            code, body, _ = get(base, "api/board")
            b = json.loads(body)
            assert code == 200 and len(b["dates"]) == 15 and b["dates"][7] == b["today"], b["dates"]
            assert [c["name"] for c in b["crags"]] == ["Ben Nevis", "Beinn a' Bhuird"]
            nevis = b["crags"][0]
            assert nevis["area"] == "Lochaber" and nevis["days"][7] is not None
            assert nevis["days"][0]["refreezes"] == 1 and nevis["days"][0]["thaws"] == 1   # fake: one cycle a day

            # crag: hourly merged series with wind basis, daily features, factors
            code, body, _ = get(base, "api/crag/" + urllib.request.quote("beinn a' bhuird"))
            d = json.loads(body)
            assert code == 200 and d["crag"]["name"] == "Beinn a' Bhuird", body[:200]
            h = d["hourly"]
            assert len(h["time"]) >= 24 * 25 and len(set(map(len, h.values()))) == 1
            assert all(t1 - t0 == 3600 for t0, t1 in zip(h["time"], h["time"][1:]))
            assert set(h["w850"]) == {1} and abs(max(h["wind"]) - 18.0) < 0.1
            assert d["factors"]["era5"]["factor"] == 1.8
            assert len(d["daily"]["date"]) >= 25 and d["daily"]["date"][-1] > b["today"]
            # a long window reaching back before the fake UKV's 850 hPa data: calibrated fallback is used
            old = (datetime.now(timezone.utc) - timedelta(days=260)).strftime("%Y-%m-%d")
            code, body, _ = get(base, f"api/crag/Ben%20Nevis?from={old}")
            hl = json.loads(body)["hourly"]
            assert code == 200 and set(hl["w850"]) == {0, 1}
            assert all(abs(w - 18.0) < 0.1 for w in hl["wind"])   # 10 m (10 m/s) x 1.8 == 850 hPa (18)
            code, body, _ = get(base, "api/crag/Ben%20Nevis?from=2020-01-01&to=2020-01-03")
            assert code == 200 and json.loads(body)["hourly"]["time"] == []

            # errors
            assert get(base, "api/crag/Nowhere")[0] == 404
            assert get(base, "api/crag/Ben%20Nevis?from=2027-13-01")[0] == 400
            assert get(base, "api/crag/Ben%20Nevis?from=2020-01-01&to=2026-01-01")[0] == 400
            assert get(base, "api/nothing")[0] == 404

            # status
            code, body, _ = get(base, "api/status")
            s = json.loads(body)
            assert code == 200 and s["sources"]["ukv"]["crags"] == 2 and s["backfill"]["ukv"]["total"] >= 1
            assert s["wind_factors"]["Ben Nevis"]["ukv"]["factor"] == 1.8 and "radar" not in s
            assert get(base, "api/radar")[0] == 404

            # crag hourly rime/thaw series: fake T = -2 + 4 sin(...) peaks at +2 C, > 1 C for 5 h a day
            assert set(h["rime_thaw"]) == {0, 1} and len(h["rime_cum"]) == len(h["time"])

            # radar: 30 dBZ everywhere for 3 h, one frame missing in the middle hour
            store = Store(load_config(str(cfgp)))
            hr = (int(datetime.now(timezone.utc).timestamp()) // 3600 - 2) * 3600
            times = [hr - 7200 + 600 * i for i in range(18)]
            times.remove(hr - 3600 + 1200)
            grid = np.full(store.grid.shape, 30, np.uint8)
            for t in times:
                store.save_frame(t, grid, store.grid.nx * store.grid.ny, 0)
            radar = srv.dash.radar
            code, body, _ = get(base, "api/radar/accum?hours=1")
            assert code == 503 and b"warming" in body, (code, body)
            assert radar.refresh() == 17 and radar.refresh() == 0     # incremental
            code, body, _ = get(base, "api/radar")
            ri = json.loads(body)
            assert code == 200 and ri["latest"] == times[-1] and ri["frames"] == times[-17:]
            assert ri["bounds"]["west"] == -11.25 and len(ri["rate_cols"]) == len(ri["rate_stops"])
            rate = float(RATE_LUT[30])                                 # mm/h
            for hours, n_frames in ((1, 6), (2, 11), (3, 17)):
                code, body, _ = get(base, f"api/radar/accum?hours={hours}")
                a = json.loads(body)
                assert code == 200 and a["frames"] == n_frames and a["expected_frames"] == 6 * hours, a
                assert a["end"] == times[-1] + 600 and a["end"] - a["start"] == 3600 * hours, a
                assert abs(a["crags"]["Ben Nevis"] - rate * hours) < 0.06 * hours, (a, rate)   # gap filled; 0.1 mm rounding
                assert a["stops"] == Dsh.ACC_STOPS_DASH and not a["adaptive"]   # short windows: fixed scale
                code, png, ctype = get(base, a["png"])
                assert code == 200 and ctype == "image/png" and png[:4] == b"\x89PNG"
            code, png, ctype = get(base, f"api/radar/frame/{times[-1]}.png")
            assert code == 200 and ctype == "image/png" and png[:4] == b"\x89PNG"
            assert get(base, f"api/radar/frame/{times[-1] + 600}.png")[0] == 404
            assert get(base, "api/radar/frame/abc.png")[0] == 400
            for bad in ("0", "169", "x"):
                assert get(base, f"api/radar/accum?hours={bad}")[0] == 400, bad
            for p in ("vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css"):
                assert get(base, p)[0] == 200, p
            s = json.loads(get(base, "api/status")[1])

            # cache: same bytes until the weather status file changes
            assert get(base, "api/board")[1] == get(base, "api/board")[1]
            print(f"OK - dashboard served {len(h['time'])} hourly points, {len(d['daily']['date'])} days")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    test_adaptive_stops()
    test_dashboard()
