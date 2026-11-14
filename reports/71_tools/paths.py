"""State paths of the live 5m rule on recorder extractions: every 100 ms step of the closing minute where the rule
either signals or one of its Binance guards (binance_against, binance_jump, shock) blocks, with the book 300 ms later.
Unlike scripts/replay_lock.py it does not stop at the first entry, so rule variants (reports/71) are simulated on the
stored rows by variants.py.

usage: python reports/71_tools/paths.py DATA_DIR OUT_DIR PERIOD [PERIOD ...]   (config/lock_live.toml, late tier and cool-down off)
"""
import bisect, dataclasses, json, os, sys
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.bot.lock import bn_shock_bp, decide, load_lock_config  # noqa: E402
from pmbot.config import BINANCE_SYMBOL, CHAINLINK_SYMBOL  # noqa: E402
from pmbot.lockmodel import ClSigma, Tables, strike_from_prints  # noqa: E402
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
GUARDS = ("binance_against", "binance_jump", "shock")
def jl(path):
    with open(path) as f:
        for line in f:
            try: yield json.loads(line)
            except ValueError: pass
def one(args):
    data, per, asset = args
    c = load_lock_config(os.path.join(REPO, "config", "lock_live.toml")).lock
    c = dataclasses.replace(c, late_tier_rel=0.0)          # the base rule; variants.py applies the late tier and the calm size
    tables = Tables(os.path.join(REPO, c.tables))
    pre = os.path.join(data, per)
    cl = pd.DataFrame(jl(pre + "_chainlink.jsonl")); cl = cl[cl.sym == CHAINLINK_SYMBOL[asset]].sort_values("rx")
    bn = pd.DataFrame(jl(pre + "_binance.jsonl")); bn = bn[bn.sym == BINANCE_SYMBOL[asset]].sort_values("rx")
    b_rx, b_mid = bn.rx.values.tolist(), ((bn.bid + bn.ask) / 2).values.tolist()
    def bn_at(t):
        i = bisect.bisect_right(b_rx, t) - 1
        return b_mid[i] if i >= 0 else None
    prints = list(zip(cl.rx.values.tolist(), (cl.ts.values // 1000).tolist(), cl.ts.values.tolist(), cl.v.values.tolist()))
    tob = pd.read_csv(pre + "_tob.csv"); tob = tob[tob.k.str.startswith(asset + "-")]
    books = {k: (g.rx.values, g[["ask", "ask_sz", "ask2", "ask2_sz"]].values) for k, g in tob.groupby("k")}
    def ask_at(k, t):
        if k not in books: return None
        rx, v = books[k]; i = np.searchsorted(rx, t, side="right") - 1
        return None if i < 0 or not np.isfinite(v[i][0]) else v[i]
    cl_by_sec, sig, pi, last, rows = {}, ClSigma(), 0, None, []
    for T in sorted({int(k.split("-")[1]) for k in books}):
        t = (T + c.rel_lo) * 1000
        while t <= (T + c.rel_hi) * 1000:
            while pi < len(prints) and prints[pi][0] <= t:
                rx, sec, ts, v = prints[pi]
                cl_by_sec.setdefault(sec, v)
                if last is None or ts >= last[1]: last = (v, ts, rx)
                if sig.last_sec is None: sig.on_print(sec, v)
                else:
                    for s in range(sig.last_sec + 1, sec + 1): sig.on_print(s, cl_by_sec.get(s, sig.last_v))
                pi += 1
            K = strike_from_prints(cl_by_sec, T)
            au, ad = ask_at(f"{asset}-{T}-U", t), ask_at(f"{asset}-{T}-D", t)
            s_, ctx = decide(c, tables, asset, T, K, t, cl_by_sec, last, sig.sigma() if sig.n >= c.min_sigma_prints else None,
                             bn_at(t), bn_at(last[1] + 999) if last else None, bn_at(t - 1000), bn_at(t - 3000),
                             (au[0], au[1]) if au is not None else None, (ad[0], ad[1]) if ad is not None else None,
                             shock=lambda: bn_shock_bp(bn_at, t, c.shock_s))
            if s_ or ctx.get("why") in GUARDS:
                r = {"period": per, "asset": asset, "T": T, "t": t, "sig": bool(s_), **{k: v for k, v in ctx.items()}}
                if s_:
                    v = ask_at(f"{asset}-{T}-{s_['side']}", t + 300)
                    r.update(limit=s_["limit"], ask=s_["ask"], x1=v[0] if v is not None else np.nan, s1=v[1] if v is not None else 0.0,
                             x2=v[2] if v is not None else np.nan, s2=v[3] if v is not None else 0.0)
                rows.append(r)
            t += 100
    return pd.DataFrame(rows)
if __name__ == "__main__":
    data, out, pers = sys.argv[1], sys.argv[2], sys.argv[3:]
    os.makedirs(out, exist_ok=True)
    jobs = [(data, p, a) for p in pers for a in ("btc", "eth", "sol", "xrp")]
    with ProcessPoolExecutor(max_workers=int(os.environ.get("JOBS", "6"))) as ex:
        res = list(ex.map(one, jobs))
    for p in pers:
        d = pd.concat([r for (_, pp, _), r in zip(jobs, res) if pp == p and len(r)], ignore_index=True)
        d.to_parquet(os.path.join(out, p + ".parquet")); print(p, len(d), int(d.sig.sum()), flush=True)
