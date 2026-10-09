"""On-disk archive: PNG grids + an SQLite catalogue.

Layout under data_dir:
    radar.sqlite                         catalogue, per-frame stats, site time series, daily index
    grid.json                            grid definition (same as the 'grid' row in the meta table)
    status.json                          collector health, rewritten after every poll
    frames/YYYY/MM/DD/YYYYMMDDTHHMMZ.png 8-bit greyscale, pixel = dBZ code (0 dry, 255 no data)
    daily/YYYY/YYYY-MM-DD.png            16-bit greyscale, pixel = daily total in 0.01 mm (65535 no data)

Files are written to a temp name and renamed, so a power cut never leaves a half-written grid.
SQLite runs in WAL mode so a dashboard can read while the collector writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Config, Site
from .decode import NO_DATA, dbz_to_rate
from .grid import Grid

DAILY_NODATA = 65535
DAILY_SCALE = 100.0  # stored value = mm * 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS frames (
    time         INTEGER PRIMARY KEY,   -- radar valid time, unix seconds UTC
    path         TEXT,                  -- relative to data_dir; NULL once pruned
    fetched_at   INTEGER NOT NULL,
    tiles_ok     INTEGER NOT NULL,
    tiles_total  INTEGER NOT NULL,
    valid_frac   REAL NOT NULL,         -- fraction of grid with data
    wet_frac     REAL NOT NULL,         -- fraction of valid area with any echo
    mean_rate    REAL NOT NULL,         -- area-weighted mean rain rate over valid area, mm/h
    max_dbz      INTEGER NOT NULL,
    unmatched_px INTEGER NOT NULL       -- colours resolved by nearest match (quality flag)
);
CREATE TABLE IF NOT EXISTS sites (
    name TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, row INTEGER NOT NULL, col INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS site_obs (
    time      INTEGER NOT NULL,
    site      TEXT NOT NULL,
    dbz       INTEGER NOT NULL,         -- code at the site pixel (255 = no data)
    rate      REAL,                     -- mm/h at the site pixel (NULL = no data)
    rate_3x3  REAL,                     -- mean mm/h over the 3x3 pixels around the site (~2 km box)
    PRIMARY KEY (time, site)
);
CREATE TABLE IF NOT EXISTS daily (
    date        TEXT PRIMARY KEY,       -- UTC date YYYY-MM-DD
    path        TEXT NOT NULL,
    n_frames    INTEGER NOT NULL,       -- frames present (144 = complete day)
    mean_mm     REAL NOT NULL,          -- area-weighted mean total over valid pixels
    max_mm      REAL NOT NULL,
    built_at    INTEGER NOT NULL
);
-- Convenience views for dashboards ---------------------------------------------------------
CREATE VIEW IF NOT EXISTS frames_iso AS
    SELECT datetime(time, 'unixepoch') AS time_utc, * FROM frames;
CREATE VIEW IF NOT EXISTS site_hourly AS
    SELECT site, strftime('%Y-%m-%d %H:00', time, 'unixepoch') AS hour_utc,
           AVG(rate) AS mm, COUNT(rate) AS n_frames      -- mean mm/h over the hour = mm in that hour
    FROM site_obs GROUP BY site, hour_utc;
CREATE VIEW IF NOT EXISTS site_daily AS
    SELECT site, date(time, 'unixepoch') AS date_utc,
           SUM(rate) / 6.0 AS mm_observed,       -- sum over frames present (10 min each)
           AVG(rate) * 24.0 AS mm_gapfilled,     -- missing frames filled with the day's mean rate
           COUNT(rate) AS n_frames               -- 144 = complete day
    FROM site_obs GROUP BY site, date_utc;
CREATE INDEX IF NOT EXISTS site_obs_site_time ON site_obs (site, time);
"""


def _atomic_png(img: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    img.save(tmp, format="PNG", optimize=True)
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dest)


def _atomic_json(obj, dest: Path) -> None:
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, dest)


def utc(t: int) -> datetime:
    return datetime.fromtimestamp(t, tz=timezone.utc)


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.data_dir
        self.grid: Grid = cfg.grid
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "radar.sqlite", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._check_grid()
        self._sync_sites(cfg.sites)
        self._area = self.grid.pixel_area_km2()[:, None]

    # --- setup ---------------------------------------------------------------------------------
    def _check_grid(self) -> None:
        g = json.dumps(self.grid.to_dict(), sort_keys=True)
        row = self.db.execute("SELECT value FROM meta WHERE key='grid'").fetchone()
        if row and json.loads(row[0]) != json.loads(g):
            raise RuntimeError(
                "The configured area/zoom differs from the grid this archive was created with. "
                "Use a new data_dir for a different area.")
        if not row:
            with self.db:
                self.db.execute("INSERT INTO meta VALUES ('grid', ?)", (g,))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('encoding', ?)", (json.dumps({
                    "frames": "uint8 PNG; 0 = no echo, 5..95 = dBZ, 255 = no data",
                    "daily": "uint16 PNG; value/100 = mm in the UTC day, 65535 = no data",
                    "rain_rate": "Marshall-Palmer Z=200R^1.6, dBZ capped at 55",
                    "source": "RainViewer Weather Maps API, Universal Blue scheme, decoded to dBZ",
                    "frame_interval_s": 600,
                }),))
        _atomic_json(self.grid.to_dict(), self.root / "grid.json")

    def _sync_sites(self, sites: list[Site]) -> None:
        with self.db:
            for s in sites:
                r, c = self.grid.latlon_to_pixel(s.lat, s.lon)
                self.db.execute("INSERT OR REPLACE INTO sites VALUES (?,?,?,?,?)", (s.name, s.lat, s.lon, r, c))

    # --- frames --------------------------------------------------------------------------------
    def have_frame(self, t: int) -> bool:
        return self.db.execute("SELECT 1 FROM frames WHERE time=?", (t,)).fetchone() is not None

    @staticmethod
    def frame_relpath(t: int) -> str:
        d = utc(t)
        return f"frames/{d:%Y/%m/%d}/{d:%Y%m%dT%H%MZ}.png"

    def save_frame(self, t: int, dbz: np.ndarray, tiles_ok: int, unmatched: int) -> dict:
        rel = self.frame_relpath(t)
        _atomic_png(Image.fromarray(dbz), self.root / rel)
        valid = dbz != NO_DATA
        rate = dbz_to_rate(dbz)
        area = np.broadcast_to(self._area, dbz.shape)
        va = float(area[valid].sum())
        stats = dict(
            valid_frac=float(valid.mean()),
            wet_frac=float(area[valid & (dbz > 0)].sum() / va) if va else 0.0,
            mean_rate=float((rate[valid] * area[valid]).sum() / va) if va else 0.0,
            max_dbz=int(dbz[valid].max()) if valid.any() else 0,
        )
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO frames VALUES (?,?,?,?,?,?,?,?,?,?)",
                (t, rel, int(time.time()), tiles_ok, self.grid.nx * self.grid.ny, stats["valid_frac"],
                 stats["wet_frac"], stats["mean_rate"], stats["max_dbz"], unmatched))
            for name, r, c in self.db.execute("SELECT name, row, col FROM sites").fetchall():
                code = int(dbz[r, c])
                win = rate[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2]
                pt = None if code == NO_DATA else float(rate[r, c])
                box = None if np.isnan(win).all() else float(np.nanmean(win))
                self.db.execute("INSERT OR REPLACE INTO site_obs VALUES (?,?,?,?,?)", (t, name, code, pt, box))
        return stats

    def load_frame(self, t: int) -> np.ndarray:
        row = self.db.execute("SELECT path FROM frames WHERE time=?", (t,)).fetchone()
        if not row or not row[0]:
            raise KeyError(f"no stored grid for {utc(t)}")
        return np.asarray(Image.open(self.root / row[0]))

    # --- daily totals --------------------------------------------------------------------------
    def days_needing_build(self, now: int) -> list[str]:
        """UTC dates that are complete (ended >30 min ago) with frames but no/old daily grid."""
        rows = self.db.execute("""
            SELECT date(f.time,'unixepoch') AS d, COUNT(*) AS n FROM frames f
            WHERE f.path IS NOT NULL GROUP BY d""").fetchall()
        built = dict(self.db.execute("SELECT date, n_frames FROM daily").fetchall())
        out = []
        for d, n in rows:
            end = int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400
            if end + 1800 <= now and built.get(d) != n:
                out.append(d)
        return sorted(out)

    def build_daily(self, date: str) -> dict:
        start = int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        times = [r[0] for r in self.db.execute(
            "SELECT time FROM frames WHERE time>=? AND time<? AND path IS NOT NULL ORDER BY time",
            (start, start + 86400))]
        acc = np.zeros(self.grid.shape, dtype=np.float64)
        cnt = np.zeros(self.grid.shape, dtype=np.uint16)
        for t in times:
            rate = dbz_to_rate(self.load_frame(t))
            ok = ~np.isnan(rate)
            acc[ok] += rate[ok]
            cnt += ok
        with np.errstate(invalid="ignore", divide="ignore"):
            mm = np.where(cnt > 0, acc / cnt * 24.0, np.nan)   # mean rate x 24 h (fills gaps)
        enc = np.where(np.isnan(mm), DAILY_NODATA, np.clip(np.round(mm * DAILY_SCALE), 0, DAILY_NODATA - 1))
        rel = f"daily/{date[:4]}/{date}.png"
        _atomic_png(Image.fromarray(enc.astype(np.uint16)), self.root / rel)
        valid = ~np.isnan(mm)
        area = np.broadcast_to(self._area, mm.shape)
        mean_mm = float((mm[valid] * area[valid]).sum() / area[valid].sum()) if valid.any() else 0.0
        max_mm = float(mm[valid].max()) if valid.any() else 0.0
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?)",
                            (date, rel, len(times), mean_mm, max_mm, int(time.time())))
        return {"date": date, "n_frames": len(times), "mean_mm": mean_mm, "max_mm": max_mm}

    # --- housekeeping --------------------------------------------------------------------------
    def prune_frames(self, keep_days: int, now: int) -> int:
        """Delete raw frame PNGs older than keep_days (their stats, site series and daily totals stay)."""
        if keep_days <= 0:
            return 0
        cutoff = now - keep_days * 86400
        built = {r[0] for r in self.db.execute("SELECT date FROM daily")}
        rows = self.db.execute("SELECT time, path FROM frames WHERE time<? AND path IS NOT NULL",
                               (cutoff,)).fetchall()
        n = 0
        with self.db:
            for t, rel in rows:
                if utc(t).strftime("%Y-%m-%d") not in built:
                    continue  # never delete frames whose daily total hasn't been built
                (self.root / rel).unlink(missing_ok=True)
                self.db.execute("UPDATE frames SET path=NULL WHERE time=?", (t,))
                n += 1
        return n

    def write_status(self, **kw) -> None:
        n, first, last = self.db.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM frames").fetchone()
        kw.update(frames_stored=n,
                  first_frame_utc=utc(first).isoformat() if first else None,
                  last_frame_utc=utc(last).isoformat() if last else None,
                  written_at_utc=datetime.now(timezone.utc).isoformat())
        _atomic_json(kw, self.root / "status.json")
