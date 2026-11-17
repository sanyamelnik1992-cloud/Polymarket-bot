"""Per-step table of the early zone on recorded 5m books: every 0.5 s of window seconds 120-238, both z readings
(Chainlink nowcast with the Chainlink 1 h sigma, and Binance mid with the Binance 1 h sigma and a Binance strike, as in
report 80), the leader asks now and 0.3 s later, our side's bids later (stop analysis). Outcome from Gamma.
usage: early_steps.py windows_all.json OUT.parquet TAPE_PREFIX [...]"""
import json, sys, collections, math
import numpy as np, pandas as pd
GW = {w["slug"]: w["win"] for w in json.load(open(sys.argv[1]))}
rows = []
def series_1s(pairs):
    """[(sec, v)] -> (s0, forward-filled per-second array)"""
    d = {}
    for s_, v_ in pairs: d[int(s_)] = v_
    s0, s1 = min(d), max(d); arr = np.full(s1 - s0 + 1, np.nan)
    for s_, v_ in d.items(): arr[s_ - s0] = v_
    ok = ~np.isnan(arr); idx = np.where(ok, np.arange(len(arr)), 0); np.maximum.accumulate(idx, out=idx)
    return s0, arr[idx], d
def hour_sigma(s0, arr):
    r2 = np.concatenate([[0.0], np.diff(np.log(arr)) ** 2]); cs = np.concatenate([[0.0], np.cumsum(r2)])
    def f(sec):
        i = int(sec) - s0
        if i - 3600 < 0 or i >= len(arr): return None
        return math.sqrt((cs[i + 1] - cs[i + 1 - 3600]) / 3600)
    return f
for prefix in sys.argv[3:]:
    cl = collections.defaultdict(list); bn = collections.defaultdict(list)
    for line in open(prefix + "_chainlink.jsonl"):
        e = json.loads(line); cl[e["sym"].split("/")[0]].append((e["rx"], e["ts"], e["v"]))
    for line in open(prefix + "_binance.jsonl"):
        e = json.loads(line)
        if e.get("bid") and e.get("ask"): bn[e["sym"][:-4].lower()].append((e["rx"], 0.5 * (e["bid"] + e["ask"])))
    tob = pd.read_csv(prefix + "_tob.csv"); tob = tob[~tob.k.str.startswith("GAP")]
    p3 = tob.k.str.split("-"); tob["a"] = p3.str[0]; tob["W"] = p3.str[1].astype(int); tob["sd"] = p3.str[2]
    for a in sorted(cl):
        c = sorted(cl[a]); rx = np.array([x[0] for x in c]); ts = np.array([x[1] for x in c]); v = np.array([x[2] for x in c])
        s0, carr, by_sec = series_1s([(t_ // 1000, v_) for t_, v_ in zip(ts, v)]); sig_c = hour_sigma(s0, carr)
        b = sorted(bn.get(a, []))
        if not b: continue
        brx = np.array([x[0] for x in b]); bmid = np.array([x[1] for x in b])
        # Binance mid at each whole second (last tick at or before it)
        bs0 = int(brx[0] // 1000) + 1; secs = np.arange(bs0, int(brx[-1] // 1000) + 1)
        j = np.searchsorted(brx, secs * 1000, side="right") - 1; barr = bmid[np.maximum(j, 0)]
        sig_b = hour_sigma(bs0, barr)
        def bn_at(ms):
            k = np.searchsorted(brx, ms, side="right") - 1
            return bmid[k] if k >= 0 else None
        ta = tob[tob.a == a]; groups = {k: g.sort_values("rx") for k, g in ta.groupby(["W", "sd"])}
        for W in sorted(set(ta.W)):
            if (W, "D") not in groups or (W, "U") not in groups: continue
            slug = f"{a}-updown-5m-{W}"
            if slug not in GW: continue
            xs, last = [], None
            for s_ in range(W - 72, W - 2):
                last = by_sec.get(s_, last)
                if s_ >= W - 62: xs.append(last)
            if not xs or None in xs: continue
            K = float(np.mean(xs))
            i0, i1 = W - 62 - bs0, W - 2 - bs0
            if i0 < 0 or i1 > len(barr): continue
            Kb = float(barr[i0:i1].mean())
            G = {sd: tuple(groups[(W, sd)][c_].values for c_ in ("rx", "ask", "ask_sz", "ask2", "ask2_sz", "bid")) for sd in ("U", "D")}
            def book(sd, ms):
                g = G[sd]; k = np.searchsorted(g[0], ms, side="right") - 1
                return None if k < 0 else tuple(x[k] for x in g[1:])
            win = 1 if GW[slug] == "Up" else 0
            # our-side bids for the stop analysis: at the end of each 5 s after rel 120 (U and D)
            for t in np.arange(W + 120, W + 238.01, 0.5):
                ms = t * 1000; k = np.searchsorted(rx, ms, side="right") - 1
                if k < 0 or ms - rx[k] > 2600: continue
                bnow, blast = bn_at(ms), bn_at(ts[k] + 999)
                sc, sb = sig_c(t), sig_b(t)
                if not bnow or not blast or not sc or not sb: continue
                tau = (W + 238) - t + 19.5
                z = math.log(v[k] * bnow / blast / K) / (sc * math.sqrt(tau)); zc = math.log(v[k] / K) / (sc * math.sqrt(tau))
                zb = math.log(bnow / Kb) / (sb * math.sqrt(tau))
                bu, bd, fu, fd = book("U", ms), book("D", ms), book("U", ms + 300), book("D", ms + 300)
                if bu is None or bd is None: continue
                rows.append((slug, a, W, float(t), z, zc, zb, sc * 1e4, sb * 1e4, bu[0], bu[1], bd[0], bd[1],
                             fu[0] if fu else np.nan, fu[1] if fu else np.nan, fu[2] if fu else np.nan, fu[3] if fu else np.nan,
                             fd[0] if fd else np.nan, fd[1] if fd else np.nan, fd[2] if fd else np.nan, fd[3] if fd else np.nan,
                             bu[4], bd[4], win))
    print(prefix, "rows", len(rows), flush=True)
cols = ["slug", "a", "W", "t", "z", "zc", "zb", "sig_c", "sig_b", "askU", "askU_sz", "askD", "askD_sz", "fU", "fU_sz", "fU2", "fU2_sz",
        "fD", "fD_sz", "fD2", "fD2_sz", "bidU", "bidD", "win"]
pd.DataFrame(rows, columns=cols).to_parquet(sys.argv[2])
