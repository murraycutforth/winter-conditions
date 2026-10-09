"""Read-only API for dashboards and notebooks.

    from rainarchive.query import Archive
    a = Archive("~/rain-archive-data")
    a.frame_times("2026-10-01", "2026-10-02")        # list of unix times
    a.rate(t)                                          # (H, W) mm/h, NaN = no data
    a.accumulate("2026-10-01", "2026-10-08")          # (H, W) mm over the week + coverage
    a.daily("2026-10-01")                              # (H, W) mm for one UTC day (precomputed)
    a.site_series("Ben Nevis", "2026-10-01", "2026-11-01")
    lat, lon = a.grid.pixel_centres()                  # coordinates for plotting

Only needs numpy + Pillow + the standard library; never writes to the archive.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .decode import dbz_to_rate
from .grid import Grid
from .store import DAILY_NODATA, DAILY_SCALE


def _ts(x) -> int:
    """Accept unix seconds, datetime, or ISO string ('2026-10-01' or '2026-10-01T12:00'); UTC."""
    if isinstance(x, (int, float)):
        return int(x)
    if isinstance(x, str):
        x = datetime.fromisoformat(x)
    if x.tzinfo is None:
        x = x.replace(tzinfo=timezone.utc)
    return int(x.timestamp())


class Archive:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).expanduser()
        self.db = sqlite3.connect(f"file:{self.root / 'radar.sqlite'}?mode=ro", uri=True, timeout=30)
        g = json.loads(self.db.execute("SELECT value FROM meta WHERE key='grid'").fetchone()[0])
        self.grid = Grid(g["zoom"], g["x0"], g["y0"], g["nx"], g["ny"])

    # --- catalogue ---------------------------------------------------------------------------
    def frame_times(self, start, end) -> list[int]:
        return [r[0] for r in self.db.execute(
            "SELECT time FROM frames WHERE time>=? AND time<? AND path IS NOT NULL ORDER BY time",
            (_ts(start), _ts(end)))]

    def frame_stats(self, start, end) -> list[dict]:
        cur = self.db.execute("SELECT * FROM frames WHERE time>=? AND time<? ORDER BY time", (_ts(start), _ts(end)))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur]

    def sites(self) -> dict[str, dict]:
        return {n: {"lat": la, "lon": lo, "row": r, "col": c}
                for n, la, lo, r, c in self.db.execute("SELECT * FROM sites")}

    # --- grids -------------------------------------------------------------------------------
    def dbz(self, t) -> np.ndarray:
        row = self.db.execute("SELECT path FROM frames WHERE time=?", (_ts(t),)).fetchone()
        if not row or not row[0]:
            raise KeyError(f"no stored grid at {t}")
        return np.asarray(Image.open(self.root / row[0]))

    def rate(self, t) -> np.ndarray:
        return dbz_to_rate(self.dbz(t))

    def accumulate(self, start, end) -> tuple[np.ndarray, np.ndarray]:
        """Total mm between start and end from raw frames.

        Gaps are filled with each pixel's mean rate over the period. Returns (mm, coverage) where
        coverage is the fraction of expected 10-minute frames that had data at each pixel.
        """
        s, e = _ts(start), _ts(end)
        acc = np.zeros(self.grid.shape, np.float64)
        cnt = np.zeros(self.grid.shape, np.uint32)
        for t in self.frame_times(s, e):
            r = self.rate(t)
            ok = ~np.isnan(r)
            acc[ok] += r[ok]
            cnt += ok
        hours = (e - s) / 3600
        with np.errstate(invalid="ignore", divide="ignore"):
            mm = np.where(cnt > 0, acc / cnt * hours, np.nan)
        return mm.astype(np.float32), (cnt / max(1, round((e - s) / 600))).astype(np.float32)

    def daily(self, date: str) -> np.ndarray:
        row = self.db.execute("SELECT path FROM daily WHERE date=?", (date,)).fetchone()
        if not row:
            raise KeyError(f"no daily total for {date}")
        v = np.asarray(Image.open(self.root / row[0])).astype(np.float32)
        v[v == DAILY_NODATA] = np.nan
        return v / DAILY_SCALE

    def daily_sum(self, start_date: str, end_date: str) -> np.ndarray:
        """Sum of precomputed daily totals for start_date..end_date inclusive (fast: no raw frames)."""
        dates = [r[0] for r in self.db.execute(
            "SELECT date FROM daily WHERE date>=? AND date<=? ORDER BY date", (start_date, end_date))]
        out = np.zeros(self.grid.shape, np.float32)
        for d in dates:
            out += np.nan_to_num(self.daily(d))
        return out

    # --- point series ------------------------------------------------------------------------
    def site_series(self, site: str, start, end, field: str = "rate") -> list[tuple[int, float | None]]:
        if field not in ("rate", "rate_3x3", "dbz"):
            raise ValueError(field)
        return self.db.execute(
            f"SELECT time, {field} FROM site_obs WHERE site=? AND time>=? AND time<? ORDER BY time",
            (site, _ts(start), _ts(end))).fetchall()

    def point_series(self, lat: float, lon: float, start, end) -> list[tuple[int, float]]:
        """Rain rate at any lat/lon (reads raw frames, so slower than site_series)."""
        r, c = self.grid.latlon_to_pixel(lat, lon)
        return [(t, float(self.rate(t)[r, c])) for t in self.frame_times(start, end)]
