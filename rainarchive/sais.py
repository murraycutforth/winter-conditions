"""Scottish Avalanche Information Service (SAIS) forecast archive -> sais.sqlite (research labels).

    python3 -m rainarchive.sais -c config.toml calendar [--season 2023 ...]   # headline hazard per area/day
    python3 -m rainarchive.sais -c config.toml status

Source: https://www.sais.gov.uk/forecast-archive/?y=<season>&a=<area code>, one calendar page per
season (y = the year the season starts, 2007 = 2007/08) and area. Each day with a report has an icon
whose title gives the headline hazard and whose link gives the full report (report_id).

What the calendar hazard means: it is the FORECAST hazard in the report issued on that date, valid
18:00 on the date to 18:00 the next day (checked against report 11253, Northern Cairngorms
2023-12-05: icon Considerable = forecast Considerable; the same report's observed hazard was
Moderate). The observed hazard, avalanche problems, aspect/elevation rose and recorded avalanches
are only in the full reports (not fetched yet).

Content is (c) sportscotland Avalanche Information Service, with no open licence: personal research
use, fetched slowly, raw pages kept so nothing needs re-fetching. Ask SAIS before redistributing.

Storage (data_dir/sais.sqlite, WAL; raw pages in data_dir/sais_raw/calendar/):
    sais_calendar   (area, date) -> hazard text, level 1..5 (NULL for "Na"), report_id, season
    sais_fetch_log  (season, area_code) -> when fetched, days found, complete flag
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path

import requests

from .weather import WeatherConfig

log = logging.getLogger("rainarchive.sais")

BASE = "https://www.sais.gov.uk/forecast-archive/"
FIRST_SEASON = 2007                 # the searchable archive starts 11 April 2007 (2006/07 tail)
AREA_CODES = {2: "Northern Cairngorms", 3: "Lochaber", 4: "Glencoe", 5: "Southern Cairngorms",
              6: "Creag Meagaidh", 7: "Torridon"}
SLUG_TO_AREA = {"northern-cairngorms": "Northern Cairngorms", "lochaber": "Lochaber", "glencoe": "Glencoe",
                "southern-cairngorms": "Southern Cairngorms", "creag-meagaidh": "Creag Meagaidh",
                "torridon": "Torridon"}
LEVELS = {"low": 1, "moderate": 2, "considerable": 3, "high": 4, "very high": 5, "na": None}
REQUEST_INTERVAL_S = 3.0            # be gentle: a small public-service site
USER_AGENT = "rain-archive SAIS research scraper (personal, non-commercial; slow)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sais_calendar (
    area TEXT NOT NULL, date TEXT NOT NULL,    -- issue date (local); forecast valid 18:00 date -> 18:00 date+1
    hazard TEXT NOT NULL,                      -- as published: Low | Moderate | Considerable | High | Very High | Na
    level INTEGER,                             -- 1..5, NULL for Na
    report_id INTEGER, season INTEGER NOT NULL, fetched_at INTEGER NOT NULL,
    PRIMARY KEY (area, date));
CREATE TABLE IF NOT EXISTS sais_fetch_log (
    season INTEGER NOT NULL, area_code INTEGER NOT NULL, fetched_at INTEGER NOT NULL,
    n_days INTEGER NOT NULL, complete INTEGER NOT NULL,   -- complete = season over when fetched
    PRIMARY KEY (season, area_code));
"""

_MONTH_RE = re.compile(r"<h3[^>]*>\s*([A-Z][a-z]+)\s+(\d{4})\s*</h3>(.*?)(?=<h3|\Z)", re.S)
_DAY_RE = re.compile(r'<div class="sais_calendar__day[^"]*">\s*<span>(\d+)</span>\s*'
                     r'<a\s+title="([^"]*?)\s+avalanche hazard[^"]*"\s+href="([^"]+)"', re.S)
_MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                       "August", "September", "October", "November", "December"], 1)}


def parse_calendar(html: str) -> list[dict]:
    """All days with a report on one season/area calendar page."""
    out = []
    for month, year, body in _MONTH_RE.findall(html):
        if month not in _MONTHS:
            continue
        for day, title, href in _DAY_RE.findall(body):
            hazard = unescape(title).strip()
            key = hazard.lower()
            if key not in LEVELS:
                raise ValueError(f"unknown hazard {hazard!r} on {year}-{month}-{day}")
            m = re.search(r"sais\.gov\.uk/([a-z-]+)/\?report_id=(\d+)", href)
            if not m or m.group(1) not in SLUG_TO_AREA:
                raise ValueError(f"unexpected report link {href!r}")
            out.append({"date": date(int(year), _MONTHS[month], int(day)).isoformat(),
                        "hazard": hazard.title() if key != "na" else "Na", "level": LEVELS[key],
                        "area": SLUG_TO_AREA[m.group(1)], "report_id": int(m.group(2))})
    return out


def season_of(d: date) -> int:
    """Season label = the year it starts (Dec 2023 .. Apr 2024 -> 2023)."""
    return d.year if d.month >= 7 else d.year - 1


class SaisDB:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = data_dir / "sais_raw" / "calendar"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "sais.sqlite", timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)


class Fetcher:
    def __init__(self, session=None, interval_s: float = REQUEST_INTERVAL_S):
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.interval_s = interval_s
        self._next = 0.0

    def get(self, url: str, params: dict) -> str:
        wait = self._next - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(url, params=params, timeout=30)
        self._next = time.monotonic() + self.interval_s
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url} {params}")
        return r.text


def fetch_calendars(sdb: SaisDB, fetcher: Fetcher, seasons: list[int] | None = None,
                    today: date | None = None) -> dict:
    """Fetch season/area calendars. Finished seasons are fetched once; the current one every run."""
    today = today or datetime.now(timezone.utc).date()
    current = season_of(today)
    seasons = seasons or list(range(FIRST_SEASON, current + 1))
    summary = {"pages": 0, "skipped": 0, "days": 0, "errors": 0}
    for y in seasons:
        for code, area in AREA_CODES.items():
            row = sdb.db.execute("SELECT complete FROM sais_fetch_log WHERE season=? AND area_code=?",
                                 (y, code)).fetchone()
            if row and row[0]:
                summary["skipped"] += 1
                continue
            try:
                html = fetcher.get(BASE, {"y": y, "a": code})
                days = parse_calendar(html)
            except Exception as e:  # noqa: BLE001
                summary["errors"] += 1
                log.warning("season %d %s: %s", y, area, e)
                continue
            (sdb.raw_dir / f"{y}_a{code}.html").write_text(html)
            wrong = [d for d in days if d["area"] != area]
            if wrong:
                log.warning("season %d a=%d: %d days link to another area (%s)", y, code, len(wrong), wrong[0]["area"])
            now = int(time.time())
            with sdb.db:
                sdb.db.executemany(
                    "INSERT OR REPLACE INTO sais_calendar VALUES (?,?,?,?,?,?,?)",
                    [(d["area"], d["date"], d["hazard"], d["level"], d["report_id"], y, now) for d in days])
                sdb.db.execute("INSERT OR REPLACE INTO sais_fetch_log VALUES (?,?,?,?,?)",
                               (y, code, now, len(days), int(y < current)))
            summary["pages"] += 1
            summary["days"] += len(days)
            log.info("season %d/%02d %-20s %3d days", y, (y + 1) % 100, area, len(days))
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rainarchive.sais")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calendar", help="fetch headline hazard calendars")
    c.add_argument("--season", type=int, action="append", help="season start year (repeatable); default all")
    sub.add_parser("status")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    sdb = SaisDB(WeatherConfig(a.config).data_dir)
    if a.cmd == "calendar":
        print(fetch_calendars(sdb, Fetcher(), a.season))
    elif a.cmd == "status":
        rows = sdb.db.execute("SELECT area, COUNT(*), MIN(date), MAX(date), "
                              "SUM(level=1), SUM(level=2), SUM(level=3), SUM(level=4), SUM(level=5), "
                              "SUM(level IS NULL) FROM sais_calendar GROUP BY area ORDER BY area").fetchall()
        print(f"{'area':20s} {'days':>5s}  first       last        low  mod  con high vhigh  na")
        for r in rows:
            print(f"{r[0]:20s} {r[1]:5d}  {r[2]}  {r[3]}  " + " ".join(f"{v:4d}" for v in r[4:]))
        n = sdb.db.execute("SELECT COUNT(*), SUM(complete) FROM sais_fetch_log").fetchone()
        print(f"calendar pages fetched: {n[0]} ({n[1] or 0} for finished seasons)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
