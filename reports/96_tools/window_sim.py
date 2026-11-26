"""Window-level simulation of the maker on the public tape (reports/96): the inventory the per-trade simulation of reports/95 ignores.

  python window_sim.py TABLE.npz [TABLE2.npz ...]        (tables from reports/95_tools/tape_table.py)

Per window, the taker trades in time order. At each trade our bid on the maker's side d is the rule's bid from the information of the
second before (fair value fL, market proxy mL, Binance move pre_03 / pre_12): b = floor((ref - h) * 100) / 100, ref = min(fair, market),
h = the favourite / underdog spread, plus the inventory skew of the live rule (long side lower, short side higher up to ref). One order of
`shares` per side; after it is filled (fully) the side has no bid for `repl_s` seconds (cancel / place round trip).
Fill models: "at"   = a trade at a price <= b fills us (reports/95: optimistic, we are never behind a queue at our price),
             "thru" = only a trade at a price < b (the price went THROUGH our level: certain, and exactly the adverse case).
PnL per share = outcome - our price (makers pay no fee). Output per rule: windows with a fill, shares, c a share, $ per filled window,
share of losing windows, the 5 % worst window, and the chance that a day of N filled windows hits the live day stop ($5)."""
import sys
import numpy as np

COLS = ("t", "rel", "d", "pl", "n", "fL", "z", "mL", "pre_03", "pre_12", "pnl", "day", "sg", "sg10")


def load(paths):
    parts = []
    for p in paths:
        T = np.load(p)
        parts.append({k: T[k] for k in COLS})
    A = {k: np.concatenate([x[k] for x in parts]) for k in COLS}
    A["start"] = (A["t"] - A["rel"]).astype(np.int64)
    A["won"] = A["pnl"] + A["pl"]
    o = np.lexsort((A["t"], A["start"]))
    return {k: v[o] for k, v in A.items()}


def phi(z):
    return np.exp(-0.5 * z * z) / np.sqrt(2 * np.pi)


def simulate(A, rule, fill="at"):
    """rule: dict of hf, hd, fav, rel_lo, rel_hi, pmin, pmax, gate, gate_col, max_side, max_unp, skew, shares, repl_s,
    k_time (time-scaled spread), pair_stop, late_rel (from this second only bids that do not grow the imbalance),
    vol_ratio (the time-scaled spread times max(1, 10-min sigma / 1-h sigma)), calm (> 0: no bid while the 10-min sigma is above
    calm x the 1-h sigma), cool_s (after ANY fill of a side, no bid on that side for this many seconds)."""
    r = dict(hf=0.015, hd=0.10, fav=0.5, rel_lo=100, rel_hi=230, pmin=0.10, pmax=0.80, gate=0.5, gate_col="pre_03",
             max_side=10, max_unp=5, skew=0.01, shares=5, repl_s=1, k_time=0.0, pair_stop=False, late_rel=999, ref="min",
             vol_ratio=False, calm=0.0, cool_s=0)
    unknown = set(rule) - set(r)
    if unknown:
        raise KeyError(f"unknown rule keys {sorted(unknown)}")
    r.update(rule)
    st, t, rel, d, pl, n = A["start"], A["t"], A["rel"], A["d"], A["pl"], A["n"]
    fL, mL, z, won, gc = A["fL"], A["mL"], A["z"], A["won"], A[r["gate_col"]]
    vr = np.where(np.isfinite(A["sg10"]) & (A["sg"] > 0), A["sg10"] / np.maximum(A["sg"], 1e-9), 1.0)
    res = []                                            # per window: (start, day, up, dn, cost, pnl, n_fills)
    bounds = np.flatnonzero(np.diff(st)) + 1
    for lo, hi in zip(np.r_[0, bounds], np.r_[bounds, len(st)]):
        have = {1: 0.0, -1: 0.0}
        cost = 0.0
        pnl = 0.0
        nf = 0
        left = {1: r["shares"], -1: r["shares"]}        # what is left of the resting order of each side
        dead = {1: -1, -1: -1}                          # no bid on the side until this second (after a full fill)
        stop = False
        for i in range(lo, hi):
            if stop:
                break
            if not (r["rel_lo"] <= rel[i] <= r["rel_hi"]) or not np.isfinite(fL[i]):
                continue
            s = int(d[i])
            if t[i] < dead[s]:
                continue
            f = fL[i]
            ref = min(f, mL[i]) if (r["ref"] == "min" and np.isfinite(mL[i])) else f
            if not (r["pmin"] <= ref <= r["pmax"]):
                continue
            if r["gate"] and gc[i] < -r["gate"]:
                continue
            if r["calm"] and vr[i] > r["calm"]:
                continue
            long_by = have[s] - have[-s]
            if have[s] >= r["max_side"] - 1e-9 or long_by >= r["max_unp"] - 1e-9:
                continue
            if rel[i] >= r["late_rel"] and long_by >= 0:
                continue                                 # late: only the short side (it pairs), nothing that grows the imbalance
            h = r["hf"] if f >= r["fav"] else r["hd"]
            if r["k_time"] > 0:
                tau0 = max(0.0, 238 - rel[i])
                h = max(h, r["k_time"] * phi(z[i]) / np.sqrt(tau0 + 19.5) * (max(1.0, vr[i]) if r["vol_ratio"] else 1.0))
            k = r["skew"] * abs(long_by) / 5.0
            h = h + k if long_by > 0 else h - min(h, k)
            b = np.floor((ref - h) * 100 + 1e-9) / 100
            if b < 0.05:
                continue
            ok = pl[i] <= b + 1e-9 if fill == "at" else pl[i] < b - 1e-9
            if not ok:
                continue
            q = min(n[i], left[s])
            have[s] += q
            cost += q * b
            pnl += q * (won[i] - b)
            nf += 1
            left[s] -= q
            if left[s] <= 1e-9:
                left[s] = r["shares"]
                dead[s] = t[i] + r["repl_s"]
            if r["cool_s"]:
                dead[s] = max(dead[s], t[i] + r["cool_s"])
            if r["pair_stop"] and have[1] >= r["shares"] - 1e-9 and abs(have[1] - have[-1]) < 1e-9:
                stop = True
        if nf:
            res.append((st[lo], int(A["day"][lo]), have[1], have[-1], cost, pnl, nf))
    return np.array(res, dtype=float).reshape(-1, 7)


def summary(label, R, stop=5.0, per_day=50):
    if not len(R):
        print(f"  {label:58s} no fills")
        return
    sh = (R[:, 2] + R[:, 3]).sum()
    pnl = R[:, 5]
    unp = np.abs(R[:, 2] - R[:, 3])
    # chance that `per_day` filled windows in a row reach -stop at some point (bootstrap of the windows)
    rng = np.random.default_rng(1)
    paths = rng.choice(pnl, size=(4000, per_day)).cumsum(axis=1)
    p_stop = (paths.min(axis=1) <= -stop).mean()
    days = np.unique(R[:, 1])
    byd = " ".join(f"{100 * R[R[:, 1] == x, 5].sum() / max((R[R[:, 1] == x, 2] + R[R[:, 1] == x, 3]).sum(), 1):+5.1f}" for x in days)
    print(f"  {label:58s} win {len(R):5d}  {100 * pnl.sum() / sh:+6.2f} c/sh  {pnl.mean():+.3f} $/win  lost {100 * (pnl < -0.005).mean():3.0f} %"
          f"  unpaired {unp.mean():4.1f}  5% worst {np.percentile(pnl, 5):+.2f}  stop@{per_day} {100 * p_stop:3.0f} %  [{byd}]")


RULES = [
    ("V2 (live 26 Nov)", {}),
    ("V2, quotes end at second 200", {"rel_hi": 200}),
    ("V2, quotes end at second 180", {"rel_hi": 180}),
    ("V2, spread >= 1.0 x the fair's 1 s move", {"k_time": 1.0}),
    ("V2, spread >= 1.5 x the fair's 1 s move", {"k_time": 1.5}),
    ("V2, spread >= 2.0 x the fair's 1 s move", {"k_time": 2.0}),
    ("V2, stop the window after its first pair", {"pair_stop": True}),
    ("V2, from second 170 only the pairing side", {"late_rel": 170}),
    ("V2, max 5 a side (one fill a side)", {"max_side": 5}),
    ("V2, gate with 1 s of reaction delay", {"gate_col": "pre_12"}),
    ("V2, favourite only", {"hd": 0.5}),
    ("C1: end 200 + spread 1.5x + pairing side from 170", {"rel_hi": 200, "k_time": 1.5, "late_rel": 170}),
    ("C2: end 180 + spread 1.5x + max 5 a side", {"rel_hi": 180, "k_time": 1.5, "max_side": 5}),
    ("C3: end 200 + spread 2x + one fill a side + pair stop", {"rel_hi": 200, "k_time": 2.0, "max_side": 5, "pair_stop": True}),
]

if __name__ == "__main__":
    A = load(sys.argv[1:])
    print("rows", len(A["t"]), "days", np.unique(A["day"]))
    for fill in ("at", "thru"):
        print(f"fill model: {fill}")
        for label, rule in RULES:
            summary(label, simulate(A, rule, fill))
