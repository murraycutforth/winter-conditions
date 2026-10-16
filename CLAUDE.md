# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here

**Read `README.md` first, especially §1 (Current status/handover) and §2 (Next steps).** It is
written as the handover brief for this project and is kept up to date with what's done, what's
pending, and what to verify next. This file only adds quick-reference commands and orientation;
it does not repeat the README's content.

Git repository (local only, no remote; initialised 2026-10-09). `.gitignore` excludes `local-data/`,
`local.toml`, `config.toml` and SQLite files.

## Commands

```bash
# setup (Python >= 3.11; only deps are numpy, pillow, requests)
python3 -m venv .venv && source .venv/bin/activate
pip install numpy pillow requests

# tests (offline only, no network access — required before considering any change done)
python3 -m tests.test_offline
python3 -m tests.test_weather_offline
python3 -m tests.test_dashboard_offline
python3 -m tests.test_sais_offline
python3 -m tests.test_ruuvi_offline

# local run (radar collector)
python3 -m rainarchive -c local.toml {run|once|status|build-daily [--date D|--all]|render [--latest|--time T|--daily D] -o out.png}

# local run (crag weather extension)
python3 -m rainarchive.weather -c local.toml update
python3 -m rainarchive.weather -c local.toml status
python3 -m rainarchive.weather -c local.toml show "Ben Nevis" --days 21
python3 -m rainarchive.weather -c local.toml features --all
python3 -m rainarchive.weather -c local.toml refetch "<crag name>"

# research: SAIS avalanche labels + baseline (log in research/SAIS_LOG.md; Mac only, needs scipy + sklearn)
python3 -m rainarchive.sais -c local.toml {calendar [--season Y]|status}
python3 research/sais_baseline.py

# RuuviTag (on the Pi: needs Bluetooth + apt python3-bleak; hourly timer rain-archive-ruuvi)
python3 -m rainarchive.ruuvi -c config.toml {sync|status|show [--hours 24]}

# local run (dashboard, then open http://localhost:8080/)
python3 -m rainarchive.dashboard -c local.toml --port 8080
```

`local.toml` (gitignore-equivalent: not committed, points `data_dir` at `./local-data`) is the Mac
dev config; `config.toml` on the Pi is the deployed one and must never be overwritten by a deploy.

## Architecture

Two independent pipelines sharing one repo, config file, and SQLite-on-disk storage convention
(WAL mode, atomic writes via temp-name + `os.replace`, readers open read-only).

- **Radar archive** (`rainarchive/{collector,store,fetcher,grid,decode,query}.py`): polls
  RainViewer every 15 min, downloads every unfetched 10-min frame, decodes Universal Blue tile
  colours to dBZ, mosaics onto a fixed Web Mercator grid, and stores per-frame stats + daily
  totals in `radar.sqlite`. `query.Archive` is the read-only API for anything downstream
  (dashboards, notebooks). Entry point: `rainarchive/__main__.py`.

- **Crag weather extension** (`rainarchive/{openmeteo,weather,features,crags}.py`): pulls hourly
  weather (UKV 2 km, ERA5, forecast) from Open-Meteo per crag into `weather.sqlite`, calibrates a
  10 m → 850 hPa ridge-wind fallback per crag/source, and derives daily winter-conditions features
  (freeze-thaw, rime, lee loading, snowpack) in `features.py`, where thresholds live as named
  constants at the top of the file. Entry point: `python3 -m rainarchive.weather`.

Both pipelines are budget/rate-limit aware against their respective free-tier APIs (see README §4),
and both write additive-only SQLite schemas (`CREATE TABLE IF NOT EXISTS`, new columns get
defaults) because production databases on the Pi already exist and must not be broken by a schema
change.

- **Dashboard** (`rainarchive/dashboard.py` + `web/dashboard/`): stdlib HTTP server on the Pi
  (port 8080, systemd `rain-archive-dashboard`) serving a read-only JSON API over both databases
  and one vanilla-JS page with vendored uPlot + Leaflet. A background thread keeps an in-memory
  hourly radar-accumulation cache for the radar page. No auth; reached over the LAN or Tailscale.

`web/` was also meant to hold two standalone HTML tools (live radar viewer, crag location picker);
they are not in the project folder at present (README §1).

## Conventions

- Target runtime: the Pi runs Debian 13 trixie / Python 3.13 / numpy 2.2 / Pillow 11.1 from apt,
  but keep code compatible with Python >= 3.11. Don't add dependencies without a good reason; prefer apt packages over pip; avoid
  numpy-2-only or Pillow-11-only APIs (e.g. don't pass `mode=` to `Image.fromarray`).
- All storage is UTC internally; weather *features* use local (Europe/London, configurable)
  calendar days because climbing days are local.
- Any new data source needs an offline fake-server test in the style of `tests/test_offline.py` /
  `tests/test_weather_offline.py` — the test suites must never touch the network.
- The user is an algorithm engineer (sensing, probabilistic inference, JAX/Python): explain the
  science/assumptions briefly, don't over-explain Python, prefer transparent documented heuristics
  over black boxes.
