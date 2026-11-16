"""Is a signal worth more when it is fresh? State paths of the live 5m rule on recorder extractions (as reports/71_tools/paths.py),
but EVERY 100 ms step of the closing minute where the model has a side at a tier (signal, ask too high, no ask, guard block,
tier_early), so the age of the model's opinion at the moment of an entry is known. Then the rule with "enter only within X s
of the model reaching the entry's tier" (reports/76).

usage: python reports/76_tools/fresh_paths.py build DATA_DIR OUT_DIR PERIOD [PERIOD ...]
       python reports/76_tools/fresh_paths.py sim DATA_DIR OUT_DIR
"""
import bisect, dataclasses, json, os, sys
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.join(HERE, "..", "..")
sys.path.insert(0, REPO); sys.path.insert(0, os.path.join(HERE, "..", "71_tools"))
from pmbot.bot.lock import bn_shock_bp, decide, load_lock_config  # noqa: E402
from pmbot.config import BINANCE_SYMBOL, CHAINLINK_SYMBOL  # noqa: E402
from pmbot.fees import fee_per_share  # noqa: E402
from pmbot.lockmodel import ClSigma, Tables, strike_from_prints  # noqa: E402
GUARDS = ("binance_against", "binance_jump", "shock")
TIERS = (0.95, 0.97, 0.99)
def jl(path):
    with open(path) as f:
        for line in f:
            try: yield json.loads(line)
            except ValueError: pass
def one(args):
    data, per, asset = args
    c = load_lock_config(os.path.join(REPO, "config", "lock_live.toml")).lock
    c = dataclasses.replace(c, late_tier_rel=0.0)          # the base rule; sim applies the late tier
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
            p, side = (s_ or ctx).get("p"), (s_ or ctx).get("side")
            if side and p is not None and p >= c.theta - 1e-12:
                r = {"period": per, "asset": asset, "T": T, "t": t, "sig": bool(s_), "why": ctx.get("why") or ("sig" if s_ else ""),
                     "p": p, "side": side, "p_cl": ctx.get("p_cl"), "ask0": ctx.get("ask")}
                if s_:
                    v = ask_at(f"{asset}-{T}-{s_['side']}", t + 300)
                    r.update(limit=s_["limit"], ask=s_["ask"], x1=v[0] if v is not None else np.nan, s1=v[1] if v is not None else 0.0,
                             x2=v[2] if v is not None else np.nan, s2=v[3] if v is not None else 0.0)
                rows.append(r)
            t += 100
    return pd.DataFrame(rows)
def tier_of(p):
    return max((f for f in TIERS if p >= f - 1e-12), default=None)
def sim(g, win, fresh=None, late_rel=260.0, late_p=0.97, small=2.0, big=10.0, big_p=0.99, only=None, stale_only=False):
    """fresh = X: an entry only within X s of the model reaching the entry's tier on this side (a streak of 100 ms steps with
    p >= that tier; a missing step or the other side ends it). stale_only: the complement."""
    rows = []
    for (a, T), w in g.groupby(["asset", "T"], sort=False):
        wn = win.get((a, int(T)))
        if wn is None: continue
        since, prev_t, prev_side, nt = {}, None, None, 0
        for r in w.itertuples():
            if prev_t is None or r.t - prev_t > 100 or r.side != prev_side: since = {}
            for f in TIERS:
                if r.p >= f - 1e-12: since.setdefault(f, r.t)
                else: since.pop(f, None)
            prev_t, prev_side = r.t, r.side
            if not r.sig or r.t < nt: continue
            rel = r.t / 1000 - T
            if late_rel and r.p < late_p - 1e-12 and rel < late_rel: continue
            f = tier_of(r.p); age = (r.t - since[f]) / 1000
            if only is not None and f != only: continue
            if fresh is not None and ((age > fresh + 1e-9) != stale_only): continue
            want = big if r.p >= big_p - 1e-12 else small
            got = cost = fee = 0.0
            for px, sz in ((r.x1, r.s1), (r.x2, r.s2)):
                if np.isfinite(px) and px <= r.limit + 1e-9 and got < want:
                    q = min(want - got, sz); got += q; cost += q * px; fee += q * fee_per_share(px)
            if got >= 1:
                won = (r.side == "U") == (wn == "Up")
                rows.append(dict(asset=a, T=T, rel=rel, p=r.p, tier=f, age=age, px=cost / got, sh=got, won=won, pnl=(got if won else 0.0) - cost - fee,
                                 u=((1 if won else 0) - cost / got - fee / got), per=r.period, day=pd.Timestamp(T + 300, unit="s").strftime("%m-%d")))
                break
            nt = r.t + 1000
    return pd.DataFrame(rows)
def line(name, e):
    if not len(e): return f"{name:46s} n=  0"
    L = int((~e.won).sum()); by = e.groupby("day").u.sum().round(2).to_dict()
    return (f"{name:46s} n={len(e):3d} lost={L:2d} ({100*L/len(e):4.1f}%) px={e.px.mean():.3f} per-share={100*e.u.mean():+5.2f}c "
            f"sum/share={e.u.sum():+6.2f} pnl=${e.pnl.sum():+7.2f} worst={e.pnl.min():+6.2f} days={by}")
if __name__ == "__main__":
    mode, data, out = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == "build":
        pers = sys.argv[4:]; os.makedirs(out, exist_ok=True)
        jobs = [(data, p, a) for p in pers for a in ("btc", "eth", "sol", "xrp")]
        with ProcessPoolExecutor(max_workers=int(os.environ.get("JOBS", "6"))) as ex:
            res = list(ex.map(one, jobs))
        for p in pers:
            d = pd.concat([r for (_, pp, _), r in zip(jobs, res) if pp == p and len(r)], ignore_index=True)
            d.to_parquet(os.path.join(out, p + ".parquet")); print(p, len(d), int(d.sig.sum()), flush=True)
    else:
        import variants as v
        g, win = v.load(data, out)
        print("== A. the rule as live now (weak tier from second 260; 2 shares below p 0.99, 10 from 0.99; no insurance, no day stop)")
        print(line("as live now", sim(g, win)))
        print("== B. entry only within X s of the model reaching the entry's tier")
        for x in (0.0, 0.1, 0.3, 0.5, 1.0, 2.0, 5.0): print(line(f"  fresh <= {x} s", sim(g, win, fresh=x)))
        print("== C. the complement: entries older than X s")
        for x in (0.0, 0.5, 1.0, 2.0): print(line(f"  older than {x} s", sim(g, win, fresh=x, stale_only=True)))
        print("== D. by tier: fresh (<= 0.3 s) / older")
        base = sim(g, win, late_rel=0.0)
        for f in TIERS:
            e = base[base.tier == f]
            print(line(f"  tier {f} (weak tier from 240 too): fresh", e[e.age <= 0.3])); print(line(f"  tier {f}: older", e[e.age > 0.3]))
        print("== E. halves: fresh <= 0.3 s / older, base rule from second 240")
        for nm, m in (("7-9 Nov", base.day <= "11-09"), ("10-11 Nov", base.day > "11-09")):
            print(line(f"  {nm} fresh", base[m & (base.age <= 0.3)])); print(line(f"  {nm} older", base[m & (base.age > 0.3)]))
        print("== F. age buckets of the base rule's entries (from second 240)")
        for lo, hi in ((0, 0.05), (0.05, 0.3), (0.3, 1), (1, 3), (3, 10), (10, 99)):
            print(line(f"  age [{lo},{hi}) s", base[(base.age >= lo) & (base.age < hi)]))
