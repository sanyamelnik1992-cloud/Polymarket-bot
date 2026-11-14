"""Rule variants on the state paths of paths.py (reports/71): the weak tier only late in the window, a cool-down after a
Binance guard, and the split of the entries as live now by tier / entry second / time since the last guard block.
Outcomes: Gamma (the exchange's own) for every window with a signal, cached in OUT_DIR/gamma.json.

usage: python reports/71_tools/variants.py DATA_DIR OUT_DIR
"""
import glob, json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.fees import fee_per_share  # noqa: E402
ORDER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111", "d1111b"]
GUARDS = ("binance_against", "binance_jump", "shock")
def gamma(key):
    a, T = key
    url = f"https://gamma-api.polymarket.com/events?slug={a}-updown-5m-{T}"
    for _ in range(4):
        try:
            e = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}), timeout=15))[0]
            m = e["markets"][0]; pr = [float(x) for x in json.loads(m["outcomePrices"])]; oc = json.loads(m["outcomes"])
            return oc[pr.index(max(pr))] if m.get("closed") and max(pr) > 0.99 else None
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return None
def load(data, out):
    g = pd.concat([pd.read_parquet(os.path.join(out, p + ".parquet")) for p in ORDER if os.path.exists(os.path.join(out, p + ".parquet"))], ignore_index=True)
    o = g.period.map({p: i for i, p in enumerate(ORDER)})
    g = g[o == o.groupby([g.asset, g["T"]]).transform("min")].sort_values(["asset", "T", "t"]).reset_index(drop=True)
    own = {}
    for f in glob.glob(os.path.join(data, "*_markets.parquet")):
        m = pd.read_parquet(f)
        if "window_start" not in m: m = m.assign(asset=m.slug.str.split("-").str[0], window_start=m.slug.str.split("-").str[-1].astype(int))
        for r in m.dropna(subset=["winner"]).itertuples(): own.setdefault((r.asset, int(r.window_start)), r.winner)
    cache = os.path.join(out, "gamma.json")
    gm = {tuple(k.split("|")): v for k, v in json.load(open(cache)).items()} if os.path.exists(cache) else {}
    gm = {(a, int(T)): v for (a, T), v in gm.items()}
    need = sorted({(r.asset, int(r.T)) for r in g[g.sig].itertuples()} - set(k for k, v in gm.items() if v))
    if need:
        with ThreadPoolExecutor(8) as ex:
            for k, w in zip(need, ex.map(gamma, need)): gm[k] = w
        json.dump({f"{a}|{T}": v for (a, T), v in gm.items()}, open(cache, "w"))
    win = {}
    for k in {(r.asset, int(r.T)) for r in g[["asset", "T"]].drop_duplicates().itertuples()}:
        win[k] = gm.get(k) or own.get(k)
    diff = sum(1 for k, v in gm.items() if v and own.get(k) and own[k] != v)
    print(f"rows {len(g)}, windows with a signal {len(need) if need else sum(1 for v in gm.values())}, gamma differs from our rule in {diff}")
    return g, win
def sim(g, win, late_rel=0.0, late_p=0.97, cool=0.0, small=2.0, big=10.0, big_p=0.97, start=0.0, cool_small=False):
    rows = []
    for (a, T), w in g.groupby(["asset", "T"], sort=False):
        wn = win.get((a, int(T)))
        if wn is None: continue
        guard_t, nt = -1e18, 0
        for r in w.itertuples():
            if r.why in GUARDS: guard_t = r.t; continue
            if not r.sig or r.t < nt: continue
            rel = r.t / 1000 - T
            if rel < start: continue
            if late_rel and r.p < late_p - 1e-12 and rel < late_rel: continue
            cooling = cool and r.t - guard_t < cool * 1000
            if cooling and not cool_small: continue
            want = big if (r.p >= big_p - 1e-12 and not cooling) else small
            got = cost = fee = 0.0
            for px, sz in ((r.x1, r.s1), (r.x2, r.s2)):
                if np.isfinite(px) and px <= r.limit + 1e-9 and got < want:
                    q = min(want - got, sz); got += q; cost += q * px; fee += q * fee_per_share(px)
            if got >= 1:
                won = (r.side == "U") == (wn == "Up")
                rows.append(dict(asset=a, T=T, t=r.t, rel=rel, p=r.p, px=cost / got, sh=got, won=won, since_guard=(r.t - guard_t) / 1000,
                                 adv3=(-1 if r.side == "U" else 1) * r.mv3, pnl=(got if won else 0.0) - cost - fee,
                                 u=((1 if won else 0) - cost / got - fee / got), day=pd.Timestamp(T + 300, unit="s").strftime("%m-%d")))
                break
            nt = r.t + 1000
    return pd.DataFrame(rows)
def line(name, e):
    if not len(e): return f"{name:44s} n=  0"
    L = int((~e.won).sum()); by = e.groupby("day").pnl.sum().round(1).to_dict()
    return (f"{name:44s} n={len(e):3d} lost={L:2d} ({100*L/len(e):4.1f}%) px={e.px.mean():.3f} per-share={100*e.u.mean():+5.2f}c "
            f"pnl={e.pnl.sum():+7.2f} worst={e.pnl.min():+6.2f} days={by}")
if __name__ == "__main__":
    g, win = load(sys.argv[1], sys.argv[2])
    base = sim(g, win)
    print("== A. the rule as live now (2 shares below p 0.97, 10 from 0.97; no insurance, no day stop)"); print(line("as live now", base))
    print("== B. its entries by tier and entry second")
    for nm, m in (("p 0.95-0.97", base.p < 0.97), ("p 0.97-0.99", (base.p >= 0.97) & (base.p < 0.99)), ("p >= 0.99", base.p >= 0.99)):
        for lo, hi in ((240, 250), (250, 260), (260, 270), (270, 300)):
            print(line(f"  {nm}, second [{lo},{hi})", base[m & (base.rel >= lo) & (base.rel < hi)]))
    print("== C. its entries by time since the last guard block in the window")
    for nm, m in (("guard block <= 2 s before", base.since_guard <= 2), ("guard block 2-5 s before", (base.since_guard > 2) & (base.since_guard <= 5)),
                  ("guard block 5-15 s before", (base.since_guard > 5) & (base.since_guard <= 15)), ("no block / older", base.since_guard > 15)):
        print(line("  " + nm, base[m])); print(line("     of them p >= 0.97", base[m & (base.p >= 0.97)]))
    print("== D. weak tier (p < 0.97) only from second X")
    for x in (250, 255, 260, 265, 270): print(line(f"  from {x}", sim(g, win, late_rel=x)))
    print(line("  no weak tier at all", sim(g, win, late_rel=300)))
    print("== E. cool-down after a guard block (all tiers)")
    for x in (1, 2, 3, 5, 8): print(line(f"  cool {x} s", sim(g, win, cool=x)))
    print("== F. both")
    for x in (2, 3, 5): print(line(f"  weak from 260 + cool {x} s", sim(g, win, late_rel=260, cool=x)))
    for x in (2, 3, 5): print(line(f"  weak from 260 + cool {x} s = small size", sim(g, win, late_rel=260, cool=x, cool_small=True)))
    print("== G. other ideas"); print(line("  everything only from second 250", sim(g, win, start=250)))
    print(line("  weak from 260, all 2 shares", sim(g, win, late_rel=260, big=2.0)))
    print(line("  as now, all 2 shares", sim(g, win, big=2.0)))
