"""Relative value between nested Up/Down markets that share the SAME closing TWAP.

In the last 5 minutes of a 15m window [T, T+900], the 5m market [T+600, T+900] settles on the identical
closing Chainlink TWAP C.  Both are digital options on C:
    P15 = P(C >= O15)      P5 = P(C >= O5)
so with the model's distribution of log C (sd s at time t) and the 5m market price P5 (more liquid):
    z5 = F^-1(P5)   ->   fair P15 = F(z5 + log(O5/O15) / s)
Strict no-arbitrage: if O15 <= O5 then P15 >= P5 (and vice versa).

The test uses observed 15m taker fills in the last 300s and the latest 5m fill-implied P5 before it
(both shifted to match time, staleness <= `max_age` s).

usage: python scripts/nested_rv.py HIST_5M HIST_15M
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.features import enrich_trades, load  # noqa: E402

pd.set_option("display.width", 220)
DF = 4


def main(h5, h15, max_age=2):
    mk5, tr5, kl = load(h5, ("btc", "eth"))
    mk15, tr15, _ = load(h15, ("btc", "eth"))
    mk5 = mk5[mk5.asset.isin(["btc", "eth"])].assign(window=300)
    t5 = enrich_trades(mk5, tr5, kl, lag_s=3)
    t15 = enrich_trades(mk15, tr15, kl, lag_s=3)
    for t in (t5, t15):
        t["tm"] = t.timestamp - 2.2
        t["end"] = t.window_start + t.window
    m5 = mk5.assign(end=mk5.window_start + 300)[["asset", "end", "price_to_beat", "condition_id"]]
    out = []
    for a in ("btc", "eth"):
        x15 = t15[(t15.asset == a) & (t15.rel >= 600) & (t15.rel < 897)].copy()
        x5 = t5[(t5.asset == a)][["tm", "end", "p_up_trade", "sigma"]].sort_values("tm")
        x15 = x15.merge(m5[m5.asset == a][["end", "price_to_beat"]].rename(columns={"price_to_beat": "o5"}), on="end")
        x15 = x15.sort_values("tm")
        j = pd.merge_asof(x15, x5.rename(columns={"tm": "tm5", "p_up_trade": "p5", "sigma": "sig5"}),
                          left_on="tm", right_on="tm5", by="end", direction="backward")
        j = j[(j.tm - j.tm5 <= max_age) & j.p5.between(0.02, 0.98)]
        # sd of log C at time t (inside / before the closing TWAP window), same algebra as pmbot.model
        tau_pre = np.maximum(840 - (j.rel - 2.2), 0)
        rem = np.clip(900 - (j.rel - 2.2), 0, 60)
        sd = np.where(tau_pre > 0, j.sigma * np.sqrt(tau_pre + 20), j.sigma * np.sqrt(np.maximum(rem, 1) / 3) * rem / 60)
        z5 = stats.t.ppf(j.p5, DF)
        fair15 = stats.t.cdf(z5 + np.log(j.o5 / j.price_to_beat) / sd, DF)
        j["fair15_up"] = fair15
        j["fair_tok"] = np.where(j.is_up_tok, fair15, 1 - fair15)
        j["rv_edge"] = np.where(j.side == "BUY", j.fair_tok - j.price, j.price - j.fair_tok) - j.fee_ps
        # strict monotonicity violation (no model): O15<=O5 requires P15>=P5
        p15 = j.p_up_trade
        j["viol"] = np.where(j.price_to_beat <= j.o5, j.p5 - p15, p15 - j.p5)
        out.append(j.assign(asset=a))
    j = pd.concat(out)
    j["pnl"] = j.taker_pnl_ps * j["size"]
    print(f"15m fills in last 5 min with a fresh 5m quote: {len(j):,}")
    print("\nStrict monotonicity violations (P5 vs P15 in the wrong order), in prob points:")
    print(j.viol.describe(percentiles=[.5, .9, .99]).round(4))
    print("fills with violation > 3c:", (j.viol > 0.03).sum(), " > 5c:", (j.viol > 0.05).sum())
    print("\nRealised taker PnL on 15m fills, bucketed by relative-value edge (fair from the 5m price):")
    j["eb"] = pd.cut(j.rv_edge, [-1, -0.1, -0.05, -0.02, 0, 0.02, 0.05, 0.1, 1])
    g = j.groupby(["asset", "eb"], observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "markets": x.condition_id.nunique(), "pred": np.average(x.rv_edge, weights=x["size"]),
        "real_pnl_ps": np.average(x.taker_pnl_ps, weights=x["size"]), "pnl_$": x.pnl.sum(),
        "notional": x.notional.sum()}))
    print(g.round(4).to_string())
    for thr in (0.02, 0.05):
        s = j[j.rv_edge > thr]
        pm = s.groupby("condition_id").apply(lambda z: (z.taker_pnl_ps * z["size"].clip(upper=100)).sum())
        print(f"rv_edge>{thr}: fills={len(s)} markets={len(pm)} roi={s.pnl.sum()/s.notional.sum():+.4f} "
              f"t(clustered)={pm.mean()/pm.std()*np.sqrt(len(pm)):+.2f}")
    # model-free comparison: same filter using our spot model instead of the 5m price
    for thr in (0.02, 0.05):
        s = j[j.model_edge_ps > thr]
        pm = s.groupby("condition_id").apply(lambda z: (z.taker_pnl_ps * z["size"].clip(upper=100)).sum())
        print(f"(spot-model edge>{thr} on same fills: roi={s.pnl.sum()/s.notional.sum():+.4f} t={pm.mean()/pm.std()*np.sqrt(len(pm)):+.2f})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
