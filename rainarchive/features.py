"""Daily winter-conditions features for a crag, from an hourly weather series.

All inputs are at crag height: 2 m temperature and humidity are downscaled by Open-Meteo to the
crag's elevation. For "ridge" wind the 850 hPa wind (~1,300-1,500 m, free air) is used when the
source has it, because a 2 km model's 10 m wind over smoothed terrain is far too light at crag
height. Where it doesn't (ERA5; UKV before ~2024-08) the caller passes 10 m wind scaled by a per-crag
calibrated factor (weather.calibrate_wind).

Definitions (tunable constants at the top of the file):
  refreeze      transition from thawed (T >= +0.5 C) to frozen (T <= -0.5 C); hysteresis stops
                noise around 0 C being counted as cycles. "thaws" is the reverse transition.
  pdh / fdh     positive / freezing degree-hours: sum of max(T,0) and max(-T,0) over the day.
  frozen precip precipitation in hours with wet-bulb temperature < 1 C (falls as snow/graupel).
  rime hour     humidity >= 95 % (in cloud), -10 C <= T <= -0.5 C and ridge wind >= 5 m/s.
                rime_face_hrs only counts rime hours with the wind blowing onto the face
                (wind from within +/-60 deg of the face's aspect).
  lee loading   frozen precip while ridge wind >= 7 m/s blows from behind the face (+/-60 deg
                of aspect+180), i.e. snow blown over the top and deposited on the face.
  rime since thaw  (hourly, for the dashboard) rime-on-face hours accumulated since the last
                sustained thaw: T > THAW_RESET_T for more than THAW_RESET_HOURS consecutive hours,
                which is taken to strip or soften the rime.
  swe_model_mm  a very simple degree-day snowpack: + frozen precip, - 3 mm per degree-day of
                melt, - rain-on-snow at 0.5 mm per mm. A rough "is there snow cover" signal.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

FROZEN_T, THAWED_T = -0.5, 0.5
RIME_RH, RIME_TMIN, RIME_TMAX, RIME_WIND = 95.0, -10.0, -0.5, 5.0
LOAD_WIND = 7.0
SNOW_TW = 1.0
MELT_MM_PER_DEG_DAY = 3.0
RAIN_ON_SNOW = 0.5
THAW_DAY_PDH = 5.0          # a day with >= 5 degree-hours above 0 C counts as a thaw day
FACE_HALF_ANGLE = 60.0
THAW_RESET_T = 1.0          # sustained thaw: T > 1 C ...
THAW_RESET_HOURS = 4        # ... for more than 4 consecutive hours resets the accumulated rime

DAILY_COLUMNS = [
    "n_hours", "tmin", "tmax", "tmean", "hrs_below0", "pdh", "fdh", "refreezes", "thaws",
    "precip_mm", "frozen_precip_mm", "rain_mm", "snowfall_cm", "rime_hrs", "rime_face_hrs",
    "rime_index", "lee_loading_mm", "wind_mean", "wind_max", "wdir_mean", "rh_mean", "swe_model_mm",
    "days_since_thaw", "refreeze_7d", "thaw_7d", "frozen_precip_7d", "frozen_precip_14d",
    "rime_face_hrs_7d", "lee_loading_7d", "pdh_3d", "fdh_7d", "rain_7d", "basis",
]


def wet_bulb(t: float, rh: float) -> float:
    """Stull (2011) wet-bulb temperature, deg C, from T (deg C) and RH (%). Good to ~1 C for RH>5%."""
    rh = max(min(rh, 100.0), 1.0)
    return (t * math.atan(0.151977 * math.sqrt(rh + 8.313659)) + math.atan(t + rh)
            - math.atan(rh - 1.676331) + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def rime_hour(t, rh, w, wd, aspect: float) -> tuple[bool, bool]:
    """(rime conditions, rime conditions with the wind onto the face) for one hour."""
    rime = (t is not None and w is not None and rh is not None and rh >= RIME_RH
            and RIME_TMIN <= t <= RIME_TMAX and w >= RIME_WIND)
    return rime, rime and wd is not None and _angle_diff(wd, aspect) <= FACE_HALF_ANGLE


def rime_since_thaw(hours: list[dict], aspect: float) -> dict[str, list[int]]:
    """Hourly rime accumulation with resets at sustained thaws.

    hours: time-sorted dicts with time, t, rh, wind, wdir (as for daily_features). Returns lists
    aligned with `hours`: rime (1 = rime-on-face hour), thaw (1 = hour inside a sustained thaw) and
    cum (rime-on-face hours since the last sustained thaw). A gap in the series breaks a warm run.
    Pass a warm-up period: the counter starts at 0 at the first hour given.
    """
    n = len(hours)
    rime, thaw, cum = [0] * n, [0] * n, [0] * n
    total, run_start = 0, None
    for i, h in enumerate(hours):
        t = h.get("t")
        if run_start is not None and h["time"] - hours[i - 1]["time"] != 3600:
            run_start = None
        if t is not None and t > THAW_RESET_T:
            if run_start is None:
                run_start = i
            if i - run_start + 1 > THAW_RESET_HOURS:
                for j in range(run_start, i + 1):   # the whole run is the thaw, counter 0 from its start
                    thaw[j], cum[j] = 1, 0
                total = 0
        else:
            run_start = None
        rime[i] = int(rime_hour(t, h.get("rh"), h.get("wind"), h.get("wdir"), aspect)[1])
        total += rime[i]
        cum[i] = total
    return {"rime": rime, "thaw": thaw, "cum": cum}


def daily_features(hours: list[dict], aspect: float, tz: str = "Europe/London") -> list[dict]:
    """hours: time-sorted dicts with keys time (unix), t, rh, precip, snowfall, wind, wdir, src.

    Returns one dict per local calendar day (DAILY_COLUMNS + 'date'), in date order.
    Stateful quantities (freeze state, snowpack, days since thaw) carry across days, so pass a
    warm-up period before the dates you care about.
    """
    zone = ZoneInfo(tz)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for h in hours:
        by_day[datetime.fromtimestamp(h["time"], zone).strftime("%Y-%m-%d")].append(h)

    state = None          # 'frozen' | 'thawed'
    swe = 0.0
    days_since_thaw = None
    hist: deque[dict] = deque(maxlen=14)
    out = []
    if not by_day:
        return out
    d0 = datetime.strptime(min(by_day), "%Y-%m-%d")
    d1 = datetime.strptime(max(by_day), "%Y-%m-%d")
    day = d0
    while day <= d1:
        key = day.strftime("%Y-%m-%d")
        day += timedelta(days=1)
        hs = [h for h in by_day.get(key, []) if h["t"] is not None]
        f = {"date": key, "n_hours": len(hs)}
        if not hs:
            hist.append(f)
            out.append(f)
            continue
        ts = [h["t"] for h in hs]
        refreezes = thaws = 0
        pdh = fdh = hrs_below0 = 0.0
        precip = frozen = rain = snowfall = 0.0
        rime = rime_face = rime_index = lee = 0.0
        winds, u, v, rhs = [], 0.0, 0.0, []
        srcs: dict[str, int] = defaultdict(int)
        for h in hs:
            t = h["t"]
            srcs[h.get("src") or "?"] += 1
            pdh += max(t, 0.0)
            fdh += max(-t, 0.0)
            hrs_below0 += t < 0
            if t <= FROZEN_T:
                if state == "thawed":
                    refreezes += 1
                state = "frozen"
            elif t >= THAWED_T:
                if state == "frozen":
                    thaws += 1
                state = "thawed"
            rh = h.get("rh")
            p = h.get("precip") or 0.0
            precip += p
            snowfall += h.get("snowfall") or 0.0
            is_frozen = rh is not None and wet_bulb(t, rh) < SNOW_TW or rh is None and t < 0.5
            if is_frozen:
                frozen += p
            else:
                rain += p
            w, wd = h.get("wind"), h.get("wdir")
            if rh is not None:
                rhs.append(rh)
            if w is not None:
                winds.append(w)
                if wd is not None:
                    u += w * math.sin(math.radians(wd))
                    v += w * math.cos(math.radians(wd))
            is_rime, on_face = rime_hour(t, rh, w, wd, aspect)
            if is_rime:
                rime += 1
                rime_index += w
                rime_face += on_face
            if (is_frozen and p > 0 and w is not None and wd is not None and w >= LOAD_WIND
                    and _angle_diff(wd, aspect + 180.0) <= FACE_HALF_ANGLE):
                lee += p
        # degree-day snowpack
        swe += frozen
        melt = MELT_MM_PER_DEG_DAY * pdh / 24.0 + RAIN_ON_SNOW * rain
        swe = max(0.0, swe - melt)
        n = len(hs)
        f.update(
            tmin=min(ts), tmax=max(ts), tmean=sum(ts) / n, hrs_below0=hrs_below0, pdh=pdh, fdh=fdh,
            refreezes=refreezes, thaws=thaws, precip_mm=precip, frozen_precip_mm=frozen, rain_mm=rain,
            snowfall_cm=snowfall, rime_hrs=rime, rime_face_hrs=rime_face, rime_index=rime_index,
            lee_loading_mm=lee,
            wind_mean=sum(winds) / len(winds) if winds else None, wind_max=max(winds) if winds else None,
            wdir_mean=(math.degrees(math.atan2(u, v)) % 360.0) if winds and (u or v) else None,
            rh_mean=sum(rhs) / len(rhs) if rhs else None, swe_model_mm=swe,
            basis=max(srcs, key=srcs.get),
        )
        if pdh >= THAW_DAY_PDH:
            days_since_thaw = 0
        elif days_since_thaw is not None:
            days_since_thaw += 1
        f["days_since_thaw"] = days_since_thaw
        hist.append(f)

        def roll(col, k):
            vals = [x.get(col) for x in list(hist)[-k:]]
            vals = [x for x in vals if x is not None]
            return sum(vals) if vals else None

        f.update(refreeze_7d=roll("refreezes", 7), thaw_7d=roll("thaws", 7),
                 frozen_precip_7d=roll("frozen_precip_mm", 7), frozen_precip_14d=roll("frozen_precip_mm", 14),
                 rime_face_hrs_7d=roll("rime_face_hrs", 7), lee_loading_7d=roll("lee_loading_mm", 7),
                 pdh_3d=roll("pdh", 3), fdh_7d=roll("fdh", 7), rain_7d=roll("rain_mm", 7))
        out.append(f)
    return out
