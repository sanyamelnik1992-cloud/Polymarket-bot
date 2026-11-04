"""Historical analysis of 5m Up/Down markets.

usage: python scripts/analyze_history.py HIST_DIR [OUT_DIR]
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.features import binance_resolution, enrich_trades, load  # noqa: E402

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)
pd.set_option("display.max_rows", 200)

TBINS = [-1e9, -600, -60, 0, 30, 60, 120, 180, 240, 270, 285, 295, 300, 1e9]
TLAB = ["<-10m", "-10m..-1m", "-1m..0", "0-30s", "30-60s", "1-2m", "2-3m", "3-4m", "4m-4m30", "4m30-4m45",
        "4m45-4m55", "4m55-5m", "after"]
PBINS = [0, 0.05, 0.15, 0.3, 0.45, 0.55, 0.7, 0.85, 0.95, 1.0001]


def section(title):
    print("\n" + "=" * 100 + f"\n{title}\n" + "=" * 100)


def main(hist, out):
    mk, tr, kl = load(hist)
    os.makedirs(out, exist_ok=True)
    span_h = (mk.window_start.max() - mk.window_start.min() + 300) / 3600
    section(f"DATA: {len(mk)} markets, {len(tr):,} taker fills, {span_h:.1f}h, assets={sorted(mk.asset.unique())}")

    # ---------------------------------------------------------------- resolution
    section("1. Outcome base rates & Binance reconstruction of the Chainlink TWAP outcome")
    r = binance_resolution(mk, kl)
    g = r.groupby("asset")
    print(pd.DataFrame({
        "markets": g.size(),
        "up_rate": g.apply(lambda x: (x.winner == "Up").mean()),
        "bn_agree": g.apply(lambda x: (x.winner == x.bn_winner).mean()),
        "med_|move|_bp": g.oracle_move.apply(lambda x: x.abs().median()),
        "vol_usd_med": g.volume.median(),
        "vol_usd_sum": g.volume.sum(),
    }).round(4))
    # autocorrelation of consecutive outcomes
    r = r.sort_values(["asset", "window_start"])
    r["up"] = (r.winner == "Up").astype(int)
    r["prev_up"] = r.groupby("asset").up.shift()
    print("\nP(Up | prev Up) vs P(Up | prev Down):")
    print(r.dropna(subset=["prev_up"]).groupby(["asset", "prev_up"]).up.agg(["mean", "size"]).unstack().round(4))
    # cross-asset same-window agreement
    piv = r.pivot_table(index="window_start", columns="asset", values="up")
    print("\nSame-window outcome correlation across assets:\n", piv.corr().round(3))
    r.to_parquet(f"{out}/resolution.parquet")

    # ---------------------------------------------------------------- trades
    section("2. Where the volume is and who makes money (taker side, after fees)")
    t = enrich_trades(mk, tr, kl, lag_s=4)
    t["tb"] = pd.cut(t.rel, TBINS, labels=TLAB)
    t["pb"] = pd.cut(t.price, PBINS)
    agg = t.groupby("tb", observed=True).apply(lambda x: pd.Series({
        "fills": len(x),
        "notional_$": x.notional.sum(),
        "share_vol": x.notional.sum() / t.notional.sum(),
        "taker_pnl_$": (x.taker_pnl_ps * x["size"]).sum(),
        "taker_roi": (x.taker_pnl_ps * x["size"]).sum() / x.notional.sum(),
        "fees_$": (x.fee_ps * x["size"]).sum(),
        "maker_pnl_$(no rebate)": (x.maker_pnl_ps * x["size"]).sum(),
    }))
    print(agg.round(4))
    tot_fee = (t.fee_ps * t["size"]).sum()
    print(f"\nTotal: notional ${t.notional.sum():,.0f}  taker pnl ${(t.taker_pnl_ps*t['size']).sum():,.0f}  "
          f"fees ${tot_fee:,.0f}  maker pnl ${(t.maker_pnl_ps*t['size']).sum():,.0f}  "
          f"maker rebate pool (20% of fees) ${0.2*tot_fee:,.0f}")

    section("3. Calibration: realised win-rate of the bought token vs its price (BUY fills only, in-window)")
    b = t[(t.side == "BUY") & (t.rel >= 0) & (t.rel < 300)]
    cal = b.groupby("pb", observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "avg_price": np.average(x.price, weights=x["size"]),
        "win_rate": np.average(x.won, weights=x["size"]),
        "edge_before_fee": np.average(x.won - x.price, weights=x["size"]),
        "edge_after_fee": np.average(x.taker_pnl_ps, weights=x["size"]),
    }))
    print(cal.round(4))
    print("\nBy time bucket x price bucket: (win_rate - price), size-weighted, BUY fills")
    cal2 = b.groupby(["tb", "pb"], observed=True).apply(
        lambda x: np.average(x.won - x.price, weights=x["size"]) if len(x) > 200 else np.nan).unstack()
    print(cal2.round(3))

    section("4. Model (Binance spot + TWAP math) vs market price: who predicts outcomes better?")
    w = t[(t.rel >= 5) & (t.rel < 299) & t.model_p_up.notna()].copy()
    eps = 1e-4
    for name, p in [("market", w.p_up_trade), ("model", w.model_p_up)]:
        pc = p.clip(eps, 1 - eps)
        y = (w.winner == "Up").astype(float)
        ll = -(y * np.log(pc) + (1 - y) * np.log(1 - pc)).mean()
        br = ((pc - y) ** 2).mean()
        print(f"{name:7s} logloss={ll:.4f} brier={br:.4f}")
    w["diff"] = w.model_p_up - w.p_up_trade
    print("\nModel-minus-market (Up prob) distribution by time bucket:")
    print(w.groupby("tb", observed=True)["diff"].describe(percentiles=[.05, .5, .95]).round(3))

    section("5. Taker fills where the model disagreed with the price: realised PnL after fee")
    w["edge_b"] = pd.cut(w.model_edge_ps, [-1, -0.1, -0.05, -0.02, 0, 0.02, 0.05, 0.1, 0.2, 1])
    e = w.groupby("edge_b", observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "notional_$": x.notional.sum(),
        "pred_edge": np.average(x.model_edge_ps, weights=x["size"]),
        "real_pnl_ps": np.average(x.taker_pnl_ps, weights=x["size"]),
        "real_pnl_$": (x.taker_pnl_ps * x["size"]).sum(),
    }))
    print(e.round(4))
    print("\nSame, split by time bucket (realised taker pnl/share when model edge > 3c):")
    good = w[w.model_edge_ps > 0.03]
    print(good.groupby(["asset", "tb"], observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "pnl_ps": np.average(x.taker_pnl_ps, weights=x["size"]),
        "pnl_$": (x.taker_pnl_ps * x["size"]).sum(), "notional_$": x.notional.sum()})).round(4))

    t.drop(columns=["tb", "pb", "edge_b"], errors="ignore").to_parquet(f"{out}/trades_enriched.parquet")


if __name__ == "__main__":
    hist = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(hist, "analysis")
    main(hist, out)
