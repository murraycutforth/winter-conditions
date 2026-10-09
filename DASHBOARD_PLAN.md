# Crag conditions dashboard — plan

Status: **draft, decisions from the 2026-09-28 review folded in** (see §0). Implements README §2 item 5
and the Cairngorm AWS half of item 2. It is deliberately not based on
the sibling `winter-climbing-dashboard` / `rime-predictor` projects: this one sits on top of the
`weather.sqlite` / `radar.sqlite` pipeline in this repo.

## 0. Decisions (2026-09-28)
- **Access:** LAN plus Tailscale (installed on the Pi 2026-10-09). Still no auth; never port-forward.
- **Default window:** −21 d … +7 d.
- **Observation logging from the dashboard:** not now; the observation method isn't chosen yet. The
  dashboard stays strictly read-only.
- **Station observations:** archive the Cairngorm AWS and the Holfuy stations (§2.5), and show them
  on the dashboard.

## 1. What the dashboard is for

The question it answers: **what state is each crag in now, and which way is it heading?**
Winter conditions are an integral over the last 2–3 weeks plus the next few days. So every crag view
is a time series from **−21 days to +7 days**, with a "now" line and the forecast shaded. Every view
shows the underlying physical variables and the rules applied to them, not a single opaque score.

The priority variables are freeze–thaw, snow, wind speed and direction (relative to the face), rime,
freezing level and lapse rate.

## 2. Data gaps to close first (backend)

The first four variables already exist in `crag_daily`. **Freezing level and lapse rate don't exist
yet.** I probed Open-Meteo for Ben Nevis on 2026-09-28:

| variable | `fc` (ukmo_seamless) | `ukv` 2025 | `ukv` 2023 | `era5` |
|---|---|---|---|---|
| `freezing_level_height` | yes | yes | **yes** | no |
| T + geopotential at 925 / 700 hPa | yes | yes | no | no |
| `snow_depth` | no | no | no | no |

- Pressure levels in UKV start in 2024-08, the same point as the existing 850 hPa data.
- The model's freezing level covers the **whole UKV archive (2022→)**, so the best source doesn't
  depend on the 850 hPa data.

### 2.1 Freezing level
- Add `freezing_level_height` → column `fzl` for the `ukv` and `fc` sources.
- New daily features:
  - `fzl_min`, `fzl_mean`, `fzl_max`
  - `hrs_fzl_below_crag`: hours when the freezing level is under the crag's elevation.
- ERA5 stays null. If a long-record proxy is ever needed, it can be derived from `t2m` and an
  assumed lapse rate, flagged as a proxy.

### 2.2 Lapse rate and temperature profile
- Add `t925, z925, t700, z700` (`ukv`/`fc`). Together with `t850, z850`, that gives a 3-level
  free-air profile each hour.
- Hourly derived quantities:
  - `lapse_925_850`, `lapse_850_700` in K/km. For reference, the dry adiabat is 9.8 K/km, the
    saturated adiabat about 5–6 K/km near 0 °C, and the standard atmosphere 6.5 K/km.
  - An inversion flag, set when a layer's lapse rate is < 0.
- Daily features: `lapse_mean`, `inversion_hrs`.
- **This matters for the existing features, not only for display.** Open-Meteo's `elevation=`
  downscaling of `t2m` applies a fixed lapse rate to the model's surface temperature. During
  inversions or strongly stable air, that can be several degrees wrong at crag height.
  - Example from the probe: Ben Nevis, 2025-02-01 12Z. The downscaled `t2m` at 1150 m was
    **+0.2 °C**, but the same model's freezing level was **1460 m** and `t850` was +2.4 °C. The
    free-air profile implies about +2 °C at the crag.
  - A 2 K error decides whether a freeze–thaw count happens at all.
- Plan: compute a second crag temperature, `t_prof`, by interpolating the profile to `elev_m`
  (linear in height between the bracketing levels; use `t2m` below the 925 hPa height).
- Show `t2m` and `t_prof` side by side. **Don't switch the features to `t_prof` yet.** Decide once
  it has been checked against the Cairngorm summit AWS (README §2 item 2). That's a
  source-validation question, and the dashboard makes the disagreement visible.

### 2.3 Snow depth
- Open-Meteo has no snow depth for the UKMO models or ERA5 at these points. Other models' depths
  would be grid-cell means at the wrong elevation anyway.
- Keep the degree-day `swe_model_mm` and label it honestly as **"modelled SWE, not observed
  depth"**.
- Show `lee_loading_7d` next to it: face-scale snow is dominated by wind redistribution, not by
  the snowfall.
- Real depth labels come later from the observations log, SAIS reports (roadmap item 4) or
  webcams.

### 2.4 Costs and migration
- API cost: UKV/fc requests go from 13 to 18 variables, so the call weight rises from ×1.3 to
  ×1.8.
- Re-fetching the pressure-level era (2024-08→) for 12 crags costs about 1.2k weighted calls, a
  fraction of one day's 7k budget.
- The freezing level for 2022-03 → 2024-07 needs a UKV re-fetch too, about 1.5k more calls.
- **Schema migration:** `SCHEMA` uses `CREATE TABLE IF NOT EXISTS`, so existing databases won't
  get new columns.
  - Add a `_migrate()` step: `PRAGMA table_info` → `ALTER TABLE ... ADD COLUMN` for missing
    `wx_hourly` and `crag_daily` columns.
  - Invalidate `fetch_log` rows for `ukv` chunks so that the newest-first backfill refills them.
- Tests: extend the fake Open-Meteo in `tests/test_weather_offline.py` to return the new fields,
  and add unit tests for the profile interpolation and lapse-rate calculation, including an
  inversion case.

### 2.5 Station observations: Cairngorm AWS and Holfuy

These are the only real mountain-top measurements available. They serve two purposes: live context
on the dashboard, and a ground truth for the model's crag-height temperature and wind (`t2m` vs
`t_prof`, and the 850 hPa / fallback wind factors).

**Cairngorm summit AWS (Heriot-Watt, 1245 m).**
- Terms, from its archive page: "free to use in any way you wish, but please reference us as the
  source". Credit it on the dashboard.
- Data, checked 2026-09-28:
  - `CurrentYear.txt`: the current year as CSV, one row every 30 min:
    `julian_day, hhmm, mean_wind, max_gust, min_wind, wdir, wdir_sd, T1, T2, stat_a, stat_b,
    T_hut`.
  - **Wind is in m/s**, despite the page saying mph: 12.63 in the CSV = 28 mph on the raw page.
  - Past years are at `<year>/YearData.txt` (2022), `<year>/YEARDATA <year>.txt` (2016) or
    `<year>/CurrentYear.txt` (2024–25). Filenames vary, so discover them from each year's
    directory listing.
  - Format changes: 1990–95 used m/s with QC flags (`Q`, `R` tokens); 1996 onwards uses the text
    format. **Backfill from 2000**, to match `era5_start`.
- **Big gaps:**
  - All of 2024 and 2025 up to about September have status `3,3`, temperature `6999` and wind
    reading 0.
  - The live feed stopped on **2026-08-31**; four weeks stale at planning time.
  - So the AWS overlaps the UKV profile era (2024-08 onwards) only from about 2025-09 to
    2026-08: a single winter. Overlap with plain UKV `t2m` is better (2022–23).
- Parsing rules (fixtures from each format era go in the tests):
  - Status other than `1,x` → the whole row is missing. Never treat those wind zeros as calm.
  - `6999` → null.
  - Julian day + `hhmm` + file year → UTC unix time.
  - **To verify:** whether the logger clock is UTC or local time. Check by cross-correlating the
    diurnal cycle with the model in summer vs winter; a 1 h lag in summer only means local time.
- Quality flags stored alongside the readings:
  - `t_suspect`: mean wind < ~4.5 m/s. The page itself warns that temperatures "read several
    degrees high when the wind speed drops below 10 mph", because the sensors aren't aspirated.
  - `icing_suspect`: a run of ≥ 2 h at ~0 wind with T < 0 °C. The anemometer is probably
    rimed. That's also a direct **rime signal** worth showing.
  - T1/T2 disagreement > 1 K.
- Fetching: `CurrentYear.txt` on each 3-hour timer run (conditional GET), and yearly files once
  for the backfill. It's a university server, so be gentle: one request per run, and ~26 requests
  for the whole backfill.
- Store the model at the AWS location as a model-only pseudo-site (`Cairn Gorm AWS`, 1245 m) in
  `wx_hourly`. That makes model-vs-observed a like-for-like comparison at the same point and
  height, which neither the Northern Corries (1050 m) nor the Shelter Stone (900 m) are. It adds
  one site to the backfill budget, about 1/12 extra.

**Holfuy stations (SAIS / SMC): CIC Hut 1746, Aonach Mòr 1365, Stob Coire Dubh 1530, Drumochter
1531, …**
- **No automated collection without permission.** Holfuy's terms forbid it without agreement.
  `api.holfuy.com/live/?s=1746` returns `"no_access"`, and `robots.txt` disallows `/api/*`, which
  includes the webcam image URLs.
- Holfuy's terms offer **free API access to named stations**, usually with the station owner's
  agreement.
- **Action for the user:** email Holfuy (info@holfuy.hu), and possibly SAIS/SMC as the station
  owners, asking for personal, non-commercial API access to the stations above. I can draft the
  email.
- Until then:
  - Build the Holfuy ingester against the documented live/archive JSON API, with a fake-server
    test. It stays disabled until `[stations.holfuy] api_key` is set in `config.toml`.
  - The dashboard only links to the station pages and webcams; no hotlinking.
- Holfuy only exposes recent history, so the archive only starts when collection starts. Poll on
  the station's update interval: 15 min on a separate small timer, or 3 h if the archive endpoint
  covers the gap. Decide from the API docs once access is granted.

**Storage** (additive, in `weather.sqlite`):
- `stations(id, name, provider, provider_id, lat, lon, elev_m, near_crags)`.
- `station_obs(station, time, t, t2, ws, gust, wmin, wd, wd_sd, rh, pressure, status, flags)`,
  primary key `(station, time)`, SI units, UTC. Unknown fields are null (the AWS has no RH;
  Holfuy stations may have it).
- A `station_hourly` view: hourly means for joining to `wx_hourly`. Wind direction is a vector
  mean.
- A `fetch_log` row per station/year for the backfill.

**Validation outputs** (roadmap item 2):
- A `features.py`-style `validate.py` that computes model − observed bias and MAE for `t2m`,
  `t_prof` and ridge wind at the AWS.
- Broken down by month, by hour of day and by an inversion flag.
- The key result: whether `t_prof` beats `t2m` during inversions (§2.2).

## 3. Serving it from the Pi

**Recommendation: a stdlib JSON API plus one static page. No new Python dependencies.**

- `rainarchive/dashboard.py`: `http.server.ThreadingHTTPServer`.
  - Opens `weather.sqlite` read-only (`file:...?mode=ro`, WAL-safe alongside the timer's writes).
    Uses `query.Archive` for radar.
  - Caches responses by the `weather_status.json` mtime. Data only changes every 3 h, so almost
    every request is a cache hit.
  - Run with `python3 -m rainarchive.dashboard -c config.toml --port 8080`.
- Endpoints:
  - `/api/crags`
  - `/api/board?from&to`: the daily summary grid for all crags
  - `/api/crag/<name>/daily?from&to`
  - `/api/crag/<name>/hourly?from&to`: t2m, t_prof, profile, fzl, ridge wind and direction,
    precipitation type
  - `/api/station/<id>/hourly?from&to`
  - `/api/status`: data freshness, backfill progress, API usage, radar collector health
- Frontend: `web/dashboard/index.html` + one JS file, vanilla.
  - Charts use **uPlot** (~50 KB), vendored into the repo, so no CDN is needed. It's fast on
    phones with ~700 hourly points × several series.
  - Wind arrows and the profile heatmap are drawn on a canvas by hand.
  - Build and verify the frontend against `local-data/` on the Mac first.
- Deployment:
  - `systemd/rain-archive-dashboard.service` (`Restart=always`, `Nice=10`, a `MemoryMax` cap).
  - `install-dashboard.sh` in the style of `install-weather.sh`.
- Why not Streamlit or Dash: both are large pip installs, use hundreds of MB of RAM and are
  sluggish on a Pi 4, all for charts that are simple time series. They'd go against the repo's
  dependency convention for no real gain.
- Access: **LAN only**, at `http://<pi-host>.local:8080`. No auth; don't port-forward it.
  Tailscale is the upgrade path when remote access is wanted, with no code change needed.

## 4. Views

### A. Board: all crags at a glance (landing page)
- Rows are crags grouped by area; columns are days −7 … +7.
- Each cell is a compact glyph:
  - Background colour = thermal state of the day: frozen all day / freeze–thaw / thawed all day.
  - Corner marks for rime-on-face hours, lee loading and rain.
  - A small freezing-level tick relative to the crag's height.
- A header strip shows `days_since_thaw` and the 7-day refreezes per crag.
- Clicking a cell opens the crag view at that date.

### B. Crag view: stacked panels sharing one time axis (−21 d … +7 d, hourly where useful)
1. **Temperature and freeze–thaw:**
   - `t2m` and `t_prof` lines, the 0 °C and ±0.5 °C hysteresis band, and refreeze/thaw
     markers.
   - Daily FDH/PDH as bars below.
2. **Freezing level:**
   - `fzl` line, with horizontal lines at the crag's elevation and the summit height.
   - Shaded where the freezing level is below the crag.
3. **Profile and lapse rate:**
   - A time–height strip of temperature interpolated between 925/850/700 hPa, with the 0 °C
     contour (the freezing level again, from the profile) and crag elevation marked.
   - Below it, the layer lapse rates against the dry/saturated/standard reference lines, with
     inversions shaded.
   - Profile data only exists from 2024-08 onwards; the panel is blank before that.
4. **Wind:**
   - Ridge wind speed. The line style says whether it's 850 hPa or the calibrated 10 m fallback,
     so the assumption is visible.
   - Direction arrows **coloured by their angle to the face**: onto the face (rime) / over the top
     (loading) / cross-wind.
5. **Rime:** daily `rime_face_hrs` bars with the 7-day rolling line. Hourly rime-condition hours
   are shaded on panel 1.
6. **Snow:**
   - Frozen precipitation and rain as stacked bars, plus lee loading bars.
   - The `swe_model_mm` line, labelled "modelled SWE".
   - `days_since_thaw`.
7. **Radar vs model precipitation**, where radar exists (from 2026-09-25).
8. **Station observations**, for crags with a nearby station (`near_crags`):
   - AWS/Holfuy temperature and wind overlaid on panels 1 and 4 as points, with quality flags
     shown.
   - The model at the station's point and height is drawn as a thin line, so the model–obs
     mismatch is visible directly.
   - Rime-icing suspect periods are marked on panel 5.
   - For example: Cairn Gorm AWS → Northern Corries and Shelter Stone; CIC Hut and Aonach Mòr →
     Ben Nevis; Stob Coire Dubh → Creag Meagaidh.
   - Station observations are shifted to crag height with the model's own profile lapse rate, and
     labelled as such. A 1245 m reading is not a 1050 m reading.
9. (Later, once an observation method is chosen) the user's own condition observations as
   markers.

A small **aspect rose** sits next to the panels: the 7-day wind-direction distribution weighted by
frozen precipitation and rime hours, with the face aspect drawn on. It shows whether the face has
been loaded, rimed or scoured.

### C. Season context (phase 3)
- This winter's cumulative FDH, refreezes and modelled SWE per crag, against the percentile band
  of ERA5 2000→ winters.
- Uses `source='era5'` for homogeneity, per README §9.

### D. Stations
- Latest reading per station with its age. Warn loudly when a station is stale; the AWS had been
  silent for 4 weeks at planning time.
- A 7-day chart of observed vs model at the station's point.
- The validation tables from `validate.py`.

### E. Status
- Data freshness per source and station, backfill progress, wind factors, API budget use, and the
  radar collector's `status.json`.

## 5. Conditions flags (phase 3, after reviewing the heuristics)
- Transparent, rule-based flags per crag and day in a new `conditions.py`, thresholds as named
  constants in the style of `features.py`. Examples:
  - **"Ice building"**: ≥ 2 refreezes in 7 d after wet input.
  - **"Rimed"**: `rime_face_hrs_7d` ≥ N.
  - **"Fresh loading / avalanche-prone"**: `lee_loading_7d` ≥ X.
  - **"Stripped"**: a thaw day with rain, and no refreeze since.
- Hovering over a flag shows the rule and the values that triggered it. No learned score until
  there are enough `observations` (roadmap item 6).

## 6. Phases

| phase | contents | done when |
|---|---|---|
| 0 | Deploy the weather extension on the Pi (README §1 items 1–3) | backfill complete, factors sane |
| 1a | §2.1–2.4: new variables, migration, `t_prof`, lapse and freezing-level features, tests | offline tests pass; the Ben Nevis Feb 2025 profile looks right; re-fetch done within budget |
| 1b | §2.5: Cairngorm AWS ingester + backfill from 2000, AWS model pseudo-site, `validate.py`; Holfuy ingester behind a config key | fake-server tests for both; AWS clock timezone settled; first model-vs-AWS bias table |
| — | **User:** request Holfuy API access | key in `config.toml` |
| 2 | §3 server + crag view panels 1, 2, 4, 6 + stations + status; systemd | usable from a phone on the LAN |
| 3 | Board, profile/lapse panel, rime panel, aspect rose, station overlays | the whole of §4 A–B |
| 4 | Season context, conditions flags | — |

Phases 1a and 1b are independent and can be built on the Mac while phase 0 (the Pi deploy) is
pending.

## 7. Open questions
1. Switch the features to `t_prof` once it's validated, or keep `t2m`? The answer comes from the
   phase 1b bias tables.
2. Is the AWS clock UTC or local time? To be settled empirically in phase 1b.
3. Which Holfuy stations are worth asking for? Suggested: CIC Hut 1746, Aonach Mòr 1365, Stob
   Coire Dubh 1530. Holfuy tends to grant up to about 3 per user.
4. Should the Cairngorm AWS outage be reported to Heriot-Watt (Cairngorm@hw.ac.uk)?
