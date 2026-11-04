"""Does the model carry information beyond the market price?  Out-of-sample by time split.

usage: python scripts/edge_test.py HIST_DIR
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.features import enrich_trades, load  # noqa: E402

pd.set_option("display.width", 220)


def logit(p):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return np.log(p / (1 - p))


def fit_logreg(X, y, iters=50):
    """Tiny Newton-Raphson logistic regression (no sklearn dependency)."""
    X = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (y - p)
        H = (X * (p * (1 - p))[:, None]).T @ X
        w += np.linalg.solve(H + 1e-6 * np.eye(len(w)), g)
    return w


def clustered(mk, tr, kl):
    """Full-sample, market-clustered t-stats (fills inside one market share the outcome)."""
    print("\n### market-clustered significance (full sample, fills capped at 100 shares)")
    for lag in (1, 2, 3, 4, 6):
        t = enrich_trades(mk, tr, kl, lag_s=lag)
        w = t[(t.rel >= 5) & (t.rel < 297)]
        for thr in (0.03, 0.1):
            s = w[w.model_edge_ps > thr]
            s = s.assign(capped=s.taker_pnl_ps * s["size"].clip(upper=100), cn=s.price * s["size"].clip(upper=100))
            pm = s.groupby("condition_id").agg(pnl=("capped", "sum"), n=("cn", "sum"))
            tstat = pm.pnl.mean() / pm.pnl.std() * np.sqrt(len(pm))
            print(f"  lag={lag}s edge>{thr}: markets={len(pm)} roi={pm.pnl.sum() / pm.n.sum():+.4f} t={tstat:+.2f}")
    t = enrich_trades(mk, tr, kl, lag_s=4)
    t["pnl"] = t.taker_pnl_ps * t["size"]
    t["half_day"] = pd.to_datetime(t.window_start, unit="s").dt.floor("12h")
    s = t[(t.rel >= 5) & (t.rel < 297) & (t.model_edge_ps > 0.05)]
    print("\n  lag=4s edge>5c ROI by 12h period (stability):")
    print(s.groupby(["half_day", "asset"]).apply(lambda x: x.pnl.sum() / x.notional.sum()).unstack().round(3).to_string())


if __name__ == "__main__":
    mk, tr, kl = load(sys.argv[1])
    clustered(mk, tr, kl)
    for lag in (4, 6, 10):
        t = enrich_trades(mk, tr, kl, lag_s=lag)
        w = t[(t.rel >= 5) & (t.rel < 297) & t.model_p_up.notna()].copy()
        y = (w.winner == "Up").astype(float).values
        X = np.column_stack([logit(w.p_up_trade.values), logit(w.model_p_up.values)])
        cut = w.window_start.quantile(0.5)
        tr_, te = (w.window_start <= cut).values, (w.window_start > cut).values
        coef = fit_logreg(X[tr_], y[tr_])
        print(f"\n### lag={lag}s  logistic y ~ a + b*logit(market) + c*logit(model)  (train half): "
              f"a={coef[0]:.3f} b={coef[1]:.3f} c={coef[2]:.3f}")
        for name, p in [("market", w.p_up_trade.values), ("model", w.model_p_up.values),
                        ("blend", 1 / (1 + np.exp(-(coef[0] + X @ coef[1:]))))]:
            pc = np.clip(p[te], 1e-4, 1 - 1e-4)
            ll = -(y[te] * np.log(pc) + (1 - y[te]) * np.log(1 - pc)).mean()
            print(f"  test {name:6s} logloss={ll:.4f}")
        # taker strategy on observed fills: trade when model edge (after fee) > thr, test half only
        wt = w[te]
        print("  taker-follow-model on observed fills (test half):")
        for thr in (0.0, 0.02, 0.05, 0.1):
            s = wt[wt.model_edge_ps > thr]
            pnl = (s.taker_pnl_ps * s["size"]).sum()
            print(f"    edge>{thr:4.2f}: fills={len(s):7d} notional=${s.notional.sum():11,.0f} "
                  f"pnl/share={np.average(s.taker_pnl_ps, weights=s['size']):+.4f} pnl=${pnl:10,.0f} "
                  f"roi={pnl / s.notional.sum():+.4f}")
        # per asset / time for edge > 5c
        s = wt[wt.model_edge_ps > 0.05].copy()
        s["tb"] = pd.cut(s.rel, [0, 60, 120, 180, 240, 270, 290, 300])
        print(s.groupby(["asset", "tb"], observed=True).apply(lambda x: pd.Series({
            "fills": len(x), "roi": (x.taker_pnl_ps * x["size"]).sum() / x.notional.sum(),
            "pnl_$": (x.taker_pnl_ps * x["size"]).sum()})).round(4).to_string())
