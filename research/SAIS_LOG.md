# Research log: predicting SAIS avalanche hazard from weather (+ radar)

Separate from the operational radar/weather work (README.md). Newest entries at the bottom.
Question: **how much of the Scottish Avalanche Information Service's daily hazard rating can be
reproduced from model weather (and later radar) at the crags?**

Code and data:
- `rainarchive/sais.py`: SAIS archive scraper → `data_dir/sais.sqlite` (+ raw pages in
  `data_dir/sais_raw/`). Test: `python3 -m tests.test_sais_offline`.
- `research/sais_baseline.py`: baseline models (Mac only: scipy + scikit-learn). Writes
  `research/results/sais_baseline.md`.
- `local-data/research/pi_features.sqlite`: snapshot of the Pi's `crag_daily` (`era5` + `best`)
  and `crag_calib`, taken 2026-10-10, after the wind calibration. Not in git.

---

## 2026-10-10 — Data source, calendar scrape, first baseline

### SAIS archive (what exists)
- `https://www.sais.gov.uk/forecast-archive/?y=<season start year>&a=<area>`: one calendar page
  per season and area, with an icon per day linking to the full report (`/<area>/?report_id=N`).
  Area codes: 2 Northern Cairngorms, 3 Lochaber, 4 Glencoe, 5 Southern Cairngorms,
  6 Creag Meagaidh, 7 Torridon. 1993–2007 is a separate legacy archive (HTML + tar.gz).
- **The calendar icon is the forecast hazard** in the report issued that day, valid 18:00 D →
  18:00 D+1 (checked on report 11253: icon Considerable = forecast Considerable; observed that day
  was Moderate).
- Full reports (not scraped yet) also hold: observed hazard; avalanche problems + trend; a
  machine-readable aspect × elevation rose in the image URL
  (`/_compassrose/?val=<8 aspects × 4 digits>&txts=600&txtm=800&txte=1300`); free text; and
  recorded avalanches (type, size, trigger, altitude, aspect).
- © sportscotland, no open licence. Personal research, fetched slowly (1 request / 3 s,
  identifying user agent, `robots.txt` allows all). Ask SAIS before publishing or redistributing.

### Calendar scrape
`python3 -m rainarchive.sais -c local.toml calendar`: 120 pages (2007/08–2026/27 × 6 areas),
0 errors, 13,190 area-days in about 6 minutes.

| area | days | Low | Moderate | Considerable | High | Very High | Na |
|---|---|---|---|---|---|---|---|
| Creag Meagaidh | 2287 | 569 | 619 | 695 | 95 | 0 | 309 |
| Glencoe | 2286 | 658 | 561 | 658 | 102 | 0 | 307 |
| Lochaber | 2437 | 632 | 591 | 782 | 106 | 0 | 326 |
| Northern Cairngorms | 2461 | 604 | 655 | 795 | 87 | 0 | 320 |
| Southern Cairngorms | 2285 | 733 | 603 | 595 | 82 | 0 | 272 |
| Torridon (from 2013/14) | 1434 | 700 | 485 | 157 | 1 | 0 | 91 |

Data facts:
- **2007/08 and 2008/09 calendars are entirely "Na".** Those seasons' ratings exist only in the
  report text ("Considerable (Category 3)"), so they need the full-report scrape.
- Very High has never been issued in this archive. High and Very High are merged as "High+" for modelling.
- Lochaber already has 6 reports for 2026/27, dated September (early-season reports).

### Baseline set-up
- Label: calendar hazard for (area, issue day D), excluding Na. Every row also needs D−1's label,
  so persistence is defined and all models are scored on the same 11,332 rows (2009/10–2025/26).
- Inputs: ERA5 daily features at one crag per area (Lochaber → Ben Nevis, Glencoe → Stob Coire
  nan Lochan, Creag Meagaidh → Creag Meagaidh, N Cairngorms → Northern Corries, S Cairngorms →
  Lochnagar, Torridon → Beinn Eighe). Features: T min/max, frozen precipitation (1, 3, 7 d), rain
  (1, 7 d), ridge wind mean/max, modelled SWE, days since thaw, refreezes over 7 d, PDH 3 d,
  FDH 7 d, RH, a drift proxy (frozen precipitation × wind) and tmax change.
  - "@issue": weather up to D only.
  - "perfect-prog": also D+1's actual weather (an upper bound on what a perfect weather forecast adds).
- Models: proportional-odds ordinal logistic regression (own implementation, analytic gradient,
  checked against finite differences and on synthetic data), and gradient boosting as a
  flexible-model reference.
- CV: leave one season out.

### Results (full table in `research/results/sais_baseline.md`)

| model | accuracy | quadratic κ | accuracy on change days |
|---|---|---|---|
| climatology, area × month majority | 0.372 | 0.146 | 0.295 |
| **persistence** (yesterday's hazard) | **0.716** | 0.795 | 0.000 |
| ordinal logit, weather @issue | 0.593 | 0.666 | 0.441 |
| ordinal logit, weather @issue + era flag | 0.646 | 0.740 | 0.467 |
| ordinal logit, weather @issue + era + yesterday | 0.730 | 0.809 | 0.209 |
| gradient boosting, weather @issue + era | 0.664 | 0.749 | 0.467 |
| gradient boosting, perfect-prog + era + yesterday | **0.749** | **0.829** | 0.377 |

The hazard changes from the previous day on 28% of rows. Nearly all errors are one level off
(within ±1 ≈ 96–99%).

### Findings
1. **Weather alone carries real signal.**
   - ERA5 at a single point, with no snowpack observations, gets 59–66% exact and about 96% within one level.
   - It predicts 44–47% of the days when the hazard changes, against 0% for persistence.
   - The coefficients make physical sense:
     - higher hazard with frozen precipitation (1 d and 3 d), modelled SWE, wind, cold (low tmax and tmin), FDH and days since thaw;
     - lower hazard with recent warmth (PDH 3 d) and with rain over 7 d.
2. **Persistence is the bar to beat, and it's high (71.6%).** Weather + yesterday's hazard only
   just beats it at issue time (73.0%). With perfect next-day weather and gradient boosting it
   reaches 74.9% and gets 38% of change days right. That is close to the ~75% agreement between
   Swiss forecasts and later field assessments (Pérez-Guillén et al. 2022), the practical ceiling
   set by how consistent the labels themselves are.
3. **There's a regime change in the labels around 2016/17.**
   - The weather-only model under-predicts every season 2009/10–2015/16 (bias −0.17 to −0.45 levels) and over-predicts every season from 2017/18 (+0.10 to +0.30). The year–bias correlation is 0.85.
   - Trained on seasons before 2016 and tested on 2016 onwards, the model predicts a mean level of 2.45 where SAIS issued 1.75.
   - High+ falls from 40–100 days per season to 0–9.
   - Season-mean SAIS level still tracks season-mean ERA5 SWE (r = 0.73), so weather explains season-to-season variation; but under similar weather, ratings since about 2016 are about 0.5–0.7 levels lower.
   - **Hypothesis (unverified):** a change in forecasting practice, e.g. adopting the EAWS
     danger-scale definitions or the avalanche-problem format.
   - A single era flag (season ≥ 2016) adds 5 points of accuracy to the weather-only model. The
     break year was chosen by eye after seeing the data, so that gain is slightly optimistic.

### Caveats
- One ERA5 point (~25 km) per area, with a ×2.75–3.91 wind factor. Weak on the local wind
  loading and aspect effects that drive Scottish hazard.
- The labels are forecasts, not truth; the observed hazard (in the full reports) is a better
  nowcast label.
- Leave-one-season-out still has autocorrelation within each held-out season. That's fine for
  accuracy, but persistence-style models look good partly because hazard is sticky.
- The "perfect-prog" rows use actual D+1 weather, so they're an upper bound, not a usable forecast.

### Next steps
1. **Check the 2016 break:** look in the SAIS annual reports (2009/10 onwards, PDFs) and the
   report format history for a change in scale or methodology; find the exact first season
   or day.
2. **Full-report scrape** (~15k pages, ~13 h at 1 request / 3 s; run on the Pi in the background):
   observed hazard, problems, rose, recorded avalanches, and the 2007–09 ratings from the text.
   Keep the raw HTML.
3. **Aspect-aware targets:** predict the rose (8 aspects × elevation band) using our
   aspect-relative loading/rime features per crag. This is where the physics should help most.
4. **Better inputs:** UKV (2 km) for 2022 onwards to test whether resolution helps; hourly drift
   features (snow while wind ≥ 7 m/s, by direction sector) instead of the daily proxy; a
   multi-crag mean per area.
5. **Radar:** the Pi archive only starts 2026-09-25, so radar can't be used for training until the
   Met Office NIMROD archive is backfilled (README roadmap item 7). It could serve as a verification
   input from this winter onwards.

---

## 2026-10-12 — Shelved

Paused by the user after the first baseline. Nothing is scheduled: the scraper only runs by hand
and nothing SAIS-related is deployed to the Pi. To resume, start from "Next steps" in the
2026-10-10 entry. The labels (`local-data/sais.sqlite`) and the feature snapshot
(`local-data/research/pi_features.sqlite`) are on the Mac only.
