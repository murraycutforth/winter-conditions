"""Offline self-test: fakes RainViewer, runs the collector pipeline, checks the stored numbers.

Run from the project folder:  python3 -m tests.test_offline
"""
from __future__ import annotations

import io
import math
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from rainarchive.config import Config, DEFAULT_SITES
from rainarchive.collector import poll_once
from rainarchive.decode import UNIVERSAL_BLUE, RATE_LUT, NO_DATA, rgba_to_dbz
from rainarchive.fetcher import RainViewer
from rainarchive.query import Archive
from rainarchive.store import Store


def _png(dbz: int | None) -> bytes:
    arr = np.zeros((256, 256, 4), np.uint8)
    if dbz is not None:  # rain over the western half of the tile only
        h = UNIVERSAL_BLUE[dbz]
        arr[:, :128] = [int(h[i:i + 2], 16) for i in (0, 2, 4, 6)]
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "PNG")
    return b.getvalue()


class FakeResp:
    def __init__(self, status=200, content=b"", js=None):
        self.status_code, self.content, self._js = status, content, js

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code), response=self)


class FakeSession:
    """Western half of tile (62,39) - which contains Ben Nevis - rains at 30 dBZ; Cairn Gorm and
    Edinburgh (eastern half) and everything else are dry.
    Tile (60,36) always fails in the newest frame, to exercise partial-frame handling."""

    def __init__(self, times):
        self.times = times
        self.headers = {}
        self.requests = 0

    def get(self, url, timeout=None):
        self.requests += 1
        if url.endswith("weather-maps.json"):
            return FakeResp(js={"host": "https://tiles.example", "radar": {
                "past": [{"time": t, "path": f"/v2/radar/{t}"} for t in self.times]}})
        parts = url.split("/")
        t, z, x, y = int(parts[5]), int(parts[7]), int(parts[8]), int(parts[9])
        if (x, y) == (60, 36) and t == self.times[-1]:
            return FakeResp(status=500)
        return FakeResp(content=_png(30 if (x, y) == (62, 39) else None))


def main():
    # decoder sanity: every table colour decodes to its own dBZ
    for d, h in UNIVERSAL_BLUE.items():
        px = np.array([[[int(h[i:i + 2], 16) for i in (0, 2, 4, 6)]]], np.uint8)
        got, _ = rgba_to_dbz(px)
        assert got[0, 0] == d or (d == 65 and got[0, 0] == 65), (d, got)
    # a slightly-off colour resolves by nearest match
    got, um = rgba_to_dbz(np.array([[[0x00, 0x56, 0x89, 255]]], np.uint8))
    assert got[0, 0] == 30 and um == 1

    with tempfile.TemporaryDirectory() as tmp:
        day0 = 1790000000 // 86400 * 86400 - 3 * 86400       # a UTC midnight in the past
        times = [day0 + 36000 + 600 * i for i in range(13)]  # 10:00..12:00 UTC that day
        cfg = Config(data_dir=Path(tmp), sites=DEFAULT_SITES, min_request_interval_s=0.0, retries=0)
        store = Store(cfg)
        sess = FakeSession(times)
        rv = RainViewer(cfg, session=sess)
        g = store.grid
        assert (g.zoom, g.x0, g.y0, g.nx, g.ny) == (7, 60, 36, 4, 5), g

        # newest frame is 'recent' relative to itself, so its partial download is retried later
        s1 = poll_once(cfg, store, rv)
        assert s1["saved"] == 12 and s1["partial"] == 1, s1
        s2 = poll_once(cfg, store, rv)
        assert s2["new"] == 1 and s2["saved"] == 0, s2   # still partial -> still waiting

        a = Archive(tmp)
        rate30 = float(RATE_LUT[30])
        assert math.isclose(rate30, (10 ** 3 / 200) ** (1 / 1.6), rel_tol=1e-6)
        ser = a.site_series("Ben Nevis", times[0], times[-1] + 1)
        assert len(ser) == 12 and all(math.isclose(v, rate30, rel_tol=1e-6) for _, v in ser), ser
        for dry in ("Edinburgh", "Cairn Gorm"):
            ser_d = a.site_series(dry, times[0], times[-1] + 1)
            assert len(ser_d) == 12 and all(v == 0 for _, v in ser_d), (dry, ser_d)

        # two-hour accumulation at Ben Nevis = rate * 2 h (12 frames, gap-filled for the missing one)
        mm, cov = a.accumulate(times[0] - 600 + 600, times[0] + 7200)
        r, c = g.latlon_to_pixel(56.7969, -5.0036)
        assert math.isclose(float(mm[r, c]), rate30 * 2, rel_tol=1e-4), mm[r, c]

        # daily total: mean rate over available frames x 24 h
        days = store.days_needing_build(day0 + 5 * 86400)
        assert days, "expected a daily build"
        info = store.build_daily(days[0])
        d = a.daily(days[0])
        assert abs(float(d[r, c]) - rate30 * 24) < 0.02, d[r, c]
        assert float(np.nanmax(d)) == float(d[r, c])

        # frames round-trip exactly
        f = a.dbz(times[0])
        assert f.dtype == np.uint8 and f.shape == (1280, 1024) and f[r, c] == 30
        assert (f != NO_DATA).all()
        size = sum(p.stat().st_size for p in Path(tmp, "frames").rglob("*.png")) / 12
        print(f"OK - {sess.requests} fake requests, {info}, ~{size / 1024:.1f} KB per stored frame")


if __name__ == "__main__":
    main()
