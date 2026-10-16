# SAIS baseline — headline hazard from ERA5 features

Rows: 11332 area-days with a previous-day label, seasons 2009/10–2025/26 (17 seasons), 6 areas. Leave-one-season-out CV.

Class counts: Low 3824, Moderate 3436, Considerable 3608, High+ 464; hazard changed from the previous day on 28% of rows.

| model | accuracy | within ±1 | quadratic κ | MAE | accuracy on change days |
|---|---|---|---|---|---|
| climatology (majority class) | 0.196 | 0.500 | -0.343 | 1.344 | 0.181 |
| climatology (area × month majority) | 0.372 | 0.739 | 0.146 | 0.902 | 0.295 |
| persistence (yesterday's hazard) | 0.716 | 0.984 | 0.795 | 0.300 | 0.000 |
| ordinal logit, weather @issue | 0.593 | 0.963 | 0.666 | 0.444 | 0.441 |
| ordinal logit, weather @issue + era flag | 0.646 | 0.983 | 0.740 | 0.371 | 0.467 |
| ordinal logit, weather @issue + yesterday | 0.721 | 0.988 | 0.797 | 0.291 | 0.145 |
| ordinal logit, weather @issue + era + yesterday | 0.730 | 0.991 | 0.809 | 0.280 | 0.209 |
| ordinal logit, weather perfect-prog | 0.605 | 0.969 | 0.687 | 0.426 | 0.481 |
| ordinal logit, weather perfect-prog + yesterday | 0.733 | 0.991 | 0.813 | 0.276 | 0.242 |
| gradient boosting, weather @issue | 0.599 | 0.959 | 0.662 | 0.443 | 0.440 |
| gradient boosting, weather @issue + era | 0.664 | 0.982 | 0.749 | 0.354 | 0.467 |
| gradient boosting, weather @issue + yesterday | 0.723 | 0.989 | 0.797 | 0.288 | 0.234 |
| gradient boosting, weather perfect-prog + yesterday | 0.736 | 0.992 | 0.813 | 0.272 | 0.320 |
| gradient boosting, weather perfect-prog + era + yesterday | 0.749 | 0.995 | 0.829 | 0.256 | 0.377 |

## Per area (accuracy)

| area | rows | persistence | ordinal logit @issue | ordinal logit @issue + yesterday |
|---|---|---|---|---|
| Creag Meagaidh | 1941 | 0.698 | 0.534 | 0.697 |
| Glencoe | 1944 | 0.713 | 0.613 | 0.725 |
| Lochaber | 2062 | 0.699 | 0.565 | 0.702 |
| Northern Cairngorms | 2094 | 0.697 | 0.604 | 0.710 |
| Southern Cairngorms | 1987 | 0.725 | 0.595 | 0.728 |
| Torridon | 1304 | 0.793 | 0.678 | 0.791 |

## Season means — weather-only ordinal logit @issue (LOSO)

| season | rows | mean SAIS level | mean model level | bias | accuracy |
|---|---|---|---|---|---|
| 2009/10 | 532 | 2.93 | 2.49 | -0.44 | 0.53 |
| 2010/11 | 561 | 2.57 | 2.12 | -0.45 | 0.52 |
| 2011/12 | 523 | 2.27 | 1.84 | -0.43 | 0.48 |
| 2012/13 | 629 | 2.58 | 2.31 | -0.27 | 0.57 |
| 2013/14 | 630 | 2.71 | 2.54 | -0.17 | 0.58 |
| 2014/15 | 677 | 2.60 | 2.16 | -0.44 | 0.52 |
| 2015/16 | 727 | 2.43 | 1.99 | -0.44 | 0.53 |
| 2016/17 | 620 | 1.84 | 1.59 | -0.25 | 0.64 |
| 2017/18 | 720 | 2.17 | 2.37 | +0.20 | 0.61 |
| 2018/19 | 720 | 1.61 | 1.72 | +0.12 | 0.68 |
| 2019/20 | 600 | 1.95 | 2.18 | +0.23 | 0.65 |
| 2020/21 | 784 | 1.90 | 2.20 | +0.30 | 0.60 |
| 2021/22 | 807 | 1.55 | 1.81 | +0.26 | 0.62 |
| 2022/23 | 740 | 1.66 | 1.81 | +0.16 | 0.61 |
| 2023/24 | 702 | 1.62 | 1.82 | +0.20 | 0.55 |
| 2024/25 | 636 | 1.35 | 1.58 | +0.23 | 0.68 |
| 2025/26 | 724 | 1.85 | 1.95 | +0.10 | 0.67 |

Cross-era, trained < 2016, tested ≥ 2016: mean SAIS 1.75, mean model 2.45, accuracy 0.374.

Cross-era, trained ≥ 2016, tested < 2016: mean SAIS 2.58, mean model 1.94, accuracy 0.406.

## Confusion matrix — ordinal logit, weather @issue (rows = SAIS, columns = model)

| | Low | Moderate | Considerable | High+ |
|---|---|---|---|---|
| Low | 2747 | 908 | 169 | 0 |
| Moderate | 979 | 1512 | 940 | 5 |
| Considerable | 183 | 928 | 2414 | 83 |
| High+ | 7 | 52 | 353 | 52 |

## Standardised coefficients — ordinal logit, weather @issue, all seasons

Positive = pushes towards higher hazard (per 1 SD of the feature).

| feature | β |
|---|---|
| tmax | -0.49 |
| swe_model_mm | +0.47 |
| frozen_precip_mm | +0.45 |
| frozen_precip_3d | +0.41 |
| tmin | -0.37 |
| wind_mean | +0.36 |
| frozen_precip_7d | +0.32 |
| fdh_7d | +0.32 |
| wind_max | +0.30 |
| days_since_thaw | +0.27 |
| pdh_3d | -0.25 |
| drift_0 | -0.25 |
| rh_mean | +0.13 |
| rain_7d | -0.13 |
| drift_3d | -0.08 |
| tmax_change | +0.07 |
| rain_mm | -0.03 |
| refreeze_7d | +0.01 |
