"""Crag weather archive + winter-conditions features.

    python3 -m rainarchive.weather -c config.toml update        # fetch (budgeted) + recompute features
    python3 -m rainarchive.weather -c config.toml status        # backfill progress, API usage
    python3 -m rainarchive.weather -c config.toml show "Ben Nevis" [--days 21]
    python3 -m rainarchive.weather -c config.toml log "Ben Nevis" 2027-02-14 --rating 3 --type ice \
            --route "Point Five" --notes "fat, some queues"
    python3 -m rainarchive.weather -c config.toml features --all  # full recompute
    python3 -m rainarchive.weather -c config.toml refetch "Lochnagar"   # after moving a crag

Everything is stored in data_dir/weather.sqlite (WAL mode; safe to read while updating):
    crags         the configured venues
    wx_hourly     hourly series per (source, crag): ukv | era5 | fc   (UTC unix time)
    crag_daily    daily features per (source, crag, local date): 'best' and 'era5'
    observations  your own conditions log - the labels for any predictive model
    fetch_log     backfill bookkeeping;  api_usage  weighted Open-Meteo calls per hour
    crag_calib    10 m -> ridge (850 hPa) wind factor per (crag, source), for hours without 850 hPa
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import sys
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .crags import Crag, load_crags
from .features import DAILY_COLUMNS, daily_features
from .openmeteo import SOURCES, COLUMNS, OpenMeteo, RateLimited, call_weight

log = logging.getLogger("rainarchive.weather")

WX_COLS = list(COLUMNS.values())

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS crags (
    name TEXT PRIMARY KEY, lat REAL, lon REAL, elev_m REAL, aspect REAL, area TEXT);
CREATE TABLE IF NOT EXISTS wx_hourly (
    source TEXT NOT NULL, crag TEXT NOT NULL, time INTEGER NOT NULL,
    {", ".join(c + " REAL" for c in WX_COLS)},
    PRIMARY KEY (source, crag, time)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS crag_daily (
    source TEXT NOT NULL, crag TEXT NOT NULL, date TEXT NOT NULL,
    {", ".join(c + (" TEXT" if c == "basis" else " REAL") for c in DAILY_COLUMNS)},
    radar_mm REAL,
    PRIMARY KEY (source, crag, date));
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, crag TEXT NOT NULL, route TEXT,
    rating INTEGER,            -- 0 not in / 1 poor / 2 climbable / 3 good / 4 excellent
    type TEXT,                 -- ice | mixed | snow | rime | neve | other
    notes TEXT, source TEXT DEFAULT 'me', created_at INTEGER);
CREATE TABLE IF NOT EXISTS fetch_log (
    source TEXT, crag TEXT, chunk TEXT, fetched_to TEXT, fetched_at INTEGER,
    PRIMARY KEY (source, crag, chunk));
CREATE TABLE IF NOT EXISTS api_usage (hour TEXT PRIMARY KEY, weight REAL);
CREATE TABLE IF NOT EXISTS crag_calib (     -- ridge wind ~= factor * ws10 where ws850 is missing
    crag TEXT NOT NULL, source TEXT NOT NULL, factor REAL, n_hours INTEGER, PRIMARY KEY (crag, source));
CREATE TABLE IF NOT EXISTS radar_daily (    -- from the rain archive, local days, 3x3 pixel mean
    crag TEXT NOT NULL, date TEXT NOT NULL, mm REAL, n_frames INTEGER, PRIMARY KEY (crag, date));
CREATE VIEW IF NOT EXISTS conditions AS         -- best features joined to your observations
    SELECT d.*, o.rating, o.type AS obs_type, o.route, o.notes
    FROM crag_daily d LEFT JOIN observations o ON o.crag = d.crag AND o.date = d.date
    WHERE d.source = 'best';
"""


class WeatherConfig:
    def __init__(self, path: str | None):
        raw = {}
        if path:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        w = raw.get("weather", {})
        self.data_dir = Path(raw.get("data_dir", "~/rain-archive-data")).expanduser()
        self.era5_start = w.get("era5_start", "2000-01-01")
        self.ukv_start = w.get("ukv_start", SOURCES["ukv"]["start"])
        self.timezone = w.get("timezone", "Europe/London")
        self.daily_budget = float(w.get("daily_call_budget", 7000))
        self.hourly_budget = float(w.get("hourly_call_budget", 3500))
        self.user_agent = raw.get("fetch", {}).get("user_agent", "rain-archive/1.0 (personal Raspberry Pi archive)")
        self.crags: list[Crag] = load_crags(path)


class WeatherDB:
    def __init__(self, cfg: WeatherConfig):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.db = sqlite3.connect(cfg.data_dir / "weather.sqlite", timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        with self.db:
            for c in cfg.crags:
                self.db.execute("INSERT OR REPLACE INTO crags VALUES (?,?,?,?,?,?)",
                                (c.name, c.lat, c.lon, c.elev_m, c.aspect, c.area))

    # --- API budget ----------------------------------------------------------------------------
    def usage(self) -> tuple[float, float]:
        now = datetime.now(timezone.utc)
        hour = now.strftime("%Y-%m-%dT%H")
        day = now.strftime("%Y-%m-%d")
        h = self.db.execute("SELECT COALESCE(SUM(weight),0) FROM api_usage WHERE hour=?", (hour,)).fetchone()[0]
        d = self.db.execute("SELECT COALESCE(SUM(weight),0) FROM api_usage WHERE hour LIKE ?", (day + "%",)).fetchone()[0]
        return h, d

    def can_spend(self, w: float) -> bool:
        h, d = self.usage()
        return h + w <= self.cfg.hourly_budget and d + w <= self.cfg.daily_budget

    def spend(self, w: float) -> None:
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        with self.db:
            self.db.execute("INSERT INTO api_usage VALUES (?,?) ON CONFLICT(hour) DO UPDATE SET weight=weight+?",
                            (hour, w, w))

    # --- hourly data ---------------------------------------------------------------------------
    def upsert_hourly(self, source: str, crag: str, data: dict) -> int:
        times = data["time"]
        rows = []
        for i, t in enumerate(times):
            vals = [(data[c][i] if data.get(c) is not None else None) for c in WX_COLS]
            if all(v is None for v in vals):
                continue
            rows.append((source, crag, t, *vals))
        with self.db:
            self.db.executemany(
                f"INSERT OR REPLACE INTO wx_hourly VALUES ({','.join('?' * (3 + len(WX_COLS)))})", rows)
        return len(rows)


def _d(s: str) -> date:
    return date.fromisoformat(s)


def plan_chunks(source: str, start: str, end: date) -> list[tuple[str, date, date]]:
    """Calendar-year chunks from start to end, most recent first."""
    s = _d(start)
    out = []
    for y in range(end.year, s.year - 1, -1):
        a, b = max(date(y, 1, 1), s), min(date(y, 12, 31), end)
        if a <= b:
            out.append((str(y), a, b))
    return out


def fetch_all(cfg: WeatherConfig, wdb: WeatherDB, om: OpenMeteo) -> dict:
    """One budgeted pass: forecast for every crag, then recent + backfill chunks for ukv and era5."""
    today = datetime.now(timezone.utc).date()
    summary = {"fc": 0, "ukv": 0, "era5": 0, "skipped_budget": 0, "errors": 0}
    # 1. forecasts (cheap, always)
    for c in cfg.crags:
        try:
            data, w = om.hourly("fc", c.lat, c.lon, c.elev_m)
            wdb.spend(w)
            with wdb.db:
                wdb.db.execute("DELETE FROM wx_hourly WHERE source='fc' AND crag=?", (c.name,))
            summary["fc"] += wdb.upsert_hourly("fc", c.name, data)
        except RateLimited as e:
            log.warning("rate limited by Open-Meteo (%s); stopping this pass", e)
            return summary
        except Exception as e:  # noqa: BLE001
            summary["errors"] += 1
            log.warning("forecast for %s failed: %s", c.name, e)
    # 2. history: newest chunks first so recent data is always current, then backfill backwards
    targets = [("ukv", cfg.ukv_start, today - timedelta(days=1)),
               ("era5", cfg.era5_start, today - timedelta(days=6))]
    work = []
    for source, start, end in targets:
        for chunk, a, b in plan_chunks(source, start, end):
            for c in cfg.crags:
                row = wdb.db.execute("SELECT fetched_to FROM fetch_log WHERE source=? AND crag=? AND chunk=?",
                                     (source, c.name, chunk)).fetchone()
                done_to = _d(row[0]) if row else None
                if done_to and done_to >= b:
                    continue
                a2 = max(a, done_to - timedelta(days=2)) if done_to else a
                work.append((b.year, source, chunk, c, a2, b))
    work.sort(key=lambda x: (-x[0], x[1] != "ukv"))
    for _, source, chunk, c, a, b in work:
        n_vars = len(SOURCES[source]["vars"])
        w_est = call_weight((b - a).days + 1, n_vars)
        if not wdb.can_spend(w_est):
            summary["skipped_budget"] += 1
            continue
        try:
            data, w = om.hourly(source, c.lat, c.lon, c.elev_m, a.isoformat(), b.isoformat())
        except RateLimited as e:
            log.warning("rate limited by Open-Meteo (%s); stopping this pass", e)
            break
        except Exception as e:  # noqa: BLE001
            summary["errors"] += 1
            log.warning("%s %s %s failed: %s", source, c.name, chunk, e)
            continue
        wdb.spend(w)
        n = wdb.upsert_hourly(source, c.name, data)
        summary[source] += n
        with wdb.db:
            wdb.db.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                           (source, c.name, chunk, b.isoformat(), int(time.time())))
        log.info("%-4s %-22s %s..%s  %d hours", source, c.name, a, b, n)
    return summary


# --- ridge-wind calibration ---------------------------------------------------------------------
# Where a source has no 850 hPa wind (all ERA5; UKV before ~2024-08), ridge wind = factor x 10 m wind.
# factor = median of ws850 (UKV, else fc, same hour) / that source's ws10, over hours with
# ws10 >= CALIB_MIN_WS10 (light winds give noisy ratios). The median is robust to the
# decoupled-inversion hours where 10 m is calm under a strong free-air wind.
CALIB_MIN_WS10 = 2.0        # m/s
CALIB_MIN_HOURS = 200       # fewer overlapping hours -> CALIB_DEFAULT
CALIB_DEFAULT = 1.6
CALIB_RECOMPUTE_TOL = 0.05  # a factor moving by more than this (relative) forces a full recompute


def calibrate_wind(wdb: WeatherDB, crag: str) -> tuple[dict[str, float], bool]:
    """Recompute and store the 10 m -> ridge wind factors for one crag.

    Returns ({source: factor}, changed) where changed means some stored factor was missing or moved by
    more than CALIB_RECOMPUTE_TOL, so features built with the old factors are stale."""
    ref = dict(wdb.db.execute(      # ukv sorts last so it wins over fc for the same hour
        "SELECT time, ws850 FROM wx_hourly WHERE crag=? AND source IN ('fc','ukv') AND ws850 IS NOT NULL "
        "ORDER BY source='ukv'", (crag,)).fetchall())
    old = dict(wdb.db.execute("SELECT source, factor FROM crag_calib WHERE crag=?", (crag,)).fetchall())
    scales, changed = {}, False
    for src in SOURCES:
        rows = wdb.db.execute("SELECT time, ws10 FROM wx_hourly WHERE crag=? AND source=? AND ws10>=?",
                              (crag, src, CALIB_MIN_WS10)).fetchall()
        ratios = [ref[t] / ws for t, ws in rows if t in ref]
        factor = statistics.median(ratios) if len(ratios) >= CALIB_MIN_HOURS else CALIB_DEFAULT
        scales[src] = factor
        if src not in old or old[src] is None or abs(factor / old[src] - 1.0) > CALIB_RECOMPUTE_TOL:
            changed = True
            with wdb.db:
                wdb.db.execute("INSERT OR REPLACE INTO crag_calib VALUES (?,?,?,?)",
                               (crag, src, factor, len(ratios)))
        else:
            scales[src] = old[src]   # keep the stored factor while drift is within tolerance
    return scales, changed


# --- features ------------------------------------------------------------------------------------
def _hours(wdb: WeatherDB, crag: str, sources: list[str], t0: int,
           scales: dict[str, float] | None = None) -> list[dict]:
    """Merged hourly series; earlier entries in `sources` take priority for the same hour.

    Ridge wind is ws850 when present, else ws10 x scales[source] (1.0 if no factor is given)."""
    rows = wdb.db.execute(
        f"SELECT source, time, t2m, rh2m, precip, snowfall, ws10, wd10, ws850, wd850 FROM wx_hourly "
        f"WHERE crag=? AND time>=? AND source IN ({','.join('?' * len(sources))})",
        (crag, t0, *sources)).fetchall()
    rank = {s: i for i, s in enumerate(sources)}
    best: dict[int, tuple] = {}
    for r in rows:
        cur = best.get(r[1])
        if cur is None or rank[r[0]] < rank[cur[0]]:
            best[r[1]] = r
    out = []
    for t in sorted(best):
        src, _, t2m, rh, p, sf, ws10, wd10, ws850, wd850 = best[t]
        if ws850 is not None:
            wind, wdir = ws850, wd850
        else:
            wind, wdir = (ws10 * (scales or {}).get(src, 1.0) if ws10 is not None else None), wd10
        out.append(dict(time=t, t=t2m, rh=rh, precip=p, snowfall=sf, wind=wind, wdir=wdir, src=src,
                        w850=ws850 is not None))
    return out


def update_radar_daily(cfg: WeatherConfig, wdb: WeatherDB) -> int:
    """Fill radar_daily for finished local days that have radar frames and no entry yet.

    Each frame is decoded once and sampled at every crag (3x3 pixel mean, ~2 km box).
    """
    if not (cfg.data_dir / "radar.sqlite").exists():
        return 0
    try:
        import numpy as np
        from zoneinfo import ZoneInfo
        from .query import Archive
        a = Archive(cfg.data_dir)
    except Exception as e:  # noqa: BLE001
        log.warning("radar archive unreadable: %s", e)
        return 0
    zone = ZoneInfo(cfg.timezone)
    pix = {}
    for c in cfg.crags:
        try:
            pix[c.name] = a.grid.latlon_to_pixel(c.lat, c.lon)
        except ValueError:
            pass
    first = a.db.execute("SELECT MIN(time) FROM frames WHERE path IS NOT NULL").fetchone()[0]
    if first is None or not pix:
        return 0
    d = datetime.fromtimestamp(first, zone).date()
    today = datetime.now(zone).date()
    n = 0
    while d < today:
        key = d.isoformat()
        have = {r[0] for r in wdb.db.execute("SELECT crag FROM radar_daily WHERE date=?", (key,))}
        if set(pix) - have:
            start = datetime(d.year, d.month, d.day, tzinfo=zone)
            times = a.frame_times(start, start + timedelta(days=1))
            sums = {k: [] for k in pix}
            for t in times:
                rate = a.rate(t)
                for k, (r, c) in pix.items():
                    win = rate[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2]
                    if not np.isnan(win).all():
                        sums[k].append(float(np.nanmean(win)))
            with wdb.db:
                for k, v in sums.items():
                    # need most of the day's 144 frames for a meaningful total; mean rate x 24 h fills gaps
                    mm = sum(v) / len(v) * 24.0 if len(v) >= 100 else None
                    wdb.db.execute("INSERT OR REPLACE INTO radar_daily VALUES (?,?,?,?)", (k, key, mm, len(v)))
            n += 1
        d += timedelta(days=1)
    return n


def compute_features(cfg: WeatherConfig, wdb: WeatherDB, full: bool = False,
                     write_days: int = 60, warmup_days: int = 150) -> int:
    """Recompute daily features. Normally only the last `write_days` (plus forecast days); a crag/
    source is fully recomputed when backfilled hourly data now reaches earlier than its features, or
    when the crag's ridge-wind factors changed."""
    n = 0
    now = datetime.now(timezone.utc)
    for c in cfg.crags:
        scales, scales_changed = calibrate_wind(wdb, c.name)
        if scales_changed:
            log.info("wind factors for %s: %s", c.name, {k: round(v, 2) for k, v in scales.items()})
        for fsource, sources in (("best", ["ukv", "fc", "era5"]), ("era5", ["era5"])):
            first_wx = wdb.db.execute(
                f"SELECT MIN(time) FROM wx_hourly WHERE crag=? AND source IN ({','.join('?' * len(sources))})",
                (c.name, *sources)).fetchone()[0]
            if first_wx is None:
                continue
            first_feat = wdb.db.execute("SELECT MIN(date) FROM crag_daily WHERE source=? AND crag=?",
                                        (fsource, c.name)).fetchone()[0]
            first_wx_day = datetime.fromtimestamp(first_wx, timezone.utc).strftime("%Y-%m-%d")
            this_full = full or scales_changed or first_feat is None or first_wx_day < (
                date.fromisoformat(first_feat) - timedelta(days=2)).isoformat()
            t0 = 0 if this_full else int((now - timedelta(days=write_days + warmup_days)).timestamp())
            feats = daily_features(_hours(wdb, c.name, sources, t0, scales), c.aspect, cfg.timezone)
            cutoff = "" if this_full else (now - timedelta(days=write_days)).strftime("%Y-%m-%d")
            feats = [f for f in feats if f["date"] >= cutoff and f.get("n_hours", 0) >= 18]
            radar = dict(wdb.db.execute("SELECT date, mm FROM radar_daily WHERE crag=?", (c.name,)).fetchall()) \
                if fsource == "best" else {}
            rows = [(fsource, c.name, f["date"], *[f.get(k) for k in DAILY_COLUMNS], radar.get(f["date"]))
                    for f in feats]
            with wdb.db:
                if fsource == "best":   # future days change with each forecast
                    wdb.db.execute("DELETE FROM crag_daily WHERE source='best' AND crag=? AND date>?",
                                   (c.name, now.strftime("%Y-%m-%d")))
                wdb.db.executemany(
                    f"INSERT OR REPLACE INTO crag_daily VALUES ({','.join('?' * (4 + len(DAILY_COLUMNS)))})", rows)
            n += len(rows)
    return n


# --- CLI -----------------------------------------------------------------------------------------
SHOW_COLS = ["date", "basis", "tmin", "tmax", "refreezes", "thaws", "days_since_thaw", "frozen_precip_mm",
             "rain_mm", "rime_face_hrs", "lee_loading_mm", "swe_model_mm", "radar_mm"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rainarchive.weather")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update")
    sub.add_parser("status")
    f = sub.add_parser("features")
    f.add_argument("--all", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("crag")
    s.add_argument("--days", type=int, default=21)
    rf = sub.add_parser("refetch", help="forget a crag's stored weather so it is re-downloaded")
    rf.add_argument("crags", nargs="+")
    lg = sub.add_parser("log")
    lg.add_argument("crag")
    lg.add_argument("date")
    lg.add_argument("--rating", type=int, required=True, help="0 not in, 1 poor, 2 climbable, 3 good, 4 excellent")
    lg.add_argument("--type", default=None, help="ice | mixed | snow | rime | neve | other")
    lg.add_argument("--route", default=None)
    lg.add_argument("--notes", default=None)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    cfg = WeatherConfig(a.config)
    wdb = WeatherDB(cfg)
    names = {c.name.lower(): c.name for c in cfg.crags}

    if a.cmd == "update":
        om = OpenMeteo(cfg.user_agent)
        summary = fetch_all(cfg, wdb, om)
        summary["radar_days"] = update_radar_daily(cfg, wdb)
        summary["feature_rows"] = compute_features(cfg, wdb)
        h, d = wdb.usage()
        summary["api_weight_hour"], summary["api_weight_today"] = round(h), round(d)
        (cfg.data_dir / "weather_status.json").write_text(json.dumps(
            {**summary, "at_utc": datetime.now(timezone.utc).isoformat()}, indent=2))
        log.info("update done: %s", summary)
    elif a.cmd == "features":
        update_radar_daily(cfg, wdb)
        print(compute_features(cfg, wdb, full=a.all), "rows")
    elif a.cmd == "status":
        h, d = wdb.usage()
        print(f"Open-Meteo usage: {h:.0f} this hour, {d:.0f} today (budget {cfg.daily_budget:.0f}/day)")
        for src in ("ukv", "era5", "fc"):
            r = wdb.db.execute("SELECT COUNT(DISTINCT crag), COUNT(*), MIN(time), MAX(time) FROM wx_hourly "
                               "WHERE source=?", (src,)).fetchone()
            span = (f"{datetime.fromtimestamp(r[2], timezone.utc):%Y-%m-%d} .. "
                    f"{datetime.fromtimestamp(r[3], timezone.utc):%Y-%m-%d %H:%M}") if r[1] else "none yet"
            print(f"  {src:5s} {r[0]:2d} crags  {r[1]:>9,d} hours  {span}")
        todo = []
        today = datetime.now(timezone.utc).date()
        for source, start, end in (("ukv", cfg.ukv_start, today - timedelta(days=1)),
                                   ("era5", cfg.era5_start, today - timedelta(days=6))):
            total = len(plan_chunks(source, start, end)) * len(cfg.crags)
            done = wdb.db.execute("SELECT COUNT(*) FROM fetch_log WHERE source=?", (source,)).fetchone()[0]
            todo.append(f"{source} {done}/{total}")
        print("  backfill chunks (crag-years):", ", ".join(todo))
        first850 = wdb.db.execute("SELECT MIN(time) FROM wx_hourly WHERE source='ukv' AND ws850 IS NOT NULL").fetchone()[0]
        print("  850 hPa wind available from:",
              f"{datetime.fromtimestamp(first850, timezone.utc):%Y-%m-%d %H:%M} (ukv)" if first850 else "none yet")
        for src, lo, hi, med_n in wdb.db.execute(
                "SELECT source, MIN(factor), MAX(factor), MIN(n_hours) FROM crag_calib GROUP BY source ORDER BY source"):
            print(f"  10 m -> ridge wind factor {src:5s} x{lo:.2f} .. x{hi:.2f}  (min {med_n} calibration hours)")
        print("  observations logged:", wdb.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
    elif a.cmd == "show":
        crag = names.get(a.crag.lower())
        if not crag:
            print("unknown crag; choose from:", ", ".join(c.name for c in cfg.crags))
            return 1
        since = (datetime.now(timezone.utc) - timedelta(days=a.days)).strftime("%Y-%m-%d")
        rows = wdb.db.execute(f"SELECT {','.join(SHOW_COLS)} FROM crag_daily WHERE source='best' AND crag=? "
                              f"AND date>=? ORDER BY date", (crag, since)).fetchall()
        hdr = ["date", "src", "Tmin", "Tmax", "refrz", "thaw", "dSinceThaw", "snowmm", "rainmm",
               "rimeFace", "loadmm", "SWEmm", "radarmm"]
        print(" ".join(f"{h:>10s}" for h in hdr))
        for r in rows:
            print(" ".join(f"{('' if v is None else (f'{v:.1f}' if isinstance(v, float) else str(v))):>10s}"
                           for v in r))
    elif a.cmd == "refetch":
        for name in a.crags:
            crag = names.get(name.lower())
            if not crag:
                print(f"unknown crag {name!r}; choose from:", ", ".join(c.name for c in cfg.crags))
                return 1
            with wdb.db:
                for t in ("wx_hourly", "fetch_log", "crag_daily", "radar_daily"):
                    col = "crag"
                    wdb.db.execute(f"DELETE FROM {t} WHERE {col}=?", (crag,))
            print(f"cleared {crag}; it will be re-downloaded on the next update")
    elif a.cmd == "log":
        crag = names.get(a.crag.lower())
        if not crag:
            print("unknown crag; choose from:", ", ".join(c.name for c in cfg.crags))
            return 1
        date.fromisoformat(a.date)
        with wdb.db:
            wdb.db.execute("INSERT INTO observations (date, crag, route, rating, type, notes, created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (a.date, crag, a.route, a.rating, a.type, a.notes, int(time.time())))
        print(f"logged {crag} {a.date} rating {a.rating}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
