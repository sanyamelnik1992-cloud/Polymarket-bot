"""5m taker (stale-quote sniping) under server-location profiles.

usage: python scripts/taker_latency_study.py OUT_CSV REC_DIR [REC_DIR ...] [--profiles=a,b] [--edges=0.05,0.10]
"""
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.replay import PROFILES  # noqa: E402
from pmbot.bot.taker_sim import TakerCfg, run  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from replay_bot import outcomes, vol_seed  # noqa: E402


def one(job):
    rec, prof, edge, winners, strikes, seed = job
    rows, stats = run(rec, PROFILES[prof], TakerCfg(min_edge=edge), winners, strikes, seed)
    df = pd.DataFrame(rows)
    n = len(df)
    return {"rec": os.path.basename(rec.rstrip("/")), "profile": prof, "min_edge": edge, "windows": n,
            "signals": stats["signals"], "missed": stats["misses"],
            "cost": df.cost.sum() if n else 0.0, "pnl": df.pnl.sum() if n else 0.0,
            "t": df.pnl.mean() / df.pnl.std() * np.sqrt(n) if n > 2 and df.pnl.std() > 0 else np.nan}


if __name__ == "__main__":
    out = sys.argv[1]
    profs = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--profiles=")),
                 ["here_slow", "dublin", "london", "tokyo"])
    edges = next((list(map(float, a.split("=", 1)[1].split(","))) for a in sys.argv if a.startswith("--edges=")),
                 [0.05, 0.10])
    jobs = []
    for rec in [a for a in sys.argv[2:] if not a.startswith("--")]:
        w, s = outcomes(rec)
        seed = vol_seed(rec, ["btc", "eth", "sol"])
        jobs += [(rec, p, e, w, s, seed) for p in profs for e in edges]
    with ProcessPoolExecutor(4) as ex:
        res = pd.DataFrame(list(ex.map(one, jobs)))
    res["roi"] = res.pnl / res.cost.replace(0, np.nan)
    pd.set_option("display.width", 200)
    print(res.round(3).to_string(index=False))
    tot = res.groupby(["profile", "min_edge"]).agg(windows=("windows", "sum"), signals=("signals", "sum"),
                                                   missed=("missed", "sum"), cost=("cost", "sum"), pnl=("pnl", "sum"))
    tot["roi"] = tot.pnl / tot.cost
    print("\nTOTAL:")
    print(tot.round(3).to_string())
    res.to_csv(out, index=False)
