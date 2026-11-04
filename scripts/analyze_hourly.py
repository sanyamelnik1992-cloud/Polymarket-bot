"""Hourly crypto Up/Down (resolution: Binance 1H candle close >= open) vs a Binance-spot model.

usage: python scripts/analyze_hourly.py HOURLY_DIR
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.fees import fee_per_share  # noqa: E402

pd.set_option("display.width", 220)


def prob_up_spot(spot, open_px, sigma, tau, df=4, vol_mult=1.3):
    sd = np.maximum(sigma * vol_mult * np.sqrt(np.maximum(tau, 1)), 1e-9)
    z = np.log(spot / open_px) / sd
    return stats.t.cdf(z * np.sqrt(df / (df - 2)) if df else z, df) if df else stats.norm.cdf(z)


def enrich(mk, tr, kl, lag_s):
    t = tr.merge(mk[["condition_id", "asset", "window_start", "winner", "token_up"]], on="condition_id")
    t["rel"] = t.timestamp - t.window_start
    t["is_up"] = t.token_id == t.token_up
    t["won"] = np.where(t.is_up, t.winner == "Up", t.winner == "Down").astype(float)
    sign = np.where(t.side == "BUY", 1.0, -1.0)
    t["fee_ps"] = fee_per_share(t.price)
    t["pnl_ps"] = sign * (t.won - t.price) - t.fee_ps
    t["notional"] = t.price * t["size"]
    parts = []
    for a, k in kl.items():
        s = t[t.asset == a].copy()
        c, o = k["c"], k["o"]
        lr = np.log(c).diff()
        sig = np.sqrt((lr ** 2).ewm(halflife=900, min_periods=60).mean())
        obs = s.timestamp.values - lag_s
        s["spot"] = c.reindex(obs).values
        s["sigma"] = sig.reindex(obs).values
        s["open_px"] = o.reindex(s.window_start.values).values
        s["tau"] = s.window_start.values + 3600 - obs
        s["p_up"] = prob_up_spot(s.spot.values, s.open_px.values, s.sigma.values, s.tau.values)
        parts.append(s)
    t = pd.concat(parts)
    t["p_tok"] = np.where(t.is_up, t.p_up, 1 - t.p_up)
    t["edge"] = sign * (t.p_tok - t.price) - t.fee_ps
    return t


if __name__ == "__main__":
    d = sys.argv[1]
    mk = pd.read_parquet(f"{d}/markets.parquet")
    tr = pd.read_parquet(f"{d}/trades.parquet")
    kl = {a: pd.read_parquet(f"{d}/klines_{a}.parquet") for a in mk.asset.unique()}
    # 1. resolution reconstruction from Binance 1s klines
    rec = []
    for _, m in mk.iterrows():
        k = kl[m.asset]
        o, c = k.o.get(m.window_start), k.c.get(m.window_start + 3599)
        rec.append({"asset": m.asset, "agree": (c >= o) == (m.winner == "Up") if o and c else np.nan,
                    "o_err_bp": (o / m.price_to_beat - 1) * 1e4 if o and m.price_to_beat else np.nan})
    rec = pd.DataFrame(rec)
    print("== resolution from Binance 1s klines:", rec.groupby("asset").agg(agree=("agree", "mean"),
                                                                          open_err_bp=("o_err_bp", lambda x: x.abs().median())).round(4).to_dict())
    hours = (mk.window_start.max() - mk.window_start.min() + 3600) / 3600
    t0 = enrich(mk, tr, kl, lag_s=4)
    g = t0.groupby("asset")
    print(pd.DataFrame({"markets": mk.groupby("asset").size(), "taker_notional_day": g.notional.sum() / hours * 24,
                        "taker_pnl_day": (g.apply(lambda x: (x.pnl_ps * x["size"]).sum())) / hours * 24,
                        "fees_day": g.apply(lambda x: (x.fee_ps * x["size"]).sum()) / hours * 24}).round(0))
    # 2. calibration of the model on trades, by time to close
    w = t0[(t0.rel >= 0) & (t0.rel < 3595)].dropna(subset=["p_up"])
    y = (w.winner == "Up").astype(float)
    pm = np.clip(np.where(w.is_up, w.price, 1 - w.price), 1e-3, 1 - 1e-3)
    pmod = np.clip(w.p_up, 1e-3, 1 - 1e-3)
    ll = lambda p: float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())  # noqa: E731
    print(f"\n== log loss on fills: market {ll(pm):.4f}  model {ll(pmod):.4f}")
    # 3. taker edge by information lag (market-clustered)
    print("\n== taker fills the model favours (edge after fee > thr): realised ROI, clustered t")
    for lag in (2, 4, 8, 15, 30):
        t = enrich(mk, tr, kl, lag_s=lag)
        t = t[(t.rel >= 0) & (t.rel < 3595)]
        for thr in (0.03, 0.1):
            s = t[t.edge > thr]
            pmk = s.groupby("condition_id").apply(lambda z: (z.pnl_ps * z["size"].clip(upper=100)).sum())
            tt = pmk.mean() / pmk.std() * np.sqrt(len(pmk)) if len(pmk) > 2 else np.nan
            print(f"  lag={lag:2d}s edge>{thr}: fills={len(s):6d} markets={len(pmk):3d} "
                  f"roi={(s.pnl_ps * s['size']).sum() / max(s.notional.sum(), 1):+.4f} t={tt:+.2f}")
    # 4. by time-to-close bucket (lag 4s, edge > 3c)
    t = enrich(mk, tr, kl, lag_s=4)
    s = t[(t.rel >= 0) & (t.rel < 3595) & (t.edge > 0.03)].copy()
    s["ttc"] = pd.cut(3600 - s.rel, [0, 60, 300, 900, 1800, 3600])
    print("\n== edge>3c, lag 4s, by time to close (s):")
    print(s.groupby(["asset", "ttc"], observed=True).apply(lambda z: pd.Series({
        "fills": len(z), "markets": z.condition_id.nunique(),
        "roi": (z.pnl_ps * z["size"]).sum() / z.notional.sum(), "pnl": (z.pnl_ps * z["size"]).sum()})).round(3).to_string())
