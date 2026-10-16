# rain-archive: Scottish rain radar + winter climbing conditions

A personal project with two parts:

- A **Raspberry Pi 4 service** that archives rain radar over Scotland (RainViewer, every 10-minute frame).
- A **crag weather extension** that builds hourly weather and daily winter-conditions features for
  12 Scottish winter climbing venues (Open-Meteo: Met Office UKV 2 km, ERA5 and forecasts).

It also includes two standalone browser tools: a live UK rain radar viewer and a crag location checker.

The end goal is a **dashboard and, eventually, a model that predicts Scottish winter climbing
conditions** (ice, névé, rime, buttresses in nick) from the weather of the previous days to weeks.

This README is both the user documentation and the handover brief for continuing the work in
Claude Code. Start with **Current status** and **Next steps**.

---

## Contents

1. [Current status (handover)](#1-current-status-handover)
2. [Next steps / roadmap](#2-next-steps--roadmap)
3. [Repository map](#3-repository-map)
4. [Working on this project (conventions for Claude Code)](#4-working-on-this-project-conventions-for-claude-code)
5. [Development and deployment workflow](#5-development-and-deployment-workflow)
6. [Part A: radar archive](#6-part-a-radar-archive)
7. [Part B: crag weather extension](#7-part-b-crag-weather-extension)
8. [Web tools](#8-web-tools)
9. [Design decisions and lessons learned](#9-design-decisions-and-lessons-learned)
10. [Known limitations and open questions](#10-known-limitations-and-open-questions)
11. [Reference: crags, sources, external data](#11-reference-crags-sources-external-data)

---

## 1. Current status (handover)

Last updated 2026-10-09.

Pi access: `ssh murray@pi4.local` (SSH key auth from the Mac works; sudo needs a password).

| Component | State | Verified how |
|---|---|---|
| Radar archive (`rainarchive` collector) | **Running on the Pi** since 2026-09-25 as systemd service `rain-archive` | Checked 2026-10-09: 2010 frames 2026-09-25 19:00 → 2026-10-09 19:00 UTC, all 20/20 tiles, valid_frac 1.0. 7 single-frame gaps (e.g. 2026-10-03 12:50, 13:20), each a frame RainViewer never offered while the collector was polling normally, so these are upstream gaps. Daily totals built through 2026-10-08. |
| Crag weather extension (`rainarchive.weather`) | **Installed and running on the Pi** since 2026-09-28 (`rain-archive-weather.timer`, every 3 h) | Checked 2026-10-09: backfill complete (ukv 60/60, era5 324/324 crag-year chunks). Every crag/source is gap-free with no NULL t2m/ws10: ukv 2022-03-01 → 2026-10-08, era5 2000-01-01 → 2026-10-03, fc 8 days. Features (`best`, `era5`) and `radar_daily` populated. One transient forecast JSON error on 2026-10-09 06:19 recovered on the next run. |
| 850 hPa coverage | **Checked on real data** | UKV has 850 hPa wind from 2024-08 onwards (partial in 2024-08 and 2024-11), none before; ERA5 has none. Median ws850/ws10 (UKV, Ben Nevis, ws10 ≥ 2 m/s) = 1.87. |
| 10 m → ridge-wind fallback (`calibrate_wind` / `crag_calib`) | **Implemented 2026-10-09** (re-done: the earlier version had been lost) and **running on the Pi**; all features fully recomputed. | Offline test (fake 850/10 m ratio 1.8 recovered exactly; a >5% factor change forces a full recompute). Real factors: ukv ×1.63–2.27, era5 ×2.75–3.91 (ERA5's ~25 km terrain is lower and smoother, so its 10 m wind is lighter). fc has too little overlap and uses the 1.6 default, but always has 850 hPa anyway. Ben Nevis Feb 2024 (10 m only): mean ridge wind 8.7 → 16.2 m/s, lee loading 114 → 177 mm, rime-face 18 → 23 h. Feb 2025 (has 850 hPa) unchanged. Calibration adds ~10 s per 3-hourly run; a full recompute takes ~4 min. Pre-change DB backup: `~/weather-backup-2026-10-09.sqlite` on the Pi (326 MB, delete once happy). |
| Crag coordinates | **Checked by the user** on 2026-09-26 with `web/crag-check.html`, and now the defaults in `crags.py` and `config.example.toml`. | — |
| Crag dashboard (`rainarchive/dashboard.py`, `web/dashboard/`) | **Running on the Pi** since 2026-10-09 as systemd service `rain-archive-dashboard`, `http://pi4.local:8080/` on the LAN; Tailscale installed on the Pi for phone access away from home | Offline test (`tests.test_dashboard_offline`) on the Mac and the Pi; endpoints checked against real data (board 38 KB / 75 ms, crag 35 KB / 30 ms, status 2 s uncached); views screenshotted at phone width. Minimal phase-2 version: board, crag view (temperature, ridge wind in mph + arrows, precipitation vs radar, SWE/loading, rime since last thaw), radar page (live frames + 1 h–7 day accumulation), status. Radar cache: ~80 s warm-up after a restart, then 183 MB RSS. The Pi kernel has `cgroup_disable=memory`, so systemd `MemoryMax` caps are **not enforced** for any service. |
| RuuviTag logger (`rainarchive/ruuvi.py`) | **Running on the Pi** since 2026-10-16: systemd timer `rain-archive-ruuvi` (hourly) | Tag 9261 (`F6:88:CF:86:92:61`, RuuviTag B, FW 3.31.1). First real sync downloaded the full on-tag history (278 × 5-min T/RH/p records from 2026-10-15 21:48 UTC) in 69 s; hourly runs fetch only new records. Offline test `tests.test_ruuvi_offline`. Bluetooth had to be unblocked (`rfkill`); `python3-bleak` installed from apt. |
| UK radar web viewer (`web/uk-rain-radar.html`) | **Working with live data** in the user's browser. **Not in the project folder on 2026-10-09** (a copy is in `~/Downloads`; `crag-check.html` wasn't found). | Background map switched from CARTO (now needs an API key) to Esri. Accumulation works UK-wide after adding request rate limiting. |

### Immediate open items

1. **Investigate `frames.unmatched_px` on the Pi.** It is not ~0: mean ≈12,700 px/frame (max 55,760,
   nonzero in 2002/2010 frames). With ~12% of 1.3 M pixels wet, that is roughly 8% of echo pixels
   resolved by nearest colour. Verify the Universal Blue table in `decode.py` against
   `https://www.rainviewer.com/files/rainviewer_api_colors_table.csv` (5–65 dBZ), and check whether
   the misses are anti-aliased edge pixels or a real table error.
2. **Sanity-check features over real winters**, ideally against known conditions (SAIS reports, UKC
   logbooks). Ben Nevis Feb 2024 looks plausible for temperature/refreezes/snow (tmin −11.3 °C on
   25 Feb, frozen-precip events of 20–28 mm, SWE 340→470 mm). Open question: ERA5 factors of ~3.5–3.9
   applied to all hours may over-inflate light-wind hours. Check the ERA5 vs UKV (`best`) rime/loading
   distributions over the 2024–26 overlap.
   ```sql
   SELECT date, round(tmin,1) tmin, round(tmax,1) tmax, refreezes, days_since_thaw,
          round(frozen_precip_mm,1) snow_mm, round(rime_face_hrs) rime_face,
          round(lee_loading_mm,1) loading, round(swe_model_mm) swe
   FROM crag_daily WHERE source='best' AND crag='Ben Nevis'
     AND date BETWEEN '2024-02-01' AND '2024-02-29';
   ```

---

## 2. Next steps / roadmap

Roughly in priority order. Each item says what "done" looks like.

1. **Finish validating the weather extension on the Pi** (§1 item 2). Deploy, backfill and
   wind calibration were done and checked on 2026-10-09. Done when: a known winter looks right.
2. **Validate model temperatures against observations.**
   - Ingest the Heriot-Watt **Cairngorm summit automatic weather station** (1245 m, since 1977).
     Check the format on its [archive page](https://cairngormweather.eps.hw.ac.uk/archive.htm) first.
   - Optionally ingest **Met Office MIDAS Open** hourly observations from CEDA. This needs a free
     account; check its station list for high-level Scottish sites.
   - Compare with `wx_hourly` at the Northern Corries/Shelter Stone and store the bias per hour and season.
   Done when: a table of model-minus-observed temperature/RH/wind by month exists.
   Planned in detail in [`DASHBOARD_PLAN.md`](DASHBOARD_PLAN.md) §2.5, along with Holfuy stations. Found on
   2026-09-28: the AWS CSV wind is in m/s; 2024 to ~2025-09 is a fault period; the live feed stopped on 2026-08-31.
3. **Field logger: RuuviTag + Coros watch GPS.** The user bought a RuuviTag (T/RH/p, logs every
   5 min on the tag, ~10 days). It lives at home and is sometimes carried up hills; the Pi checks
   for it hourly and back-fills the tag's log when it returns (done 2026-10-16, see §1 and
   `ruuvi.sqlite`). **Next:** import GPS tracks from the user's Coros watch, time-join them with
   `ruuvi_history` to tag readings with position/altitude and the nearest crag, flag "at home" vs
   "on the hill", and compare with `wx_hourly` for the same hour and height.
   - Coros exports FIT/GPX; check the format (and whether it records barometric altitude) first.
   - Tag readings with the nearest crag within 1 km and store the joined result (e.g. a
     `field_obs` table), then add a view comparing it with `wx_hourly` for the same hour.
   - The tag sits in a pocket or pack: expect body-heat bias unless it is carried outside.
4. **Scottish Avalanche Information Service (SAIS) reports**, now a research thread with its own
   log: [`research/SAIS_LOG.md`](research/SAIS_LOG.md). The headline hazard calendars for 2007–2026
   are scraped (`rainarchive/sais.py`), and a first weather → hazard baseline exists. Full reports
   (observed hazard, problems, aspect rose, recorded avalanches) are next.
5. **Dashboard.** Plan: [`DASHBOARD_PLAN.md`](DASHBOARD_PLAN.md). A minimal phase-2 version is live
   (§7, Dashboard). Next: freezing level / profile panels once phase 1a adds those variables, station
   overlays after phase 1b, then the phase-3 views (aspect rose, season context, conditions flags).
6. **Conditions model**, once enough `observations` exist. Start with transparent rules or logistic
   regression on the `conditions` view; use the `era5` source for multi-season consistency; watch
   the class balance (log bad days too).
7. **Historical radar backfill (optional).** The Met Office 1 km NIMROD composite archive is on
   CEDA. RainViewer only has 2 hours of history, so the Pi's archive only starts 2026-09-25.
8. **Host the radar viewer for phone use (optional).** GitHub Pages; the file is self-contained.
9. **Fixed crag sensor (optional, later).** Discussed: an Iridium/RockBLOCK satellite link, or LTE-M
   where there's signal. The CIC hut (SMC) was suggested as the best first site. Needs landowner/SSSI
   permission; rime and snow burial are the main engineering problems.

---

## 3. Repository map

```
rain-archive/
├── README.md                    this file
├── config.example.toml          copy to config.toml (radar + weather + crags)
├── install.sh                   Pi install for the radar collector (apt deps, self-test, live poll, systemd)
├── install-weather.sh           Pi install for the weather timer (self-test, systemd timer, first run)
├── install-dashboard.sh         Pi install for the dashboard service (self-test, systemd, port 8080)
├── install-ruuvi.sh             Pi install for the hourly RuuviTag check (bleak, rfkill, systemd timer)
├── systemd/
│   ├── rain-archive.service             always-on radar collector (Restart=always)
│   ├── rain-archive-weather.service     oneshot weather update
│   ├── rain-archive-weather.timer       every 3 h (00/3:17 + random delay), Persistent=true
│   ├── rain-archive-dashboard.service   always-on read-only web dashboard (port 8080)
│   ├── rain-archive-ruuvi.service       oneshot RuuviTag check (scan, broadcast sample, history download)
│   └── rain-archive-ruuvi.timer         hourly (+ up to 2 min random delay), Persistent=true
├── rainarchive/
│   ├── __main__.py      radar CLI: run | once | status | build-daily | render
│   ├── config.py        radar config (TOML); grid bbox; sites
│   ├── grid.py          Web Mercator tile/pixel grid (z7, x60–63, y36–40 → 1024×1280 px)
│   ├── decode.py        Universal Blue colour → dBZ → mm/h (Marshall–Palmer, cap 55 dBZ)
│   ├── fetcher.py       RainViewer client: throttled (0.8 s), retries, 429 back-off
│   ├── store.py         radar.sqlite schema, atomic PNG writes, stats, site series, daily totals, pruning
│   ├── collector.py     poll loop: fetch every missing frame, daily builds, status.json
│   ├── query.py         read-only Archive API for dashboards/notebooks
│   ├── crags.py         Crag dataclass + DEFAULT_CRAGS (user-verified)
│   ├── openmeteo.py     Open-Meteo client: ukv / era5 / fc sources, call weighting, pacing
│   ├── features.py      daily conditions features (pure Python; thresholds at the top)
│   ├── render.py        radar grid → RGBA images (frame colours, accumulation scale)
│   ├── ruuvi.py         RuuviTag over Bluetooth: broadcast decode, on-tag log download → ruuvi.sqlite
│   ├── sais.py          SAIS forecast-archive scraper → sais.sqlite (research labels)
│   ├── weather.py       weather.sqlite schema, budgeted backfill, wind calibration, features, CLI
│   └── dashboard.py     stdlib HTTP server: JSON API + static page, read-only, cached per data update
├── tests/
│   ├── test_offline.py          radar pipeline against a fake RainViewer
│   ├── test_weather_offline.py  features + weather pipeline against a fake Open-Meteo
│   ├── test_dashboard_offline.py  dashboard API/static serving against a fake-built weather.sqlite
│   ├── test_sais_offline.py       SAIS calendar scraper against a fake sais.gov.uk
│   └── test_ruuvi_offline.py      RuuviTag decode + download/resume against a fake Bluetooth backend
├── research/
│   ├── SAIS_LOG.md              research log: predicting SAIS hazard from weather (+ radar)
│   ├── sais_baseline.py         baseline models (Mac only: scipy + scikit-learn)
│   └── results/                 generated result tables
└── web/
    ├── dashboard/               index.html, app.js, app.css, vendor/ uPlot 1.6.32 + Leaflet 1.9.4 (vendored)
    ├── uk-rain-radar.html       live UK radar viewer (Leaflet, RainViewer; loop + accumulation)
    └── crag-check.html          draggable crag map → emits a [[crags]] TOML block
```

About 2,000 lines of Python. The only dependencies are **numpy, Pillow, requests** (apt packages on
the Pi) and the standard library (`sqlite3`, `tomllib`, `zoneinfo`).

---

## 4. Working on this project (conventions for Claude Code)

- **Target runtime: the Pi runs Debian 13 (trixie), Python 3.13, numpy 2.2, Pillow 11.1 from apt**
  (checked 2026-10-09; earlier notes assumed Bookworm / 3.11). Keep code working on Python ≥ 3.11.
  - Don't add dependencies without a good reason. If you must, prefer an apt package (`python3-xyz`) over pip.
  - Still avoid numpy-2-only or Pillow-11-only APIs so the code also runs on older installs. For example, don't pass `mode=` to `Image.fromarray` (deprecated).
- **Everything is UTC internally** (unix seconds).
  - Radar daily totals use UTC days.
  - Weather features use **Europe/London local days** (configurable) because climbing days are local.
- **Never lose data on power cuts:**
  - Write files to a temp name and `os.replace` them.
  - SQLite runs in WAL mode.
  - Readers (`query.Archive`, dashboards) open the databases **read-only**.
- **Respect the free API limits:**
  - **RainViewer:** 100 requests/IP/minute, max zoom 7, Universal Blue scheme only, ~2 h of past
    frames at 10 min spacing, no nowcast (limits in force since Jan 2026). The collector spaces
    requests 0.8 s apart; `config.py` refuses anything under 0.65 s.
  - **Open-Meteo (free, non-commercial):** roughly 10,000 calls/day, 5,000/hour and 600/minute, where
    a request counts as `max(1, days/14) × max(1, n_vars/10)` calls. The code tracks usage in
    `api_usage` and enforces `daily_call_budget` (7000) and `hourly_call_budget` (3500), pacing to
    400 weighted calls/min.
- **Tests must pass offline**; neither test suite touches the network:
  ```bash
  python3 -m tests.test_offline && python3 -m tests.test_weather_offline
  ```
  Add a fake-server test for any new data source, in the style of the existing ones.
- **Keep the archive formats stable.** Frame PNG encoding (uint8: 0 dry, 5–95 dBZ, 255 no data),
  the daily PNG encoding (uint16, mm×100, 65535 no data) and the grid are recorded in the `meta`
  table. `Store` refuses to open an archive whose grid doesn't match the config. Schema changes must
  be additive (`CREATE TABLE IF NOT EXISTS`, new columns with defaults) because the Pi's databases
  already exist.
- The user is an algorithm engineer (sensing, probabilistic inference, JAX/Python). Explain the
  science/assumptions briefly; don't over-explain Python. Prefer transparent, documented heuristics
  over black boxes.
- **Crag geometry** (`lat, lon, elev_m, aspect`) drives the temperature downscaling and the
  rime/loading features. If a crag changes, run `refetch "<crag>"`.

---

## 5. Development and deployment workflow

### 5.1 Local development (Mac)

```bash
cd rain-archive
python3 -m venv .venv && source .venv/bin/activate      # Python ≥ 3.11
pip install numpy pillow requests
python3 -m tests.test_offline && python3 -m tests.test_weather_offline

cp config.example.toml local.toml
sed -i '' 's|^data_dir = .*|data_dir = "./local-data"|' local.toml
sed -i '' 's|^era5_start = .*|era5_start = "2025-10-01"|' local.toml   # short backfill for testing
python3 -m rainarchive.weather -c local.toml update
python3 -m rainarchive.weather -c local.toml show "Ben Nevis"
```

`local-data/` from the 2026-09-26 test may still exist on the Mac, with UKV 2022→ for all crags.
Run `features --all` after pulling the wind-calibration change.

### 5.2 Deploying to the Pi

The Pi holds `~/rain-archive` (code + `config.toml`) and the data in `data_dir` (default
`~/rain-archive-data`; the user may have moved it to a USB SSD, so check `config.toml`).

```bash
# from the Mac (rsync is nicer than the zip used so far; never overwrite the Pi's config.toml)
rsync -av --exclude .git --exclude .venv --exclude __pycache__ --exclude local-data --exclude local.toml --exclude config.toml \
      ./ murray@pi4.local:~/rain-archive/
ssh murray@pi4.local 'sudo systemctl restart rain-archive'   # only if radar code changed
```

### 5.3 Installing the weather extension on the Pi (pending)

```bash
ssh -t murray@pi4.local 'cd rain-archive && ./install-weather.sh'
# then, after 10–15 min:
ssh murray@pi4.local 'cd rain-archive && \
  systemctl list-timers rain-archive-weather.timer --no-pager; \
  journalctl -u rain-archive-weather -n 25 --no-pager; \
  python3 -m rainarchive.weather -c config.toml status; \
  python3 -m rainarchive -c config.toml status'
```

The Pi's `config.toml` was created before the weather extension existed, so it has no `[weather]` or
`[[crags]]` section. The code then falls back to built-in defaults (ERA5 from 2000; the 12
user-verified crags in `crags.py`). Copying those blocks from `config.example.toml` is optional but
makes the settings explicit.

### 5.4 Dashboard and remote access

```bash
ssh -t murray@pi4.local 'cd rain-archive && ./install-dashboard.sh'   # also re-run after code changes
```

- LAN: `http://pi4.local:8080/`. Away from home: install the Tailscale app on the phone, sign in
  with the same account as the Pi, then open `http://pi4:8080/` (MagicDNS) or the Pi's `100.x` address
  (`tailscale ip -4` on the Pi). The dashboard has no login, so never port-forward 8080.
- Tailscale was installed on the Pi on 2026-10-09 (`tailscaled` enabled at boot, hostname `pi4`).
  Consider disabling key expiry for the Pi in the Tailscale admin console so it stays connected.

### 5.5 Health checks

```bash
systemctl status rain-archive                  # radar collector running?
journalctl -u rain-archive -f                  # one line per saved frame
python3 -m rainarchive -c config.toml status   # status.json + archive size
python3 -m rainarchive -c config.toml render --latest -o latest.png
python3 -m rainarchive.weather -c config.toml status
cat <data_dir>/weather_status.json
```

---

## 6. Part A: radar archive

### What it does

- Every 15 min (aligned to :01:30, :16:30, …) it fetches RainViewer's index and downloads **every past
  frame not yet stored**, so the archive is gap-free unless the Pi is offline for more than ~2 h.
- Each frame is 20 z7 tiles (256 px) in the Universal Blue scheme, fetched with no smoothing.
  They're decoded to dBZ and mosaicked onto a fixed 1024×1280 grid (~650 m pixels at Scottish
  latitudes).
- **Partial frames:** a frame with failed tiles is retried on later polls. Once it's older than 90 min
  it's saved with the missing tiles marked as no data (255).
- It records per-frame stats, site time series and a daily total grid per UTC day (built ≥30 min
  after the day ends).

### Storage (`data_dir/`)

```
radar.sqlite                          catalogue + time series (WAL)
grid.json                             grid definition
status.json                           collector health
frames/YYYY/MM/DD/YYYYMMDDTHHMMZ.png  uint8: 0 dry, 5..65 dBZ, 255 no data
daily/YYYY/YYYY-MM-DD.png             uint16: mm × 100, 65535 no data
```

About 2–4 GB/year with all frames kept. `keep_frames_days` prunes raw frames but never a day whose
daily grid hasn't been built.

**Grid:** Web Mercator zoom 7, tiles x 60–63, y 36–40, spanning 11.25°W–0° and 54.2°N–61.7°N. Row 0 is
north. Pixel (r, c) is the global tile-pixel (x0·256+c, y0·256+r). `Grid.pixel_centres()` gives
lat/lon.

**Tables and views:**

| name | one row per | contents |
|---|---|---|
| `frames` | frame | time, path, tiles_ok/total, valid_frac, wet_frac, area-weighted mean_rate, max_dbz, unmatched_px |
| `site_obs` | frame × site | dBZ code, rate (mm/h), rate_3x3 |
| `daily` | UTC day | path, n_frames (144 = complete), mean_mm, max_mm |
| `sites` | site | lat, lon, row, col (Ben Nevis, Cairn Gorm, Edinburgh by default) |
| `meta` | key | grid JSON, encodings |
| `site_hourly` (view) | hour × site | mm in that hour |
| `site_daily` (view) | day × site | mm_observed, mm_gapfilled, n_frames |
| `frames_iso` (view) | frame | readable timestamps |

### Maths

- **Colour decoding:** an exact table lookup, then the nearest colour for any remainder. Colours more
  than 30 RGB units from the table are treated as dry. Count of nearest-colour matches → `unmatched_px`.
- **Rain rate:** Marshall–Palmer, Z = 200 R^1.6, with dBZ capped at 55 (limits hail/clutter).
- **Accumulations:** gap-filled as the mean rate over available frames × the period.

### Read API

```python
from rainarchive.query import Archive
a = Archive("~/rain-archive-data")
a.site_series("Ben Nevis", "2026-10-01", "2026-11-01")   # [(t, mm/h)]
mm, coverage = a.accumulate("2026-10-01", "2026-10-08")
a.daily("2026-10-01"); a.daily_sum("2026-10-01", "2026-10-31")
a.point_series(lat, lon, start, end)                      # any point, from raw frames
```

### Radar CLI

`python3 -m rainarchive -c config.toml {run|once|status|build-daily [--date D|--all]|render [--latest|--time T|--daily D] -o out.png}`

---

## 7. Part B: crag weather extension

### Sources (all hourly, UTC, m/s, °C, mm)

| key | provider / model | span | fields |
|---|---|---|---|
| `ukv` | Open-Meteo Historical Forecast API, `ukmo_uk_deterministic_2km` | 2022-03-01 → yesterday | surface + 850 hPa (T, RH, wind, geopotential) |
| `era5` | Open-Meteo Historical Weather API, `era5` (~25 km) | `era5_start` (default 2000-01-01) → today−6 | surface only |
| `fc` | Open-Meteo Forecast API, `ukmo_seamless` | yesterday → +7 days | surface + 850 hPa; replaced every run |

- **Temperature at crag height:** every request passes `elevation=elev_m`, and Open-Meteo downscales
  2 m temperature to that height with a lapse rate.
- **Wind at crag height:** model terrain is far below the real crags, so for ridge-level wind the code
  uses **850 hPa** (~1,300–1,500 m).
- **Fallback wind:** where 850 hPa isn't available (all ERA5, and part of the UKV archive), it uses
  10 m wind × a per-crag, per-source factor. The factor is the median ratio of UKV/fc 850 hPa wind to
  that source's 10 m wind over hours with 10 m wind ≥ 2 m/s. It's stored in `crag_calib`, with a
  default of 1.6 if there are fewer than 200 overlapping hours. If a factor moves by more than 5%, that
  crag's features are fully recomputed.
- **Unknown variables:** if Open-Meteo rejects the variable list (HTTP 400), the client retries with
  surface variables only.

### Scheduling and backfill

- **Timer:** runs every 3 h. Each run:
  1. Refreshes the forecast for all crags.
  2. Fetches the history in calendar-year chunks per crag, **newest first**, while the budget allows.
     Progress is kept in `fetch_log` (`fetched_to`). The current year's chunk is topped up from
     `fetched_to − 2 days`, so a Pi that was down for weeks catches up.
  3. Updates `radar_daily`.
  4. Recomputes features: the last 60 days (with 150 days of warm-up), or a full history when older
     data has arrived or the wind factors changed.
- **Full backfill:** ERA5 2000→ plus UKV 2022→ for 12 crags is ≈10.5k weighted calls, so about 2 days.

### Tables (`data_dir/weather.sqlite`, WAL)

| table | key | contents |
|---|---|---|
| `crags` | name | lat, lon, elev_m, aspect, area |
| `wx_hourly` | (source, crag, time) | t2m rh2m precip snowfall ws10 wd10 gust10 cloud_low t850 rh850 ws850 wd850 z850 |
| `crag_daily` | (source, crag, date) | features (below) + radar_mm; `source` = `best` (ukv > fc > era5 per hour, `basis` = majority source) or `era5` (consistent 2000→ record) |
| `radar_daily` | (crag, date) | radar mm at the crag (3×3 px), local day, needs ≥100 of 144 frames |
| `crag_calib` | (crag, source) | 10 m → ridge wind factor, n_hours |
| `observations` | id | the user's conditions log: date, crag, route, rating 0–4, type, notes |
| `fetch_log`, `api_usage` | — | backfill bookkeeping, weighted API calls per UTC hour |
| `conditions` (view) | — | `crag_daily` (best) LEFT JOIN `observations`, the training table |

### Daily features (`features.py`; thresholds are constants at the top)

- **Temperature:** `tmin tmax tmean hrs_below0 pdh fdh` (positive/freezing degree-hours)
- **Freeze–thaw:** `refreezes thaws`, with ±0.5 °C hysteresis and state carried across days. `days_since_thaw` counts from the last day with ≥5 degree-hours above 0 °C.
- **Precipitation:** `precip_mm`, `frozen_precip_mm` (wet-bulb < 1 °C, Stull 2011), `rain_mm`, `snowfall_cm` (model)
- **Rime:** `rime_hrs`, meaning hours with RH ≥ 95%, −10 ≤ T ≤ −0.5 °C and ridge wind ≥ 5 m/s. `rime_face_hrs` counts only hours when the wind comes from within ±60° of the face aspect; `rime_index` is the sum of wind speed over rime hours.
- **Loading:** `lee_loading_mm`, frozen precipitation while ridge wind ≥ 7 m/s blows from within ±60° of aspect+180°
- **Wind and humidity:** `wind_mean wind_max wdir_mean` (vector mean) and `rh_mean`
- **Snowpack:** `swe_model_mm`, a degree-day snowpack (+ frozen precip, − 3 mm/°C·day melt, − 0.5 × rain)
- **Rolling totals:** `refreeze_7d thaw_7d frozen_precip_7d frozen_precip_14d rime_face_hrs_7d lee_loading_7d pdh_3d fdh_7d rain_7d`
- **Bookkeeping:** `n_hours` (days with fewer than 18 hours are skipped) and `basis`

### Weather CLI

```bash
python3 -m rainarchive.weather -c config.toml update            # what the timer runs
python3 -m rainarchive.weather -c config.toml status
python3 -m rainarchive.weather -c config.toml show "Ben Nevis" --days 21
python3 -m rainarchive.weather -c config.toml features --all
python3 -m rainarchive.weather -c config.toml refetch "Lochnagar" ...
python3 -m rainarchive.weather -c config.toml log "Ben Nevis" 2027-02-14 --rating 3 --type ice \
        --route "Point Five" --notes "fat, well bonded"
```

---

### Dashboard (`rainarchive/dashboard.py`, `web/dashboard/`)

`python3 -m rainarchive.dashboard -c config.toml [--port 8080]`. Standard library only. Opens the
databases read-only and caches each response until `weather_status.json` or the radar `status.json`
changes, or the local date rolls over.

- **API:** `/api/board` (−7…+7 days for all crags), `/api/crag/<name>?from&to` (hourly merged
  `best` series with the ridge-wind basis per hour, plus daily features; default −21…+7, max 400
  days), `/api/status`. Only whitelisted static files are served.
- **Board:** cell colour = thermal state (max ≤ 0 °C frozen, min ≥ 0 °C thawed, else freeze–thaw);
  number = frozen precipitation mm; marks for lee loading ≥ 5 mm, rime on face ≥ 3 h, rain ≥ 1 mm.
- **Crag view:** temperature with the ±0.5 °C hysteresis band; ridge wind in mph (solid = 850 hPa,
  dashed = 10 m × calibrated factor; the API stays in m/s) with arrows coloured onto-face /
  over-the-top / cross using the same ±60° rule as `features.py`; daily precipitation (frozen vs
  rain) with radar dots; modelled SWE and lee loading; hourly rime-on-face hours accumulated
  since the last sustained thaw (T > 1 °C for > 4 consecutive hours, `THAW_RESET_*` in
  `features.py`; thaws shaded and reset the counter; 60 days of warm-up history).
- **Radar page:** Leaflet map (vendored; Esri grey basemap tiles fetched by the phone).
  "Now" plays the last 6 h of frames in RainViewer colours. "Accumulated" sums the last 1 h to
  7 days (slider) from an in-memory cache of hourly totals at half resolution (~1.3 km, float16,
  ~110 MB), filled by a background thread. A window is the last N whole hours plus the current
  partial hour, so it can run up to 50 minutes longer than N. A complete hour with missing frames
  is filled at that hour's mean rate. Per-crag values are means over a 3×3 box of half-resolution
  pixels (≈4 km).
  Colour scale: windows ≤ 3 h use fixed thresholds (0.2 … 150 mm); longer windows use
  `adaptive_stops`, which places the 10 thresholds at equal-area quantiles of the pixels with
  ≥ 0.2 mm, rounded to readable values, so each colour covers ~10% of the area with rain. Colours
  are comparable within one map but not between windows; the legend shows the actual values.
- **Not yet:** freezing level, profile/lapse rate and station panels (need plan phases 1a/1b).

## 8. Web tools

Both are single files that load Leaflet 1.9.4 from cdnjs. Open them directly in a browser, or serve
them with `python3 -m http.server`. They **can't be published as claude.ai artifacts**, because
artifacts block external data hosts.

### `web/uk-rain-radar.html`: live UK radar viewer

- **Views:** a radar loop (~2 h, 10-min frames, play/slider, 5-minute refresh) and an **accumulated**
  mode (30 min / 1 h / 2 h totals).
- **Accumulation method:** tile pixels are decoded to dBZ, then to mm/h with Marshall–Palmer, then
  summed over frames.
- **Summit panels:** Ben Nevis and Cairn Gorm on OpenTopoMap, with the rate at the summit and the total.
- **Point readout:** click anywhere on the map.
- **On-map colour bars:** for the rain rate, with ticks placed by their true dBZ position, and for
  accumulation.
- **Background maps:** Esri Light Grey (default), Esri Streets and OpenTopoMap. CARTO was removed
  because it now needs an API key.
- **Rate limiting:**
  - One shared tile store, so each (frame, z, x, y) is fetched once and used for both display and maths.
  - A request queue capped at 90 requests/min, with retries.
  - Data tiles are only requested over the British Isles (lat 49.3–61.2, lon −10.9–2.8).
  - The main map uses data one zoom level coarser than it displays.
  - A full UK load is about 65 requests.

### `web/crag-check.html`: crag location checker

- **Maps:** 12 topo maps covering ~15–20 km each.
- **Editing:** a draggable red dot for the position, an aspect arrow, and editable height and aspect.
- **Output:** a live `[[crags]]` TOML block with a copy button.
- **Status:** the user used it on 2026-09-26 to correct 7 crags.

---

## 9. Design decisions and lessons learned

- **RainViewer (free API):** changed on 1 Jan 2026 to 100 requests/min per IP, max zoom 7, Universal
  Blue only, no nowcast or satellite. Tiles are CORS-enabled, so browser pixel decoding works.
  - Only ~2 h of history is kept, which is why the archive polls every 15 min but downloads every
    10-min frame.
- **Why PNG + SQLite for radar:** frames are mostly zero, so PNG compresses them well (dry frames
  ~2 KB). They're lossless and readable by any tool, including browser canvases. SQLite keeps the
  catalogue and point series queryable and crash-safe. Zarr/NetCDF appends were avoided because a
  Pi can lose power mid-append.
- **dBZ, not rain rate, is stored.** The Z–R relation and caps can change later without re-downloading.
- **Weather is fetched from archives rather than collected.** Unlike radar, point weather history is
  archived by Open-Meteo (UKV from 2022, ERA5 from 1940). The Pi therefore stores a local cache plus
  derived features; it doesn't need to capture data before it disappears.
- **The model's summits are too low.** A 2 km model smooths Ben Nevis far below 1345 m.
  - Temperature is handled by Open-Meteo's `elevation` downscaling.
  - Wind uses 850 hPa, with the calibrated 10 m fallback.
  - ERA5 at 25 km is worse again. Use it for consistent multi-decade statistics, not for absolute values.
- **Local-day features** because climbing days are local; everything stored hourly is UTC.
- **The `best` series blends sources** (UKV → fc → ERA5), so it has discontinuities at 2022-03 (ERA5 →
  UKV) and where the 850 hPa data starts. Use `source='era5'` for anything that needs a
  homogeneous record.
- **CARTO basemaps now need an API key.** Use Esri or OpenTopoMap.
- **Claude Cowork's shell** can't reach the Pi (no SSH keys, sandboxed), which is why deployment moves
  to Claude Code on the Mac.

---

## 10. Known limitations and open questions

- **Colour table unverified** against the raw CSV (§1 item 4). dBZ 65–74 are all white in the table and
  75 appeared green. Only 5–65 are used, and values ≥ 55 are capped for rain rate anyway.
- **Radar limitations:** radar underestimates precipitation where hills block the beam and where it
  forms below the beam (West Highlands), and overestimates in melting layers and snow. There's no
  gauge adjustment.
- **The feature thresholds are heuristics** (rime RH ≥ 95%, −10…−0.5 °C, wind ≥ 5 m/s; loading ≥ 7 m/s;
  ±60° sectors; melt 3 mm/°C·day) and should be tuned against observations.
- **Open-Meteo's height adjustment** only corrects temperature. Humidity and wind are model-grid
  values; check them against summit observations (roadmap item 2).
- **Radar history starts 2026-09-25**, so `radar_mm` is empty for earlier days. Historical radar needs
  the NIMROD archive (roadmap item 7).
- **Open-Meteo quirks:** one UKV chunk (Lochnagar 2022) timed out in the Mac test and should be retried
  automatically; check that `status` reaches 60/60. The request timeout is 120 s.
- **The Pi's hostname, username and `data_dir`** aren't recorded here; ask the user.
- **Timezone:** the user's is Europe/London.

---

## 11. Reference: crags, sources, external data

### Crags (user-verified positions, 2026-09-26)

| name | area | lat | lon | elev_m | aspect |
|---|---|---|---|---|---|
| Northern Corries | Northern Cairngorms | 57.1040 | −3.6753 | 1050 | 340 |
| Ben Nevis | Lochaber | 56.8010 | −5.0060 | 1150 | 20 |
| Lochnagar | Southern Cairngorms | 56.9559 | −3.2380 | 1000 | 30 |
| Creag Meagaidh | Creag Meagaidh | 56.9570 | −4.5850 | 950 | 60 |
| Beinn Eighe | Torridon | 57.5852 | −5.4307 | 850 | 320 |
| Beinn Bhan | Torridon | 57.4480 | −5.6655 | 700 | 75 |
| Shelter Stone Crag | Northern Cairngorms | 57.0922 | −3.6502 | 900 | 45 |
| Church Door Buttress | Glencoe | 56.6455 | −5.0290 | 1050 | 0 |
| Stob Coire nan Lochan | Glencoe | 56.6515 | −5.0130 | 1000 | 45 |
| Beinn Dorain | Southern Highlands | 56.5030 | −4.7250 | 850 | 290 |
| Beinn a' Bhuird | Southern Cairngorms | 57.0950 | −3.4729 | 1000 | 20 |
| A' Mhaighdean | Fisherfield | 57.7195 | −5.3475 | 850 | 315 |

Heights and aspects are judgement calls for the classic winter lines, so refine them as needed. Aspect
is the direction the faces look towards.

### APIs and data

- RainViewer Weather Maps API: https://www.rainviewer.com/api/weather-maps-api.html
  - Limits (since Jan 2026): https://www.rainviewer.com/api/transition-faq.html
  - Colour table: https://www.rainviewer.com/files/rainviewer_api_colors_table.csv
- Open-Meteo:
  - UK Met Office models: https://open-meteo.com/en/docs/ukmo-api
  - Historical Forecast API (UKV from 2022-03-01): https://open-meteo.com/en/docs/historical-forecast-api
  - Historical Weather API (ERA5 from 1940): https://open-meteo.com/en/docs/historical-weather-api
- Cairngorm summit automatic weather station (Heriot-Watt): https://cairngormweather.eps.hw.ac.uk/current.htm (archive: `/archive.htm`)
- Met Office MIDAS Open (CEDA): https://catalogue.ceda.ac.uk/uuid/dbd451271eb04662beade68da43546e1/
- Met Office NIMROD radar archive (CEDA): search "NIMROD" on data-search.nerc.ac.uk
- Met Office Weather DataHub (official radar/forecast APIs, key required): https://www.metoffice.gov.uk/services/data/met-office-weather-datahub

### Field hardware discussed (not yet bought)

- **Climbing-day logger:** Kestrel DROP D3 (T/RH/pressure, onboard logging, CSV) plus a phone GPX track.
  Alternatives are a RuuviTag Pro (lighter and cheaper, but check the cold rating and whether the
  model has pressure) or a Garmin watch with a barometer + tempe (FIT files; no RH; stores
  barometric altitude, not raw pressure).
- **Fixed sensor:** LTE-M/NB-IoT loggers (IoTLogger, Digital Matter Hawk, NORVI) only work where
  there's signal. RockBLOCK (Iridium) or Myriota satellite links work anywhere.
