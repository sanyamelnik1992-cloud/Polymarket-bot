"""How much does server location matter for the 5m bot? Replay recordings under latency profiles.

usage: python scripts/latency_study.py OUT_DIR REC_DIR [REC_DIR ...] [--strategies v2,v3_mid] [--profiles a,b]
Profiles: see pmbot.bot.replay.PROFILES (here_slow = how we tested so far; dublin = AWS eu-west-1;
london = co-located in eu-west-2; tokyo = next to Binance).
"""
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.config import load_config  # noqa: E402
from pmbot.bot.replay import PROFILES, run_replay  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from replay_bot import outcomes, vol_seed  # noqa: E402

STRATEGIES = {
    "v2": ("config/paper_v2.toml", {}),
    "v3_mid": ("config/paper_v3.toml", {}),
    "model_h2": ("config/paper_v2.toml", {"strategy": {"half_spread": 0.02, "vol_mult": 1.6}}),
    "mid_h2": ("config/paper_v3.toml", {"strategy": {"half_spread": 0.02}}),
    "v3_mid_touch": ("config/paper_v3.toml", {"execution": {"paper_fill_at_touch": True}}),
    "v2_touch": ("config/paper_v2.toml", {"execution": {"paper_fill_at_touch": True}}),
    "mid_h2_touch": ("config/paper_v3.toml", {"strategy": {"half_spread": 0.02}, "execution": {"paper_fill_at_touch": True}}),
    "mid_h1": ("config/paper_v3.toml", {"strategy": {"half_spread": 0.01}}),
    "mid_h1_touch": ("config/paper_v3.toml", {"strategy": {"half_spread": 0.01}, "execution": {"paper_fill_at_touch": True}}),
    # variants around D (colo_strict = the live test's config): reaction threshold, disagreement guard, distance
    "d": ("config/colo_strict.toml", {}),
    "d_fast05": ("config/colo_strict.toml", {"execution": {"fast_move_sigmas": 0.5}}),
    "d_fast2": ("config/colo_strict.toml", {"execution": {"fast_move_sigmas": 2.0}}),
    "d_dis02": ("config/colo_strict.toml", {"filters": {"max_disagree": 0.02}}),
    "d_h2": ("config/colo_strict.toml", {"strategy": {"half_spread": 0.02}}),
    "d_vol18": ("config/colo_strict.toml", {"strategy": {"vol_mult": 1.8}}),       # calibrated model (reports/23)
    "d_start30": ("config/colo_strict.toml", {"strategy": {"start_s": 30}}),       # no quotes in the first 30 s
    "d_skew005": ("config/colo_strict.toml", {"strategy": {"skew_per_share": 0.005}}),  # stronger pairing skew
    "d_skew01": ("config/colo_strict.toml", {"strategy": {"skew_per_share": 0.010}}),
    # 1c without the strict guard: enough fills per recording to compare timing / skew / reaction variants
    "m1_start30": ("config/paper_v3.toml", {"strategy": {"half_spread": 0.01, "start_s": 30}}),
    "m1_skew005": ("config/paper_v3.toml", {"strategy": {"half_spread": 0.01, "skew_per_share": 0.005}}),
    "m1_fast05": ("config/paper_v3.toml", {"strategy": {"half_spread": 0.01}, "execution": {"fast_move_sigmas": 0.5}}),
}
BN_STALE_OFF = "--bn-stale-off" in sys.argv   # old recordings stored only BBO changes -> staleness is an artifact


def one(job):
    strat, prof, rec, out, winners, strikes, seed = job
    path, over = STRATEGIES[strat]
    over = json.loads(json.dumps(over))
    over.setdefault("risk", {})["max_actions_per_sec"] = 100
    if BN_STALE_OFF:
        over.setdefault("filters", {})["bn_max_age_ms"] = 600000
    cfg = load_config(path, over)
    name = f"{os.path.basename(rec.rstrip('/'))}__{strat}__{prof}"
    jd = os.path.join(out, name)
    eng = run_replay(rec, cfg, jd, winners, strikes, seed, profile=PROFILES[prof])
    st = []
    for line in open(os.path.join(jd, "events.jsonl")):
        e = json.loads(line)
        if e["type"] == "settle":
            st.append((e["pnl"], e["cost"], e["rebate"]))
    df = pd.DataFrame(st, columns=["pnl", "cost", "rebate"])
    n = len(df)
    return {"rec": os.path.basename(rec.rstrip("/")), "strategy": strat, "profile": prof, "windows": n,
            "fills": eng.stats["fills"], "pnl": df.pnl.sum() if n else 0.0, "rebate": df.rebate.sum() if n else 0.0,
            "cost": df.cost.sum() if n else 0.0,
            "t": df.pnl.mean() / df.pnl.std() * np.sqrt(n) if n > 2 and df.pnl.std() > 0 else np.nan,
            "worst": df.pnl.min() if n else np.nan}


if __name__ == "__main__":
    out = sys.argv[1]
    recs = [a for a in sys.argv[2:] if not a.startswith("--")]
    strats = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--strategies=")), ["v2", "v3_mid"])
    profs = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--profiles=")),
                 ["here_slow", "dublin", "london", "tokyo"])
    os.makedirs(out, exist_ok=True)
    jobs = []
    for rec in recs:
        winners, strikes = outcomes(rec)
        seed = vol_seed(rec, ["btc", "eth", "sol"])
        jobs += [(s, p, rec, out, winners, strikes, seed) for s in strats for p in profs]
    with ProcessPoolExecutor(4) as ex:
        res = list(ex.map(one, jobs))
    df = pd.DataFrame(res)
    df["roi"] = df.pnl / df.cost.replace(0, np.nan)
    pd.set_option("display.width", 220)
    print(df.round(3).to_string(index=False))
    tot = df.groupby(["strategy", "profile"]).agg(windows=("windows", "sum"), fills=("fills", "sum"),
                                                  pnl=("pnl", "sum"), cost=("cost", "sum"), rebate=("rebate", "sum"))
    tot["roi"] = tot.pnl / tot.cost
    print("\nTOTAL over recordings:")
    print(tot.round(3).to_string())
    df.to_csv(os.path.join(out, "latency_study.csv"), index=False)
