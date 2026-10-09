"""Open-Meteo client for crag point time series.

Sources (all hourly, UTC, SI units with wind in m/s):
    ukv   Met Office UKV 2 km, as archived by the Historical Forecast API     2022-03-01 ->
    era5  ECMWF ERA5 reanalysis (~25 km), Historical Weather API              1940 ->  (~5 day lag)
    fc    latest Met Office forecast (UKV for ~2 days, then global model)     now -> +7 days

The free, non-commercial tier is limited to roughly 10,000 calls/day, 5,000/hour and 600/minute,
where a long or many-variable request counts as several calls. Usage is tracked in the database
and backfills are spread over several days to stay well inside that.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

SURFACE = ["temperature_2m", "relative_humidity_2m", "precipitation", "snowfall",
           "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "cloud_cover_low"]
LEVEL_850 = ["temperature_850hPa", "relative_humidity_850hPa", "wind_speed_850hPa",
             "wind_direction_850hPa", "geopotential_height_850hPa"]

SOURCES = {
    "ukv":  {"url": "https://historical-forecast-api.open-meteo.com/v1/forecast",
             "model": "ukmo_uk_deterministic_2km", "vars": SURFACE + LEVEL_850, "start": "2022-03-01"},
    "era5": {"url": "https://archive-api.open-meteo.com/v1/archive",
             "model": "era5", "vars": SURFACE, "start": None},
    "fc":   {"url": "https://api.open-meteo.com/v1/forecast",
             "model": "ukmo_seamless", "vars": SURFACE + LEVEL_850, "start": None},
}

# response field -> database column
COLUMNS = {
    "temperature_2m": "t2m", "relative_humidity_2m": "rh2m", "precipitation": "precip",
    "snowfall": "snowfall", "wind_speed_10m": "ws10", "wind_direction_10m": "wd10",
    "wind_gusts_10m": "gust10", "cloud_cover_low": "cloud_low",
    "temperature_850hPa": "t850", "relative_humidity_850hPa": "rh850", "wind_speed_850hPa": "ws850",
    "wind_direction_850hPa": "wd850", "geopotential_height_850hPa": "z850",
}


def call_weight(n_days: float, n_vars: int) -> float:
    """Open-Meteo counts requests longer than 2 weeks or with more than 10 variables as extra calls."""
    return max(1.0, n_days / 14.0) * max(1.0, n_vars / 10.0)


class OpenMeteo:
    def __init__(self, user_agent: str, session: requests.Session | None = None,
                 per_minute_weight: float = 400.0):
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.per_minute_weight = per_minute_weight
        self._next_ok = 0.0

    def hourly(self, source: str, lat: float, lon: float, elev: float,
               start: str | None = None, end: str | None = None, forecast_days: int = 7) -> tuple[dict, float]:
        """Fetch hourly data. Returns ({column: [values], 'time': [unix]}, weight_used)."""
        src = SOURCES[source]
        variables = list(src["vars"])
        for attempt in range(2):
            params = {"latitude": lat, "longitude": lon, "elevation": elev, "models": src["model"],
                      "hourly": ",".join(variables), "timezone": "GMT", "timeformat": "unixtime",
                      "wind_speed_unit": "ms", "precipitation_unit": "mm", "temperature_unit": "celsius"}
            if source == "fc":
                params.update(past_days=1, forecast_days=forecast_days)
                n_days = forecast_days + 1
            else:
                params.update(start_date=start, end_date=end)
                n_days = (time.mktime(time.strptime(end, "%Y-%m-%d")) -
                          time.mktime(time.strptime(start, "%Y-%m-%d"))) / 86400 + 1
            weight = call_weight(n_days, len(variables))
            # pace requests so weighted usage stays under the per-minute limit
            wait = self._next_ok - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_ok = time.monotonic() + 60.0 * weight / self.per_minute_weight
            r = self.s.get(src["url"], params=params, timeout=120)
            if r.status_code == 400 and attempt == 0 and variables != SURFACE:
                reason = _reason(r)
                log.warning("%s rejected the variable list (%s); retrying with surface variables only",
                            source, reason)
                variables = list(SURFACE)
                continue
            if r.status_code == 429:
                raise RateLimited(_reason(r))
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code}: {_reason(r)}", response=r)
            js = r.json()
            h = js.get("hourly") or {}
            out = {"time": [int(t) for t in h.get("time", [])]}
            for field, col in COLUMNS.items():
                out[col] = h.get(field)  # None if not requested / not provided
            return out, weight
        raise RuntimeError("unreachable")


class RateLimited(Exception):
    pass


def _reason(r) -> str:
    try:
        return r.json().get("reason", r.text[:200])
    except Exception:  # noqa: BLE001
        return r.text[:200]
