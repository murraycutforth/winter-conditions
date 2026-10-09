"""Read-only crag conditions dashboard: a small JSON API plus one static page (web/dashboard/).

    python3 -m rainarchive.dashboard -c config.toml [--host 0.0.0.0] [--port 8080]

Standard library only. Opens weather.sqlite / radar.sqlite read-only, so it is safe alongside the
collector and the 3-hourly weather timer. Responses are cached until weather_status.json or the radar
status.json changes (i.e. until new data arrives) or the local date rolls over.

Endpoints (all GET, JSON unless .png):
    /api/board                  days -7..+7 for every crag: the landing-page grid
    /api/crag/<name>[?from=YYYY-MM-DD&to=YYYY-MM-DD]
                                hourly merged series + daily features, default -21..+7 local days
    /api/status                 data freshness, backfill progress, wind factors, API use, radar health
    /api/radar                  grid bounds, recent frame times, crag points, colour scales
    /api/radar/frame/<t>.png    one radar frame (unix time t) in RainViewer colours
    /api/radar/accum?hours=N    accumulation over the last N hours (1..168): window, coverage, per-crag mm
    /api/radar/accum.png?hours=N[&latest=t]   the same accumulation as an image

Radar accumulations come from an in-memory cache of hourly totals at half the archive's
resolution (~1.3 km), filled by a background thread (about a minute at start-up on a Pi 4, then
one frame per 10 minutes), so any window up to a week is a sum of at most 169 small arrays.

No authentication: serve it on the LAN or over Tailscale only, never port-forwarded.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import io
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import numpy as np

from . import weather as W
from .decode import UNIVERSAL_BLUE
from .features import THAW_RESET_HOURS, THAW_RESET_T, rime_since_thaw
from .render import ACC_COLS, render_frame, render_mm

log = logging.getLogger("rainarchive.dashboard")

WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "dashboard"
STATIC = {   # url path -> (file under WEB_DIR, content type); nothing else is served
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/vendor/uPlot.iife.min.js": ("vendor/uPlot.iife.min.js", "text/javascript; charset=utf-8"),
    "/vendor/uPlot.min.css": ("vendor/uPlot.min.css", "text/css; charset=utf-8"),
    "/vendor/leaflet/leaflet.js": ("vendor/leaflet/leaflet.js", "text/javascript; charset=utf-8"),
    "/vendor/leaflet/leaflet.css": ("vendor/leaflet/leaflet.css", "text/css; charset=utf-8"),
}

BOARD_BACK, BOARD_FWD = 7, 7
CRAG_BACK, CRAG_FWD = 21, 7
MAX_SPAN_DAYS = 400          # longest /api/crag window (keeps responses phone-sized)
SOURCES_BEST = ["ukv", "fc", "era5"]   # same priority as the 'best' features
BOARD_COLS = ["tmin", "tmax", "refreezes", "thaws", "frozen_precip_mm", "rain_mm", "rime_face_hrs",
              "lee_loading_mm", "swe_model_mm", "days_since_thaw", "refreeze_7d", "basis", "radar_mm"]
CRAG_DAILY_COLS = ["date", "basis", "tmin", "tmax", "pdh", "fdh", "refreezes", "thaws", "precip_mm",
                   "frozen_precip_mm", "rain_mm", "rime_face_hrs", "rime_face_hrs_7d", "lee_loading_mm",
                   "lee_loading_7d", "swe_model_mm", "days_since_thaw", "refreeze_7d", "wind_mean",
                   "wdir_mean", "radar_mm"]
RIME_WARMUP_DAYS = 60        # history used to find the last sustained thaw before the window

RADAR_KEEP_HOURS = 7 * 24    # longest accumulation offered
RADAR_FRAME_HOURS = 6        # recent frames offered for the instantaneous view
RADAR_REFRESH_S = 60
RADAR_DS = 2                 # accumulation grid = archive grid downsampled by this factor
RADAR_PNG_CACHE = 64
ACC_STOPS_DASH = [0.2, 0.5, 1, 2, 5, 10, 20, 40, 80, 150]
ACC_COLS_DASH = ACC_COLS + ["#f2a33a"]
ACC_FIXED_MAX_HOURS = 3      # windows up to this use the fixed scale above; longer ones adapt
ACC_MIN_STOP = 0.2           # mm; less than this is drawn as dry (transparent)
_NICE = [1, 1.2, 1.5, 2, 2.5, 3, 3.5, 4, 5, 6, 7, 8, 9]   # x 10^k: readable legend values


def _nice_round(x: float) -> float:
    """Nearest (in log space) nice number to x."""
    k = 10.0 ** np.floor(np.log10(x))
    cands = [m * k for m in _NICE + [10]]
    return round(min(cands, key=lambda c: abs(np.log(c / x))), 6)


def _nice_next(x: float) -> float:
    """The smallest nice number above x."""
    k = 10.0 ** np.floor(np.log10(x))
    return round(next(m * k for m in _NICE + [10, 12] if m * k > x * (1 + 1e-9)), 6)


def adaptive_stops(mm: np.ndarray, n: int) -> list[float]:
    """n colour thresholds giving roughly equal wet area per colour (histogram equalisation).

    The first stop is ACC_MIN_STOP; the others are quantiles at 1/n .. (n-1)/n of the pixels with
    at least that much rain, rounded to nice numbers and forced strictly increasing. Quantiles,
    not the maximum, so a few cluttered pixels can't stretch the scale."""
    wet = mm[mm >= ACC_MIN_STOP]
    if wet.size < 100:
        return list(ACC_STOPS_DASH)
    qs = np.quantile(wet.astype(np.float32), np.arange(1, n) / n)
    stops = [ACC_MIN_STOP]
    for q in qs:
        v = _nice_round(max(float(q), ACC_MIN_STOP))
        stops.append(v if v > stops[-1] else _nice_next(stops[-1]))
    return stops
RATE_LEGEND = [0.1, 0.5, 1, 2, 4, 8, 16, 32]   # mm/h shown in the instantaneous legend


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


def _r(v, nd=2):
    return round(v, nd) if isinstance(v, float) else v


class Dashboard:
    """The data side: one read-only connection per call, so it is safe from server threads."""

    def __init__(self, cfg: W.WeatherConfig):
        self.cfg = cfg
        self.zone = ZoneInfo(cfg.timezone)
        self.crags = {c.name: c for c in cfg.crags}
        self.radar = RadarCache(cfg.data_dir, cfg.crags)
        self._cache: dict[str, bytes] = {}
        self._stamp = None
        self._lock = threading.Lock()

    # --- plumbing ------------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        p = self.cfg.data_dir / "weather.sqlite"
        if not p.exists():
            raise NotFound("no weather.sqlite yet")
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)

    def _today(self) -> date:
        return datetime.now(self.zone).date()

    def _local_midnight(self, d: date) -> int:
        return int(datetime(d.year, d.month, d.day, tzinfo=self.zone).timestamp())

    def _data_stamp(self):
        out = [self._today().isoformat()]
        for name in ("weather_status.json", "status.json"):
            try:
                out.append((self.cfg.data_dir / name).stat().st_mtime_ns)
            except FileNotFoundError:
                out.append(None)
        return tuple(out)

    def cached(self, key: str, fn) -> bytes:
        stamp = self._data_stamp()
        with self._lock:
            if stamp != self._stamp:
                self._cache.clear()
                self._stamp = stamp
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        body = json.dumps(fn(), separators=(",", ":")).encode()
        with self._lock:
            if len(self._cache) > 200:       # odd custom windows shouldn't grow memory unbounded
                self._cache.clear()
            self._cache[key] = body
        return body

    @staticmethod
    def _factors(db: sqlite3.Connection, crag: str | None = None) -> dict:
        try:
            q = "SELECT crag, source, factor, n_hours FROM crag_calib" + (" WHERE crag=?" if crag else "")
            rows = db.execute(q, (crag,) if crag else ()).fetchall()
        except sqlite3.OperationalError:     # database written before the calibration existed
            return {}
        out: dict = {}
        for c, s, f, n in rows:
            out.setdefault(c, {})[s] = {"factor": _r(f), "n_hours": n}
        return out.get(crag, {}) if crag else out

    # --- endpoints -----------------------------------------------------------------------------
    def board(self) -> dict:
        today = self._today()
        dates = [(today + timedelta(days=i)).isoformat() for i in range(-BOARD_BACK, BOARD_FWD + 1)]
        db = self._db()
        try:
            rows = db.execute(f"SELECT crag, date, {','.join(BOARD_COLS)} FROM crag_daily WHERE source='best' "
                              f"AND date>=? AND date<=?", (dates[0], dates[-1])).fetchall()
        finally:
            db.close()
        by_crag: dict = {}
        for crag, d, *vals in rows:
            by_crag.setdefault(crag, {})[d] = {k: _r(v, 1) for k, v in zip(BOARD_COLS, vals)}
        crags = []
        for c in self.cfg.crags:
            days = by_crag.get(c.name, {})
            latest = days.get(today.isoformat()) or days.get((today - timedelta(days=1)).isoformat()) or {}
            crags.append({"name": c.name, "area": c.area, "elev_m": c.elev_m, "aspect": c.aspect,
                          "days_since_thaw": latest.get("days_since_thaw"),
                          "refreeze_7d": latest.get("refreeze_7d"),
                          "days": [days.get(d) for d in dates]})
        return {"today": today.isoformat(), "dates": dates, "crags": crags}

    def crag(self, name: str, d0: str | None, d1: str | None) -> dict:
        c = self.crags.get(name) or {k.lower(): v for k, v in self.crags.items()}.get(name.lower())
        if c is None:
            raise NotFound(f"unknown crag {name!r}")
        today = self._today()
        try:
            a = date.fromisoformat(d0) if d0 else today - timedelta(days=CRAG_BACK)
            b = date.fromisoformat(d1) if d1 else today + timedelta(days=CRAG_FWD)
        except ValueError as e:
            raise BadRequest(str(e)) from None
        if b < a or (b - a).days > MAX_SPAN_DAYS:
            raise BadRequest(f"window must be 0..{MAX_SPAN_DAYS} days")
        t0, t1 = self._local_midnight(a), self._local_midnight(b + timedelta(days=1))
        db = self._db()
        try:
            factors = self._factors(db, c.name)
            scales = {s: v["factor"] for s, v in factors.items()}
            hours_all = W._hours(SimpleNamespace(db=db), c.name, SOURCES_BEST,
                                 t0 - RIME_WARMUP_DAYS * 86400, scales)
            daily = db.execute(f"SELECT {','.join(CRAG_DAILY_COLS)} FROM crag_daily WHERE source='best' "
                               f"AND crag=? AND date>=? AND date<=? ORDER BY date",
                               (c.name, a.isoformat(), b.isoformat())).fetchall()
        finally:
            db.close()
        rime = rime_since_thaw(hours_all, c.aspect)
        keep = [i for i, h in enumerate(hours_all) if t0 <= h["time"] < t1]
        hours = [hours_all[i] for i in keep]
        hourly = {k: [_r(h[k], 1) for h in hours] for k in ("time", "t", "rh", "precip", "snowfall", "wind", "wdir")}
        hourly["w850"] = [1 if h["w850"] else 0 for h in hours]
        hourly["src"] = [h["src"] for h in hours]
        for k in ("rime", "thaw", "cum"):
            hourly["rime_" + k] = [rime[k][i] for i in keep]
        return {
            "crag": {"name": c.name, "area": c.area, "lat": c.lat, "lon": c.lon, "elev_m": c.elev_m,
                     "aspect": c.aspect},
            "from": a.isoformat(), "to": b.isoformat(), "now": int(time.time()),
            "tz": self.cfg.timezone, "factors": factors,
            "thaw_rule": {"t": THAW_RESET_T, "hours": THAW_RESET_HOURS},
            "hourly": hourly,
            "daily": {k: [_r(r[i], 2) for r in daily] for i, k in enumerate(CRAG_DAILY_COLS)},
        }

    def status(self) -> dict:
        def read_json(name):
            try:
                p = self.cfg.data_dir / name
                return {**json.loads(p.read_text()), "file_mtime_utc": int(p.stat().st_mtime)}
            except (FileNotFoundError, ValueError):
                return None

        out: dict = {"now": int(time.time()), "weather_update": read_json("weather_status.json"),
                     "radar_collector": read_json("status.json")}
        db = self._db()
        try:
            latest = {}
            for src in ("ukv", "era5", "fc"):
                per = {c.name: db.execute("SELECT MIN(time), MAX(time) FROM wx_hourly WHERE source=? AND crag=?",
                                          (src, c.name)).fetchone() for c in self.cfg.crags}
                maxes = [v[1] for v in per.values() if v[1] is not None]
                mins = [v[0] for v in per.values() if v[0] is not None]
                latest[src] = {"crags": len(maxes), "first": min(mins) if mins else None,
                               "last_min": min(maxes) if maxes else None,   # the most lagging crag
                               "last_max": max(maxes) if maxes else None}
            out["sources"] = latest
            today = datetime.now(timezone.utc).date()
            backfill = {}
            for source, start, end in (("ukv", self.cfg.ukv_start, today - timedelta(days=1)),
                                       ("era5", self.cfg.era5_start, today - timedelta(days=6))):
                total = len(W.plan_chunks(source, start, end)) * len(self.cfg.crags)
                done = db.execute("SELECT COUNT(*) FROM fetch_log WHERE source=?", (source,)).fetchone()[0]
                backfill[source] = {"done": done, "total": total}
            out["backfill"] = backfill
            out["wind_factors"] = self._factors(db)
            out["api_weight_today"] = _r(db.execute(
                "SELECT COALESCE(SUM(weight),0) FROM api_usage WHERE hour LIKE ?",
                (today.isoformat() + "%",)).fetchone()[0], 0)
            out["daily_budget"] = self.cfg.daily_budget
            out["observations"] = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        finally:
            db.close()
        rp = self.cfg.data_dir / "radar.sqlite"
        if rp.exists():
            r = sqlite3.connect(f"file:{rp}?mode=ro", uri=True, timeout=30)
            try:
                since = int(time.time()) - 86400
                n24, unm = r.execute("SELECT COUNT(*), AVG(unmatched_px) FROM frames WHERE time>=?", (since,)).fetchone()
                total, first, last = r.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM frames").fetchone()
                out["radar"] = {"frames_24h": n24, "expected_24h": 144, "frames_total": total,
                                "first": first, "last": last, "unmatched_px_24h_mean": _r(unm, 0)}
            finally:
                r.close()
        return out


def _dbz_hex(rate_mm_h: float) -> str:
    """Universal Blue colour for a rain rate (inverse Marshall-Palmer, as used by decode.py)."""
    dbz = int(round(10 * np.log10(200 * rate_mm_h ** 1.6)))
    return "#" + UNIVERSAL_BLUE[min(max(dbz, 5), 65)][:6]


class RadarCache:
    """Hourly rain totals (mm, float16, half resolution) for the last week, kept up to date by a
    background thread; plus an LRU of rendered PNGs. Thread-safe."""

    def __init__(self, data_dir: Path, crags):
        self.data_dir = data_dir
        self.crags = crags
        self.lock = threading.Lock()
        self.buckets: dict[int, list] = {}      # hour start (unix) -> [float16 mm array, n_frames]
        self.seen: set[int] = set()
        self.latest: int | None = None
        self.ready = False
        self.progress = (0, 0)
        self.grid = None
        self.png: OrderedDict = OrderedDict()

    def available(self) -> bool:
        return (self.data_dir / "radar.sqlite").exists()

    def archive(self):
        from .query import Archive
        return Archive(self.data_dir)          # one sqlite connection per caller thread

    def start(self) -> None:
        def loop():
            while True:
                try:
                    if self.available():
                        self.refresh()
                except Exception:  # noqa: BLE001
                    log.exception("radar cache refresh failed")
                time.sleep(RADAR_REFRESH_S)
        threading.Thread(target=loop, name="radar-cache", daemon=True).start()

    def refresh(self) -> int:
        """Add any new frames from the last week to the hourly totals; drop expired hours."""
        a = self.archive()
        try:
            self.grid = a.grid
            latest = a.db.execute("SELECT MAX(time) FROM frames WHERE path IS NOT NULL").fetchone()[0]
            if latest is None:
                self.ready = True
                return 0
            cutoff = (latest // 3600) * 3600 - (RADAR_KEEP_HOURS + 1) * 3600
            todo = [t for t in a.frame_times(cutoff, latest + 1) if t not in self.seen]
            for i, t in enumerate(todo):
                r = np.nan_to_num(a.rate(t))
                h, w = r.shape
                mm = r.reshape(h // RADAR_DS, RADAR_DS, w // RADAR_DS, RADAR_DS).mean(axis=(1, 3)) / 6.0
                hb = t - t % 3600
                with self.lock:
                    b = self.buckets.get(hb)
                    self.buckets[hb] = [((b[0].astype(np.float32) + mm) if b else mm).astype(np.float16),
                                        (b[1] if b else 0) + 1]
                    self.seen.add(t)
                    self.progress = (i + 1, len(todo))
            with self.lock:
                for hb in [k for k in self.buckets if k < cutoff]:
                    del self.buckets[hb]
                self.seen = {t for t in self.seen if t >= cutoff}
                self.latest = latest
                self.ready = True
            return len(todo)
        finally:
            a.db.close()

    def _cached_png(self, key, make) -> bytes:
        with self.lock:
            if key in self.png:
                self.png.move_to_end(key)
                return self.png[key]
        buf = io.BytesIO()
        make().save(buf, "PNG", optimize=False)
        body = buf.getvalue()
        with self.lock:
            self.png[key] = body
            while len(self.png) > RADAR_PNG_CACHE:
                self.png.popitem(last=False)
        return body

    def info(self) -> dict:
        if not self.available():
            raise NotFound("no radar archive")
        a = self.archive()
        try:
            latest = a.db.execute("SELECT MAX(time) FROM frames WHERE path IS NOT NULL").fetchone()[0]
            frames = a.frame_times(latest - RADAR_FRAME_HOURS * 3600 + 1, latest + 1) if latest else []
            bounds = a.grid.bounds()
        finally:
            a.db.close()
        return {"bounds": bounds, "latest": latest, "frames": frames, "ready": self.ready,
                "progress": list(self.progress), "max_hours": RADAR_KEEP_HOURS,
                "crags": [{"name": c.name, "lat": c.lat, "lon": c.lon} for c in self.crags],
                "acc_stops": ACC_STOPS_DASH, "acc_cols": ACC_COLS_DASH,
                "rate_stops": RATE_LEGEND, "rate_cols": [_dbz_hex(r) for r in RATE_LEGEND]}

    def frame_png(self, t: int) -> bytes:
        def make():
            a = self.archive()
            try:
                return render_frame(a.dbz(t))
            except KeyError:
                raise NotFound(f"no frame at {t}") from None
            finally:
                a.db.close()
        return self._cached_png(("frame", t), make)

    def window(self, hours: int) -> tuple[int, int]:
        end = self.latest + 600                                    # the last frame covers 10 min
        start = (end // 3600) * 3600 - hours * 3600 if end % 3600 else end - hours * 3600
        return start, end

    def accumulate(self, hours: int) -> tuple[np.ndarray, dict]:
        if not self.ready or self.latest is None:
            raise NotReady(self.progress)
        start, end = self.window(hours)
        with self.lock:
            parts = [(hb, self.buckets.get(hb)) for hb in range(start, end, 3600)]
        mm, n_frames = None, 0
        for hb, b in parts:
            if not b:
                continue
            # a complete hour with a few missing frames is filled at that hour's mean rate
            scale = 6.0 / b[1] if hb + 3600 <= end and b[1] < 6 else 1.0
            v = b[0].astype(np.float32) * scale
            mm = v if mm is None else mm + v
            n_frames += b[1]
        if mm is None:
            mm = np.zeros((self.grid.height // RADAR_DS, self.grid.width // RADAR_DS), np.float32)
        crags = {}
        for c in self.crags:
            try:
                r, col = self.grid.latlon_to_pixel(c.lat, c.lon)
            except ValueError:
                continue
            r, col = r // RADAR_DS, col // RADAR_DS
            crags[c.name] = round(float(mm[max(r - 1, 0):r + 2, max(col - 1, 0):col + 2].mean()), 1)
        meta = {"hours": hours, "start": start, "end": end, "latest": self.latest,
                "frames": n_frames, "expected_frames": (end - start) // 600,
                "max_mm": round(float(mm.max()), 1), "crags": crags,
                "stops": (list(ACC_STOPS_DASH) if hours <= ACC_FIXED_MAX_HOURS
                          else adaptive_stops(mm, len(ACC_COLS_DASH))),
                "cols": ACC_COLS_DASH, "adaptive": hours > ACC_FIXED_MAX_HOURS}
        return mm, meta

    def accum_json(self, hours: int) -> dict:
        _, meta = self.accumulate(hours)
        return {**meta, "png": f"api/radar/accum.png?hours={hours}&latest={meta['latest']}"}

    def accum_png(self, hours: int) -> bytes:
        if not self.ready or self.latest is None:
            raise NotReady(self.progress)
        return self._cached_png(("acc", hours, self.latest),
                                lambda: self._render_acc(hours))

    def _render_acc(self, hours: int):
        mm, meta = self.accumulate(hours)
        return render_mm(mm, meta["stops"], meta["cols"])


class NotReady(Exception):
    pass


def _hours_param(q: dict) -> int:
    try:
        h = int(q.get("hours", "24"))
    except ValueError:
        raise BadRequest("hours must be an integer") from None
    if not 1 <= h <= RADAR_KEEP_HOURS:
        raise BadRequest(f"hours must be 1..{RADAR_KEEP_HOURS}")
    return h


def make_handler(dash: Dashboard):
    radar = dash.radar
    class Handler(BaseHTTPRequestHandler):
        server_version = "rain-archive-dashboard"

        def log_message(self, fmt, *args):   # keep the journal quiet; enable with -v
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-cache"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json_error(self, code: int, msg: str):
            self._send(code, json.dumps({"error": msg}).encode(), "application/json")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if path in STATIC:
                    fname, ctype = STATIC[path]
                    body = (WEB_DIR / fname).read_bytes()
                    self._send(200, body, ctype, "max-age=86400" if path.startswith("/vendor/") else "no-cache")
                elif path == "/api/board":
                    self._send(200, dash.cached("board", dash.board), "application/json")
                elif path == "/api/status":
                    self._send(200, dash.cached("status", dash.status), "application/json")
                elif path.startswith("/api/crag/"):
                    name = unquote(path[len("/api/crag/"):])
                    key = f"crag|{name.lower()}|{q.get('from')}|{q.get('to')}"
                    self._send(200, dash.cached(key, lambda: dash.crag(name, q.get("from"), q.get("to"))),
                               "application/json")
                elif path == "/api/radar":
                    self._send(200, json.dumps(radar.info()).encode(), "application/json")
                elif path.startswith("/api/radar/frame/") and path.endswith(".png"):
                    try:
                        t = int(path[len("/api/radar/frame/"):-4])
                    except ValueError:
                        raise BadRequest("bad frame time") from None
                    if not radar.available():
                        raise NotFound("no radar archive")
                    self._send(200, radar.frame_png(t), "image/png", "max-age=604800")
                elif path == "/api/radar/accum":
                    self._send(200, json.dumps(radar.accum_json(_hours_param(q))).encode(), "application/json")
                elif path == "/api/radar/accum.png":
                    self._send(200, radar.accum_png(_hours_param(q)), "image/png", "max-age=3600")
                else:
                    self._json_error(404, "not found")
            except NotFound as e:
                self._json_error(404, str(e))
            except NotReady as e:
                done, total = e.args[0]
                self._json_error(503, f"radar cache warming up: {done}/{total} frames")
            except BadRequest as e:
                self._json_error(400, str(e))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                log.exception("error serving %s", self.path)
                self._json_error(500, f"{type(e).__name__}: {e}")

    return Handler


def make_server(cfg: W.WeatherConfig, host: str, port: int, radar_thread: bool = True) -> ThreadingHTTPServer:
    dash = Dashboard(cfg)
    if radar_thread:
        dash.radar.start()
    srv = ThreadingHTTPServer((host, port), make_handler(dash))
    srv.dash = dash
    srv.daemon_threads = True
    return srv


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rainarchive.dashboard")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    srv = make_server(W.WeatherConfig(a.config), a.host, a.port)
    log.info("dashboard on http://%s:%d/ (data %s)", a.host, a.port, W.WeatherConfig(a.config).data_dir)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
