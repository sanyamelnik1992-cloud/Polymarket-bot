"""Replay recorded live data through the real bot for one or more config variants (in parallel processes).

usage: python scripts/replay_bot.py REC_DIR OUT_DIR config/paper_v2.toml [key=value ...] [--grid]
  key=value overrides, e.g.  strategy.half_spread=0.03 filters.max_disagree=0.15
  --grid runs the built-in variant grid against the given base config
"""
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.config import load_config  # noqa: E402
from pmbot.bot.replay import run_replay  # noqa: E402
from pmbot.config import BINANCE_REST, BINANCE_SYMBOL  # noqa: E402
from pmbot.gamma import fetch_event, parse_event  # noqa: E402
from pmbot.http import get_json  # noqa: E402

GRID = {
    "base": {},
    "vol1.3": {"strategy": {"vol_mult": 1.3}},
    "vol1.6": {"strategy": {"vol_mult": 1.6}},
    "blend0.5": {"strategy": {"model_weight": 0.5}},
    "blend0.5_h3": {"strategy": {"model_weight": 0.5, "half_spread": 0.03}},
    "h3": {"strategy": {"half_spread": 0.03}},
    "h5": {"strategy": {"half_spread": 0.05}},
    "disagree15": {"filters": {"max_disagree": 0.15}},
    "stop180": {"strategy": {"stop_s": 180}},
    "vol1.3_disagree15": {"strategy": {"vol_mult": 1.3}, "filters": {"max_disagree": 0.15}},
    "no_toxic": {"filters": {"toxic_shares": 0, "toxic_fills": 0}},
    "lat300": {"execution": {"place_latency_ms": 300, "cancel_latency_ms": 300}},
}


def parse_kv(args):
    over = {}
    for a in args:
        k, v = a.split("=", 1)
        sec, key = k.split(".", 1)
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            pass
        over.setdefault(sec, {})[key] = v
    return over


def merge(a, b):
    out = json.loads(json.dumps(a))
    for k, v in b.items():
        out.setdefault(k, {}).update(v)
    return out


def outcomes(rec_dir):
    winners, strikes = {}, {}
    with open(f"{rec_dir}/markets.jsonl") as f:
        slugs = {json.loads(line)["slug"] for line in f}
    for s in slugs:
        e = fetch_event(s)
        if not e:
            continue
        m = parse_event(e)
        if m["winner"]:
            winners[s] = m["winner"]
        if m["price_to_beat"]:
            strikes[s] = float(m["price_to_beat"])
    return winners, strikes


def vol_seed(rec_dir, assets):
    with open(f"{rec_dir}/binance.jsonl") as f:
        first = json.loads(f.readline())["rx"]
    out = {}
    for a in assets:
        k = get_json(f"{BINANCE_REST}/klines", {"symbol": BINANCE_SYMBOL[a], "interval": "1s",
                                                "startTime": first - 1800_000, "limit": 1000})
        k2 = get_json(f"{BINANCE_REST}/klines", {"symbol": BINANCE_SYMBOL[a], "interval": "1s",
                                                 "startTime": k[-1][0] + 1000, "limit": 1000}) if k else []
        out[a] = [float(r[4]) for r in k + k2 if r[0] < first]
    return out


def one(args):
    name, rec_dir, out_dir, cfg_path, over, winners, strikes, seed = args
    cfg = load_config(cfg_path, over)
    eng = run_replay(rec_dir, cfg, os.path.join(out_dir, name), winners, strikes, seed)
    pnl_by_window = []
    with open(os.path.join(out_dir, name, "events.jsonl")) as f:
        for line in f:
            e = json.loads(line)
            if e["type"] == "settle":
                pnl_by_window.append((e["key"], e["pnl"], e["cost"]))
    df = pd.DataFrame(pnl_by_window, columns=["key", "pnl", "cost"])
    t = df.pnl.mean() / df.pnl.std() * np.sqrt(len(df)) if len(df) > 2 and df.pnl.std() > 0 else np.nan
    eq = df.pnl.cumsum()
    return {"variant": name, "windows": len(df), "pnl": round(df.pnl.sum(), 2), "cost": round(df.cost.sum(), 2),
            "roi": round(df.pnl.sum() / df.cost.sum(), 4) if len(df) else np.nan, "t": round(t, 2),
            "worst": round(df.pnl.min(), 2) if len(df) else np.nan,
            "max_dd": round((eq - eq.cummax()).min(), 2) if len(df) else np.nan,
            "fills": eng.stats["fills"], "killed": eng.risk.killed,
            **{f"pnl_{a}": round(df[df.key.str.startswith(a)].pnl.sum(), 2) for a in ("btc", "eth", "sol")}}


if __name__ == "__main__":
    rec, out, cfg_path = sys.argv[1], sys.argv[2], sys.argv[3]
    rest = [a for a in sys.argv[4:] if a != "--grid"]
    os.makedirs(out, exist_ok=True)
    base_over = parse_kv(rest)
    cfg0 = load_config(cfg_path, base_over)
    winners, strikes = outcomes(rec)
    seed = vol_seed(rec, cfg0.strategy.assets)
    variants = GRID if "--grid" in sys.argv else {"run": {}}
    jobs = [(n, rec, out, cfg_path, merge(base_over, v), winners, strikes, seed) for n, v in variants.items()]
    with ProcessPoolExecutor(min(4, len(jobs))) as ex:
        res = list(ex.map(one, jobs))
    df = pd.DataFrame(res).set_index("variant")
    pd.set_option("display.width", 220)
    print(df.to_string())
    df.to_csv(os.path.join(out, "summary.csv"))
