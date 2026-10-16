"""First baseline: predict the SAIS headline hazard from ERA5 crag features (+ persistence).

    python3 research/sais_baseline.py [--sais local-data/sais.sqlite]
                                      [--features local-data/research/pi_features.sqlite]
                                      [--out research/results/sais_baseline.md]

Research code for the Mac (uses scipy + scikit-learn; nothing here runs on the Pi).

Label   SAIS calendar hazard for (area, issue date D) = forecast valid 18:00 D -> 18:00 D+1.
Inputs  ERA5 daily features (source='era5', local days) at one representative crag per area,
        with the calibrated 10 m -> ridge wind. ERA5 for homogeneity across all seasons.
Sets    issue: weather up to and including day D (what is known at issue time, minus the
               forecaster's own field observations and weather forecast);
        pp:    issue + day D+1's actual weather ("perfect prognosis": an upper bound on what a
               perfect weather forecast would add).
CV      leave-one-season-out over seasons with labels; every model is scored on the same rows
        (days whose previous day also has a label, so persistence is defined).
Models  climatology (most frequent class), persistence (yesterday's hazard), proportional-odds
        ordinal logistic regression (weather only; weather + yesterday's hazard), and
        histogram gradient boosting (same two inputs) as a flexible-model reference.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import cohen_kappa_score, confusion_matrix

AREA_CRAG = {   # representative crag per SAIS area (Glencoe's two crags share one ERA5 cell)
    "Lochaber": "Ben Nevis", "Glencoe": "Stob Coire nan Lochan", "Creag Meagaidh": "Creag Meagaidh",
    "Northern Cairngorms": "Northern Corries", "Southern Cairngorms": "Lochnagar", "Torridon": "Beinn Eighe",
}
DAY_COLS = ["tmin", "tmax", "frozen_precip_mm", "rain_mm", "wind_mean", "wind_max", "swe_model_mm",
            "days_since_thaw", "refreeze_7d", "frozen_precip_7d", "rain_7d", "pdh_3d", "fdh_7d", "rh_mean"]
NEXT_COLS = ["tmin", "tmax", "frozen_precip_mm", "rain_mm", "wind_mean", "wind_max"]
LEVEL_NAMES = {1: "Low", 2: "Moderate", 3: "Considerable", 4: "High+"}
ERA_BREAK = 2016   # first season of the lower-rating regime found on 2026-10-10 (see research/SAIS_LOG.md)


# --- data ------------------------------------------------------------------------------------------
def load(sais_path: Path, feat_path: Path):
    s = sqlite3.connect(f"file:{sais_path}?mode=ro", uri=True)
    labels = {(a, d): (lv, season) for a, d, lv, season in
              s.execute("SELECT area, date, level, season FROM sais_calendar WHERE level IS NOT NULL")}
    f = sqlite3.connect(f"file:{feat_path}?mode=ro", uri=True)
    cols = sorted(set(DAY_COLS) | set(NEXT_COLS))
    feats = {}
    for crag in AREA_CRAG.values():
        for row in f.execute(f"SELECT date, {','.join(cols)} FROM crag_daily WHERE source='era5' AND crag=?", (crag,)):
            feats[(crag, row[0])] = dict(zip(cols, row[1:]))
    return labels, feats


def build(labels, feats):
    """Rows with a label, a previous-day label, and complete features for D-2..D+1."""
    X_issue, X_pp, y, prev, seasons, areas, dates = [], [], [], [], [], [], []
    for (area, d), (lv, season) in sorted(labels.items()):
        crag = AREA_CRAG[area]
        D = date.fromisoformat(d)
        p = labels.get((area, (D - timedelta(days=1)).isoformat()))
        days = {k: feats.get((crag, (D + timedelta(days=k)).isoformat())) for k in (-2, -1, 0, 1)}
        if p is None or any(v is None for v in days.values()):
            continue
        f0, fn = days[0], days[1]
        if any(f0[c] is None for c in DAY_COLS if c != "days_since_thaw") or any(fn[c] is None for c in NEXT_COLS):
            continue
        fp3 = sum(days[k]["frozen_precip_mm"] or 0 for k in (-2, -1, 0))
        drift = lambda f: (f["frozen_precip_mm"] or 0) * (f["wind_mean"] or 0)   # snow x wind: drifting proxy
        issue = [f0[c] if f0[c] is not None else 60.0 for c in DAY_COLS]        # no thaw on record -> long ago
        issue += [fp3, drift(f0), sum(drift(days[k]) for k in (-2, -1, 0)),
                  f0["tmax"] - days[-1]["tmax"]]                                 # warming trend
        nxt = [fn[c] for c in NEXT_COLS] + [drift(fn)]
        X_issue.append(issue)
        X_pp.append(issue + nxt)
        y.append(min(lv, 4))              # High and Very High merged: too rare to model separately
        prev.append(min(p[0], 4))
        seasons.append(season)
        areas.append(area)
        dates.append(d)
    names_issue = DAY_COLS + ["frozen_precip_3d", "drift_0", "drift_3d", "tmax_change"]
    names_pp = names_issue + [f"next_{c}" for c in NEXT_COLS] + ["next_drift"]
    return (np.array(X_issue, float), np.array(X_pp, float), np.array(y), np.array(prev), np.array(seasons),
            np.array(areas), np.array(dates), names_issue, names_pp)


# --- proportional-odds ordinal logistic regression -------------------------------------------------
class OrdinalLogit:
    """P(y <= k | x) = sigmoid(theta_k - x.beta), k = 1..K-1; L2 penalty on beta; features standardised."""

    def __init__(self, l2: float = 1.0):
        self.l2 = l2

    def fit(self, X, y):
        self.classes = np.unique(y)
        K, n, p = len(self.classes), *X.shape
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        Z = (X - self.mu) / self.sd
        yi = np.searchsorted(self.classes, y)

        def unpack(w):
            beta, t0, dt = w[:p], w[p], w[p + 1:]
            return beta, np.concatenate([[t0], t0 + np.cumsum(np.exp(dt))])

        rows = np.arange(n)
        hi, lo = yi, yi - 1                     # theta index of the upper / lower class boundary
        has_hi, has_lo = hi <= K - 2, lo >= 0

        def nll(w):
            """Negative log-likelihood + L2, with its analytic gradient (proportional odds)."""
            beta, theta = unpack(w)
            eta = Z @ beta
            s = expit(theta[None, :] - eta[:, None])               # (n, K-1) cumulative probs
            U = np.where(has_hi, s[rows, np.minimum(hi, K - 2)], 1.0)
            L = np.where(has_lo, s[rows, np.maximum(lo, 0)], 0.0)
            pr = np.clip(U - L, 1e-12, 1)
            dU = np.where(has_hi, U * (1 - U), 0.0)                # d sigmoid / d argument
            dL = np.where(has_lo, L * (1 - L), 0.0)
            g_eta = (dU - dL) / pr                                  # d(-log p)/d eta
            g_beta = Z.T @ g_eta + 2 * self.l2 * beta
            g_theta = np.zeros(K - 1)
            np.add.at(g_theta, hi[has_hi], -dU[has_hi] / pr[has_hi])
            np.add.at(g_theta, lo[has_lo], dL[has_lo] / pr[has_lo])
            dt = w[p + 1:]
            g_t0 = g_theta.sum()
            g_dt = np.exp(dt) * np.cumsum(g_theta[::-1])[::-1][1:]  # theta_m depends on dt_j for j <= m
            return -np.log(pr).sum() + self.l2 * beta @ beta, np.concatenate([g_beta, [g_t0], g_dt])

        w0 = np.concatenate([np.zeros(p), [-1.0], np.zeros(K - 2)])
        self.w = minimize(nll, w0, method="L-BFGS-B", jac=True).x
        self.beta, self.theta = unpack(self.w)
        return self

    def predict_proba(self, X):
        eta = ((X - self.mu) / self.sd) @ self.beta
        cdf = np.column_stack([np.zeros(len(X)), expit(self.theta[None, :] - eta[:, None]), np.ones(len(X))])
        return np.diff(cdf, axis=1)

    def predict(self, X):
        return self.classes[self.predict_proba(X).argmax(1)]


# --- evaluation ------------------------------------------------------------------------------------
def scores(y, yhat, prev):
    change = y != prev
    return {"acc": (y == yhat).mean(), "within1": (np.abs(y - yhat) <= 1).mean(),
            "qwk": cohen_kappa_score(y, yhat, weights="quadratic"), "mae": np.abs(y - yhat).mean(),
            "acc_change": (y[change] == yhat[change]).mean() if change.any() else np.nan}


def loso(make, X, y, seasons):
    yhat = np.empty_like(y)
    for s in np.unique(seasons):
        tr, te = seasons != s, seasons == s
        yhat[te] = make().fit(X[tr], y[tr]).predict(X[te])
    return yhat


class AreaMonthMajority:
    """Most frequent class for the same area and calendar month in the training seasons."""

    def fit(self, X, y):
        self.table, self.fallback = {}, np.bincount(y).argmax()
        for key in set(map(tuple, X[:, :2].astype(int))):
            m = (X[:, 0] == key[0]) & (X[:, 1] == key[1])
            self.table[key] = np.bincount(y[m]).argmax()
        return self

    def predict(self, X):
        return np.array([self.table.get(tuple(r), self.fallback) for r in X[:, :2].astype(int)])


class Majority:
    def fit(self, X, y):
        vals, counts = np.unique(y, return_counts=True)
        self.c = vals[counts.argmax()]
        return self

    def predict(self, X):
        return np.full(len(X), self.c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sais", default="local-data/sais.sqlite")
    ap.add_argument("--features", default="local-data/research/pi_features.sqlite")
    ap.add_argument("--out", default="research/results/sais_baseline.md")
    a = ap.parse_args()
    labels, feats = load(Path(a.sais), Path(a.features))
    Xi, Xp, y, prev, seasons, areas, dates, ni, npp = build(labels, feats)
    onehot = np.eye(4)[prev - 1][:, 1:]       # yesterday's hazard as indicators (Low = reference)
    era = (seasons >= ERA_BREAK).astype(float)[:, None]
    area_id = np.unique(areas, return_inverse=True)[1]
    month = np.array([int(d[5:7]) for d in dates])
    X_am = np.column_stack([area_id, month])
    out = [f"# SAIS baseline — headline hazard from ERA5 features\n",
           f"Rows: {len(y)} area-days with a previous-day label, seasons {seasons.min()}/{(seasons.min() + 1) % 100:02d}"
           f"–{seasons.max()}/{(seasons.max() + 1) % 100:02d} ({len(np.unique(seasons))} seasons), "
           f"{len(np.unique(areas))} areas. Leave-one-season-out CV.\n",
           "Class counts: " + ", ".join(f"{LEVEL_NAMES[k]} {int((y == k).sum())}" for k in sorted(np.unique(y))) +
           f"; hazard changed from the previous day on {(y != prev).mean():.0%} of rows.\n"]

    gbm = lambda: HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                                 l2_regularization=1.0, random_state=0)
    runs = [("climatology (majority class)", Majority, Xi),
            ("climatology (area × month majority)", AreaMonthMajority, X_am),
            ("persistence (yesterday's hazard)", None, None),
            ("ordinal logit, weather @issue", OrdinalLogit, Xi),
            ("ordinal logit, weather @issue + era flag", OrdinalLogit, np.hstack([Xi, era])),
            ("ordinal logit, weather @issue + yesterday", OrdinalLogit, np.hstack([Xi, onehot])),
            ("ordinal logit, weather @issue + era + yesterday", OrdinalLogit, np.hstack([Xi, era, onehot])),
            ("ordinal logit, weather perfect-prog", OrdinalLogit, Xp),
            ("ordinal logit, weather perfect-prog + yesterday", OrdinalLogit, np.hstack([Xp, onehot])),
            ("gradient boosting, weather @issue", gbm, Xi),
            ("gradient boosting, weather @issue + era", gbm, np.hstack([Xi, era])),
            ("gradient boosting, weather @issue + yesterday", gbm, np.hstack([Xi, prev[:, None]])),
            ("gradient boosting, weather perfect-prog + yesterday", gbm, np.hstack([Xp, prev[:, None]])),
            ("gradient boosting, weather perfect-prog + era + yesterday", gbm, np.hstack([Xp, era, prev[:, None]]))]
    out.append("| model | accuracy | within ±1 | quadratic κ | MAE | accuracy on change days |")
    out.append("|---|---|---|---|---|---|")
    preds = {}
    for name, make, X in runs:
        yhat = prev.copy() if make is None else loso(make, X, y, seasons)
        preds[name] = yhat
        sc = scores(y, yhat, prev)
        out.append(f"| {name} | {sc['acc']:.3f} | {sc['within1']:.3f} | {sc['qwk']:.3f} | {sc['mae']:.3f} | "
                   f"{sc['acc_change']:.3f} |")
        print(f"{name:52s} acc {sc['acc']:.3f}  qwk {sc['qwk']:.3f}  change-day acc {sc['acc_change']:.3f}")

    # per-area accuracy for the weather-only issue-time logit vs persistence
    out.append("\n## Per area (accuracy)\n")
    out.append("| area | rows | persistence | ordinal logit @issue | ordinal logit @issue + yesterday |")
    out.append("|---|---|---|---|---|")
    for ar in sorted(np.unique(areas)):
        m = areas == ar
        out.append(f"| {ar} | {m.sum()} | {(prev[m] == y[m]).mean():.3f} | "
                   f"{(preds['ordinal logit, weather @issue'][m] == y[m]).mean():.3f} | "
                   f"{(preds['ordinal logit, weather @issue + yesterday'][m] == y[m]).mean():.3f} |")

    # season-by-season bias of the weather-only model: the regime change
    yw = preds["ordinal logit, weather @issue"]
    out.append("\n## Season means — weather-only ordinal logit @issue (LOSO)\n")
    out.append("| season | rows | mean SAIS level | mean model level | bias | accuracy |")
    out.append("|---|---|---|---|---|---|")
    for s_ in np.unique(seasons):
        m = seasons == s_
        out.append(f"| {s_}/{(s_ + 1) % 100:02d} | {m.sum()} | {y[m].mean():.2f} | {yw[m].mean():.2f} | "
                   f"{(yw[m] - y[m]).mean():+.2f} | {(yw[m] == y[m]).mean():.2f} |")
    early, late = seasons < ERA_BREAK, seasons >= ERA_BREAK
    for tr, te, txt in ((early, late, f"trained < {ERA_BREAK}, tested ≥ {ERA_BREAK}"),
                        (late, early, f"trained ≥ {ERA_BREAK}, tested < {ERA_BREAK}")):
        pr = OrdinalLogit().fit(Xi[tr], y[tr]).predict(Xi[te])
        out.append(f"\nCross-era, {txt}: mean SAIS {y[te].mean():.2f}, mean model {pr.mean():.2f}, "
                   f"accuracy {(pr == y[te]).mean():.3f}.")

    # confusion matrix + coefficients of the weather-only issue-time logit
    ks = sorted(np.unique(y))
    cm = confusion_matrix(y, preds["ordinal logit, weather @issue"], labels=ks)
    out.append("\n## Confusion matrix — ordinal logit, weather @issue (rows = SAIS, columns = model)\n")
    out.append("| | " + " | ".join(LEVEL_NAMES[k] for k in ks) + " |")
    out.append("|---" * (len(ks) + 1) + "|")
    for k, row in zip(ks, cm):
        out.append(f"| {LEVEL_NAMES[k]} | " + " | ".join(str(v) for v in row) + " |")
    full = OrdinalLogit().fit(Xi, y)
    order = np.argsort(-np.abs(full.beta))
    out.append("\n## Standardised coefficients — ordinal logit, weather @issue, all seasons\n")
    out.append("Positive = pushes towards higher hazard (per 1 SD of the feature).\n")
    out.append("| feature | β |")
    out.append("|---|---|")
    for i in order:
        out.append(f"| {ni[i]} | {full.beta[i]:+.2f} |")

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(out) + "\n")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
