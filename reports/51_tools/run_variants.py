"""Replay lock-rule variants (production decide + config/lock_tables.npz) on recorder extractions, in parallel.
usage: REPLAY_DATA=dir python reports/51_tools/run_variants.py PERIOD [PERIOD ...]   (periods: r1107 r1108 r1109 d1109)
Writes DATA/out/<variant>__<period>.csv (one row per entry, 5 shares, FAK on the book 300 ms later)."""
import contextlib
import io
import json
import os
import sys
import tomllib
from concurrent.futures import ProcessPoolExecutor

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DATA = os.path.abspath(os.environ.get("REPLAY_DATA", "data"))   # folder with PERIOD_tob.csv, _chainlink.jsonl, _binance.jsonl, markets
OUT = os.path.join(DATA, "out")
MK = {"r1107": "hist96_markets.parquet", "r1108": "hist96_markets.parquet", "r1109": "r1109_markets.parquet",
      "d1109": "d1109_markets.parquet"}
VARIANTS = {
    "L": ("config/lock.toml", {}),
    "th098": ("config/lock.toml", {"theta": 0.98}),
    "L3": ("config/lock_tier.toml", {}),
    "L4": ("config/lock_tier_wide.toml", {}),
    "L3top": ("config/lock_tier.toml", {"theta": 0.95, "tiers": [[0.99, 0.98], [0.97, 0.95], [0.95, 0.93]]}),
}


def dump(d):
    out = [f"{k} = {json.dumps(v)}" for k, v in d.items() if not isinstance(v, dict)]
    for sec, kv in d.items():
        if isinstance(kv, dict):
            out.append(f"[{sec}]")
            out += [f"{k} = {json.dumps(v)}" for k, v in kv.items()]
    return "\n".join(out) + "\n"


def job(args):
    name, period = args
    os.chdir(REPO)
    sys.path.insert(0, REPO)
    import importlib.util
    spec = importlib.util.spec_from_file_location("rl", "scripts/replay_lock.py")
    rl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rl)
    base, ov = VARIANTS[name]
    d = tomllib.load(open(base, "rb"))
    d["lock"].update(ov)
    cfg = os.path.join(OUT, f"{name}.toml")
    open(cfg, "w").write(dump(d))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = rl.main(os.path.join(DATA, period), os.path.join(DATA, MK[period]), cfg, 5.0)
    r.to_csv(os.path.join(OUT, f"{name}__{period}.csv"), index=False)
    return name, period, len(r)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    periods = sys.argv[1:]
    jobs = [(v, p) for p in periods for v in VARIANTS]
    with ProcessPoolExecutor(max_workers=6) as ex:
        for name, period, n in ex.map(job, jobs):
            print(name, period, n, flush=True)
