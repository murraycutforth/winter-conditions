"""Offline test of the crag weather extension against a fake Open-Meteo.

Run from the project folder:  python3 -m tests.test_weather_offline
"""
from __future__ import annotations

import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rainarchive import weather as W
from rainarchive.crags import Crag
from rainarchive.features import daily_features, rime_since_thaw, wet_bulb
from rainarchive.openmeteo import OpenMeteo


def test_features():
    # 3 days: frozen (-3 C), a thaw day (+3 C from 10:00-16:00 then -3 C), frozen with snow + NW wind
    t0 = int(datetime(2027, 1, 10, tzinfo=timezone.utc).timestamp())
    hours = []
    for i in range(72):
        day, hr = divmod(i, 24)
        t = 3.0 if (day == 1 and 10 <= hr < 16) else -3.0
        snowy = day == 2
        hours.append(dict(time=t0 + 3600 * i, t=t, rh=99.0 if snowy else 70.0, precip=1.0 if snowy else 0.0,
                          snowfall=0.7 if snowy else 0.0, wind=12.0, wdir=200.0 if snowy else 10.0, src="ukv"))
    f = daily_features(hours, aspect=20.0, tz="UTC")
    assert [x["date"] for x in f] == ["2027-01-10", "2027-01-11", "2027-01-12"]
    d0, d1, d2 = f
    assert d0["refreezes"] == 0 and d1["thaws"] == 1 and d1["refreezes"] == 1, (d0, d1)
    assert d1["pdh"] == 18 and d1["days_since_thaw"] == 0 and d2["days_since_thaw"] == 1
    assert d0["days_since_thaw"] is None            # no thaw seen yet in the series
    assert d2["frozen_precip_mm"] == 24 and d2["rain_mm"] == 0
    # wind from 200 deg is from behind a face with aspect 20 -> lee loading, not rime on the face
    assert d2["lee_loading_mm"] == 24 and d2["rime_hrs"] == 24 and d2["rime_face_hrs"] == 0
    assert abs(d2["swe_model_mm"] - 24) < 1e-9 and d2["frozen_precip_7d"] == 24
    assert abs(wet_bulb(20, 50) - 13.7) < 0.3   # Stull's published check value


def test_rime_since_thaw():
    # 3 rime hours, a 4 h warm spell (not sustained: needs > 4 h), 1 cold hour, a 5 h thaw,
    # 2 rime hours, a gap, then 3 + 3 warm hours split by the gap (neither run counts)
    t0 = int(datetime(2027, 1, 10, tzinfo=timezone.utc).timestamp())
    rime = dict(t=-3.0, rh=99.0, wind=10.0, wdir=20.0)
    warm = dict(t=2.0, rh=99.0, wind=10.0, wdir=20.0)
    seq = [rime] * 3 + [warm] * 4 + [dict(rime, rh=50.0)] + [warm] * 5 + [rime] * 2
    hours = [dict(h, time=t0 + 3600 * i) for i, h in enumerate(seq)]
    t1 = hours[-1]["time"] + 3 * 3600
    hours += [dict(warm, time=t1 + 3600 * i) for i in range(3)] + [dict(warm, time=t1 + 3600 * (i + 4)) for i in range(3)]
    r = rime_since_thaw(hours, aspect=20.0)
    assert r["cum"][:8] == [1, 2, 3, 3, 3, 3, 3, 3], r["cum"]
    assert r["thaw"][:13] == [0] * 8 + [1] * 5 and r["cum"][8:13] == [0] * 5, r
    assert r["cum"][13:15] == [1, 2] and r["rime"][13:15] == [1, 1]
    assert r["thaw"][15:] == [0] * 6 and r["cum"][15:] == [2] * 6, r
    # wind from behind the face is rime, but not rime on the face
    assert rime_since_thaw([dict(rime, time=t0, wdir=200.0)], aspect=20.0)["cum"] == [0]


class Resp:
    def __init__(self, js, status=200):
        self._js, self.status_code, self.text = js, status, str(js)

    def json(self):
        return self._js


class FakeOM:
    """Hourly T = -2 + 4*sin(2*pi*hour/24) at every crag: one thaw and one refreeze per day.

    Wind is 10 m/s at 10 m and 18 m/s at 850 hPa (true ridge factor 1.8). ERA5 has no 850 hPa, and
    UKV has none before NO_850_BEFORE, like the real archive before ~2024-08."""
    NO_850_BEFORE = int((datetime.now(timezone.utc) - timedelta(days=200)).timestamp())

    def __init__(self):
        self.headers, self.calls = {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if "start_date" in params:
            a = datetime.fromisoformat(params["start_date"]).replace(tzinfo=timezone.utc)
            b = datetime.fromisoformat(params["end_date"]).replace(tzinfo=timezone.utc) + timedelta(days=1)
        else:
            now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            a, b = now - timedelta(days=1), now + timedelta(days=params["forecast_days"])
        times = list(range(int(a.timestamp()), int(b.timestamp()), 3600))
        hv = params["hourly"].split(",")
        h = {"time": times}
        for v in hv:
            h[v] = [(-2 + 4 * math.sin(2 * math.pi * ((t // 3600) % 24) / 24)) if v.startswith("temperature")
                    else 90.0 if v.startswith("relative") else 18.0 if v == "wind_speed_850hPa" else 10.0 if "speed" in v else 0.2 if v == "precipitation"
                    else 330.0 if "direction" in v else 0.0 for t in times]
        if "archive" in url:
            h.pop("wind_speed_850hPa", None)
        elif "historical-forecast" in url and "wind_speed_850hPa" in h:
            h["wind_speed_850hPa"] = [None if t < self.NO_850_BEFORE else 18.0 for t in times]
        return Resp({"hourly": h})


def test_pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        cfgp = Path(tmp) / "c.toml"
        start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
        cfgp.write_text(f'data_dir = "{tmp}"\n[weather]\nera5_start = "{start}"\nukv_start = "{start}"\n'
                        f'daily_call_budget = 100000\nhourly_call_budget = 100000\n'
                        '[[crags]]\nname="Ben Nevis"\nlat=56.801\nlon=-5.006\nelev_m=1150\naspect=20\n'
                        '[[crags]]\nname="Lochnagar"\nlat=56.962\nlon=-3.24\nelev_m=1000\naspect=30\n')
        cfg = W.WeatherConfig(str(cfgp))
        wdb = W.WeatherDB(cfg)
        fake = FakeOM()
        om = OpenMeteo("test", session=fake, per_minute_weight=1e9)
        s = W.fetch_all(cfg, wdb, om)
        assert s["errors"] == 0 and s["fc"] > 0 and s["ukv"] > 0 and s["era5"] > 0, s
        ukv_calls = [p for u, p in fake.calls if "historical-forecast" in u]
        assert all(p["models"] == "ukmo_uk_deterministic_2km" and p["elevation"] in (1150, 1000) for p in ukv_calls)
        # same-day second pass: only the forecasts are refreshed
        n_before = len(fake.calls)
        W.fetch_all(cfg, wdb, om)
        assert len(fake.calls) - n_before == 2, fake.calls[n_before:]
        # next day: forecasts plus a short ukv and era5 tail per crag
        wdb.db.execute("UPDATE fetch_log SET fetched_to = date(fetched_to, '-1 day') "
                       "WHERE (source, chunk) IN (SELECT source, MAX(chunk) FROM fetch_log GROUP BY source)")
        wdb.db.commit()
        n_before = len(fake.calls)
        W.fetch_all(cfg, wdb, om)
        tail = fake.calls[n_before:]
        assert len(tail) == 2 * 3, len(tail)
        spans = [(p["start_date"], p["end_date"]) for u, p in tail if "start_date" in p]
        assert all((datetime.fromisoformat(b) - datetime.fromisoformat(a)).days <= 3 for a, b in spans), spans
        n = W.compute_features(cfg, wdb)
        assert n > 0
        rows = wdb.db.execute("SELECT date, refreezes, thaws, basis, frozen_precip_mm FROM crag_daily "
                              "WHERE source='best' AND crag='Ben Nevis' ORDER BY date").fetchall()
        mid = rows[len(rows) // 2]
        assert mid[1] == 1 and mid[2] == 1 and mid[3] == "ukv", mid
        assert rows[-1][0] > datetime.now(timezone.utc).strftime("%Y-%m-%d")   # forecast days present
        # ridge wind: calibrated 10 m -> 850 hPa factor recovers 1.8 for ukv and era5
        calib = dict(wdb.db.execute("SELECT source, factor FROM crag_calib WHERE crag='Ben Nevis'").fetchall())
        assert abs(calib["ukv"] - 1.8) < 1e-9 and abs(calib["era5"] - 1.8) < 1e-9, calib
        old_day = (datetime.now(timezone.utc) - timedelta(days=300)).strftime("%Y-%m-%d")
        new_day = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        for src, day in (("best", old_day), ("best", new_day), ("era5", old_day)):
            wm = wdb.db.execute("SELECT wind_mean FROM crag_daily WHERE source=? AND crag='Ben Nevis' AND date=?",
                                (src, day)).fetchone()[0]
            assert abs(wm - 18.0) < 1e-6, (src, day, wm)   # 850 hPa where present, 10 m x 1.8 elsewhere
        # unchanged factors: incremental recompute only; a moved factor forces a full recompute
        assert W.calibrate_wind(wdb, "Ben Nevis")[1] is False
        wdb.db.execute("UPDATE crag_calib SET factor=1.0 WHERE crag='Ben Nevis' AND source='era5'")
        wdb.db.execute("UPDATE crag_daily SET wind_mean=-1 WHERE crag='Ben Nevis' AND source='era5' AND date=?",
                       (old_day,))
        wdb.db.commit()
        W.compute_features(cfg, wdb)
        wm = wdb.db.execute("SELECT wind_mean FROM crag_daily WHERE source='era5' AND crag='Ben Nevis' AND date=?",
                            (old_day,)).fetchone()[0]
        assert abs(wm - 18.0) < 1e-6, wm
        # the budget stops a pass
        cfg.daily_budget = 0
        wdb.db.execute("DELETE FROM fetch_log")
        s3 = W.fetch_all(cfg, wdb, om)
        assert s3["skipped_budget"] > 0 and s3["ukv"] == 0, s3
        # CLI smoke
        assert W.main(["-c", str(cfgp), "log", "ben nevis", "2027-02-14", "--rating", "3", "--type", "ice"]) == 0
        assert W.main(["-c", str(cfgp), "show", "Ben Nevis", "--days", "3"]) == 0
        assert W.main(["-c", str(cfgp), "status"]) == 0
        print(f"OK - {len(fake.calls)} fake API calls, {n} feature rows")


if __name__ == "__main__":
    test_features()
    test_rime_since_thaw()
    test_pipeline()
