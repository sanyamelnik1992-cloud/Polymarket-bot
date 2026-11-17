"""Early 0.99 rule on recorded 5m BOOKS (not trades): Chainlink strike / nowcast like the bot, sigma = rms of 1 s Chainlink
returns over the last hour, z = ln(S/K) / (sigma * sqrt(tau0 + 19.5)), tau0 = s to the first closing print (end - 62).
Entry: first 0.5 s step in the zone with z >= z0 (and the Chainlink-only z >= agree) whose leader ask is in [pmin, cap];
filled at the book 0.3 s later (walk 2 levels, up to SH shares at <= cap), else retried. Outcome: Gamma.
usage: early_books.py windows_all.json TAPE_PREFIX [TAPE_PREFIX ...]   (prefix -> _tob.csv, _chainlink.jsonl, _binance.jsonl)"""
import json, sys, collections, math
import numpy as np, pandas as pd
SH = 10.0
def fee(p): return 0.07 * p * (1 - p)
GW = {w["slug"]: w["win"] for w in json.load(open(sys.argv[1]))}
def load(prefix):
    cl = collections.defaultdict(list); bn = collections.defaultdict(list)
    for line in open(prefix + "_chainlink.jsonl"):
        e = json.loads(line); cl[e["sym"].split("/")[0]].append((e["rx"], e["ts"], e["v"]))
    for line in open(prefix + "_binance.jsonl"):
        e = json.loads(line)
        if e.get("bid") and e.get("ask"): bn[e["sym"][:-4].lower()].append((e["rx"], 0.5 * (e["bid"] + e["ask"])))
    tob = pd.read_csv(prefix + "_tob.csv")
    tob = tob[~tob.k.str.startswith("GAP")]
    return cl, bn, tob
def run_all(prefixes, variants):
    res = {v: [] for v in variants}; sig_info = []
    for prefix in prefixes:
        cl, bn, tob = load(prefix)
        tob["a"] = tob.k.str.split("-").str[0]; tob["W"] = tob.k.str.split("-").str[1].astype(int); tob["sd"] = tob.k.str.split("-").str[2]
        for a in sorted(cl):
            c = sorted(cl[a]); rx = np.array([x[0] for x in c]); ts = np.array([x[1] for x in c]); v = np.array([x[2] for x in c])
            by_sec = {}
            for t_, v_ in zip(ts // 1000, v): by_sec[int(t_)] = v_
            s0, s1 = min(by_sec), max(by_sec)
            arr = np.full(s1 - s0 + 1, np.nan)
            for s_, v_ in by_sec.items(): arr[s_ - s0] = v_
            ok = ~np.isnan(arr); idx = np.where(ok, np.arange(len(arr)), 0); np.maximum.accumulate(idx, out=idx); arr = arr[idx]
            r2 = np.concatenate([[0.0], np.diff(np.log(arr)) ** 2]); cs = np.concatenate([[0.0], np.cumsum(r2)])
            def sig(sec):
                i = int(sec) - s0
                if i - 3600 < 0 or i >= len(arr): return None
                return math.sqrt((cs[i + 1] - cs[i + 1 - 3600]) / 3600)
            b = sorted(bn.get(a, [])); brx = np.array([x[0] for x in b]); bmid = np.array([x[1] for x in b])
            def bn_at(ms):
                j = np.searchsorted(brx, ms, side="right") - 1
                return bmid[j] if j >= 0 else None
            ta = tob[tob.a == a]
            for (W, sd), g in ta.groupby(["W", "sd"]):
                pass
            groups = {k: g.sort_values("rx") for k, g in ta.groupby(["W", "sd"])}
            for W in sorted(set(ta.W)):
                if ("U" not in [k[1] for k in groups if k[0] == W]) or (W, "D") not in groups or (W, "U") not in groups: continue
                slug = f"{a}-updown-5m-{W}"
                if slug not in GW: continue
                xs = []; last = None
                for s_ in range(W - 72, W - 2):
                    last = by_sec.get(s_, last)
                    if s_ >= W - 62: xs.append(last)
                if None in xs or not xs: continue
                K = float(np.mean(xs))
                G = {sd: (groups[(W, sd)].rx.values, groups[(W, sd)].ask.values, groups[(W, sd)].ask_sz.values, groups[(W, sd)].ask2.values,
                          groups[(W, sd)].ask2_sz.values, groups[(W, sd)].bid.values) for sd in ("U", "D")}
                def book(sd, ms):
                    r_, a1, s1_, a2, s2, bd = G[sd]; j = np.searchsorted(r_, ms, side="right") - 1
                    if j < 0: return None
                    return a1[j], s1_[j], a2[j], s2[j], bd[j]
                win = GW[slug]
                steps = []
                for t in np.arange(W + 120, W + 238.01, 0.5):
                    ms = t * 1000; j = np.searchsorted(rx, ms, side="right") - 1
                    if j < 0: continue
                    pts, pv = ts[j], v[j]
                    if ms - rx[j] > 2600: continue          # stale Chainlink (the bot's cl_max_age_ms)
                    bnow, blast = bn_at(ms), bn_at(pts + 999)
                    if not bnow or not blast: continue
                    sg = sig(t)
                    if not sg: continue
                    S_ = pv * bnow / blast; tau0 = (W + 238) - t
                    z = math.log(S_ / K) / (sg * math.sqrt(tau0 + 19.5)); zc = math.log(pv / K) / (sg * math.sqrt(tau0 + 19.5))
                    steps.append((t, z, zc, sg))
                if not steps: continue
                sig_info.append(steps[0][3])
                for vk in variants:
                    z0, lo, hi, pmin, cap, agree = vk
                    for t, z, zc, sg in steps:
                        if not (W + lo <= t <= W + hi): continue
                        if abs(z) < z0 or abs(zc) < agree or z * zc <= 0: continue
                        sd = "U" if z > 0 else "D"
                        bk = book(sd, t * 1000)
                        if bk is None or not (pmin - 1e-9 <= bk[0] <= cap + 1e-9): continue
                        f = book(sd, t * 1000 + 300)
                        if f is None or not (f[0] <= cap + 1e-9): continue
                        got = min(SH, f[1]); cost = got * f[0]
                        if got < SH and f[2] == f[2] and f[2] <= cap + 1e-9:
                            q = min(SH - got, f[3]); got += q; cost += q * f[2]
                        px = cost / got
                        won = (sd == "U") == (win == "Up")
                        # our side's bid after the entry, each second to the end (stop analysis)
                        path = []
                        for tt in np.arange(t + 1, W + 299, 1.0):
                            bb = book(sd, tt * 1000)
                            path.append(bb[4] if bb is not None and bb[4] == bb[4] else 0.0)
                        res[vk].append(dict(slug=slug, a=a, W=W, t=float(t), x=float(W + 300 - t), z=z, px=px, sh=got, won=won, path=path, day=pd.to_datetime(W, unit="s").strftime("%m-%d")))
                        break
    return res, sig_info
if __name__ == "__main__":
    variants = [(3.0, 180, 238, 0.985, 0.99, 2.5), (3.0, 120, 238, 0.985, 0.99, 2.5), (2.5, 180, 238, 0.985, 0.99, 2.0), (3.5, 180, 238, 0.985, 0.99, 3.0),
                (3.0, 180, 238, 0.95, 0.99, 2.5), (3.0, 180, 238, 0.95, 0.984, 2.5), (3.0, 180, 238, 0.985, 0.99, 0.0)]
    res, si = run_all(sys.argv[2:], variants)
    print("windows with steps", len(si), "median sigma bp/s", round(np.median(si) * 1e4, 3))
    for vk, ent in res.items():
        n = len(ent); lost = sum(not e["won"] for e in ent)
        if not n: print(vk, "no entries"); continue
        pnl = sum(e["sh"] * ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) for e in ent); shs = sum(e["sh"] for e in ent)
        days = collections.Counter(e["day"] for e in ent); dl = collections.Counter(e["day"] for e in ent if not e["won"])
        print(f"z>={vk[0]} zone rel[{vk[1]},{vk[2]}] ask[{vk[3]},{vk[4]}] agree {vk[5]}: entries {n}, lost {lost} ({100*lost/n:.2f}%), avg px {np.mean([e['px'] for e in ent]):.4f}, "
              f"pnl/share {100*pnl/shs:+.2f}c, $ total at 10 sh {pnl:+.2f} | by day " + " ".join(f"{d}:{days[d]}/{dl[d]}" for d in sorted(days)))
        for e in ent:
            if not e["won"]: print("     LOST", e["slug"], f"x={e['x']:.0f}s z={e['z']:+.2f} px={e['px']:.3f} min bid after {min(e['path']) if e['path'] else None}")
    json.dump({str(k): v for k, v in res.items()}, open("early_books_res.json", "w"))
