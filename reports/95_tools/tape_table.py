"""Every public taker trade of BTC 5m windows as a MAKER fill, with our Binance fair value, outcome and context -> table.npz

  python tape_table.py TAPE_DIR BN1S.npz OUT.npz [asset=btc] [duration_s=300]   (TAPE_DIR from fetch_tape.py, BN1S from bn_fetch.py)

A taker SELL of O at p = a maker bought O at p; a taker BUY of O at p = a maker bought the other side at 1 - p (the books are
mirrors). So every row is a long position of a bid-only maker: side d (+1 Up / -1 Down), price pl, size n.
  fu    fair value of Up at the trade second (Binance 1 s close of the second BEFORE it: no look-ahead):
        Phi(ln(S / K) / (sigma * sqrt(tau0 + 19.5))), K = mean of [T-62, T-3], sigma = RMS of 10 s returns over 1 h per 1 s
  edge  fair - price of the maker's side;  pnl = outcome - price (per share, makers pay no fee)
  mk5, mk30   the maker side's fair value 5 / 30 s later minus now (adverse selection: < 0 = the fill was picked off)
  pre_LK      Binance move toward the maker's side over K seconds ending L seconds before the trade (bp)
  mL          the market's price of the maker's side = mean Up price of the trades of the 5 s before (>= 3 trades), seen from the maker
  sg    sigma (bp per sqrt(s));  z the fair value's argument;  rel the second of the window;  day = UTC day number."""
import json, sys
import numpy as np
from scipy.special import ndtr

TAPE, BN, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
ASSET = sys.argv[4] if len(sys.argv) > 4 else "btc"
DUR = int(sys.argv[5]) if len(sys.argv) > 5 else 300
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}[ASSET]
B = np.load(BN)
T0 = int(B["t0"])
PX = B[SYMBOL].astype(float)
LP = np.log(PX)
N = len(PX)
# sigma per second: RMS of 10 s log returns over the last hour (360 samples, stride 10), per 1 s
sq = np.full(N, np.nan)
sq[10:] = (LP[10:] - LP[:-10]) ** 2
SIG = np.full(N, np.nan)
for c in range(10):
    idx = np.arange(c, N, 10)
    v = sq[idx]
    v = np.where(np.isnan(v), 0.0, v)
    cs = np.cumsum(v)
    roll = cs.copy()
    roll[360:] = cs[360:] - cs[:-360]
    SIG[idx[360:]] = np.sqrt(roll[360:] / 360.0 / 10.0)
# the same RMS over the last 10 min (60 samples): the regime of the moment against the hour's average
SIG10 = np.full(N, np.nan)
for c in range(10):
    idx = np.arange(c, N, 10)
    v = np.where(np.isnan(sq[idx]), 0.0, sq[idx])
    cs = np.cumsum(v)
    roll = cs.copy()
    roll[60:] = cs[60:] - cs[:-60]
    SIG10[idx[60:]] = np.sqrt(roll[60:] / 60.0 / 10.0)
W = {w["slug"]: w for w in json.load(open(f"{TAPE}/windows.json")) if w["asset"] == ASSET and w["end"] - w["start"] == DUR}
TR = json.load(open(f"{TAPE}/trades.json"))
cols = {k: [] for k in "t start rel d pl n won u".split()}
for s, rows in TR.items():
    w = W.get(s)
    if not w:
        continue
    up_won = w["win"] == "Up"
    for ts, sd, out, n, p, wal in rows:
        if not (w["start"] <= ts < w["end"]):
            continue
        if sd == "S":
            d = 1 if out == "Up" else -1
            pl = p
        else:
            d = -1 if out == "Up" else 1
            pl = 1.0 - p
        cols["t"].append(ts); cols["start"].append(w["start"]); cols["rel"].append(ts - w["start"])
        cols["d"].append(d); cols["pl"].append(pl); cols["n"].append(n)
        cols["won"].append(1.0 if (d == 1) == up_won else 0.0)
        cols["u"].append(p if out == "Up" else 1.0 - p)          # the Up price this trade says (bid or ask side)
A = {k: np.array(v) for k, v in cols.items()}
t, start = A["t"].astype(np.int64), A["start"].astype(np.int64)
i = t - T0 - 1                                             # the close of the second before the trade = the price at second t


def fair_up(ii, st, tt):
    """Phi(z) and z at index ii (a second index), for windows starting at st, evaluated at second tt."""
    K = np.empty(len(st))
    for s in np.unique(st):                                 # strike per window: mean of [s-62, s-3]
        m = st == s
        K[m] = PX[s - 62 - T0: s - 2 - T0].mean()
    tau0 = np.maximum(0.0, (st + DUR - 62) - tt)
    sg = SIG[ii]
    z = np.log(PX[ii] / K) / (sg * np.sqrt(tau0 + 19.5) + 1e-18)
    z = np.where(tau0 > 0, z, np.nan)
    return ndtr(z), z, sg


fu, z, sg = fair_up(i, start, t)
d = A["d"]
fL = np.where(d == 1, fu, 1 - fu)
out = dict(t=t, rel=A["rel"], d=d, pl=A["pl"], n=A["n"], fL=fL, z=z, sg=sg * 1e4, edge=fL - A["pl"], pnl=A["won"] - A["pl"],
           day=(t - T0) // 86400, sg10=SIG10[i] * 1e4)
for k in (5, 30):
    ok = i + k < N
    fu_k, _, _ = fair_up(np.minimum(i + k, N - 1), start, t + k)
    out[f"mk{k}"] = np.where(ok, np.where(d == 1, fu_k, 1 - fu_k) - fL, np.nan)
for L, K in ((0, 1), (0, 3), (1, 2), (1, 5), (2, 3), (0, 10)):
    out[f"pre_{L}{K}"] = d * 1e4 * (LP[i - L] - LP[i - L - K])
# taker flow of the previous 10 s of the same window, toward the maker's side: net shares the takers sold into our side
# (maker-long shares: + = takers have been hitting bids on this side). Computed per window by a sorted cumulative sum.
flow = np.zeros(len(t))
order = np.lexsort((t, start))
ts_o, st_o, d_o, n_o = t[order], start[order], d[order], A["n"][order]
for s in np.unique(st_o):
    m = np.where(st_o == s)[0]
    tt_, dd_, nn_ = ts_o[m], d_o[m], np.minimum(n_o[m], 50) * d_o[m]
    cs = np.concatenate([[0.0], np.cumsum(nn_)])
    lo = np.searchsorted(tt_, tt_ - 10, side="left")
    hi = np.arange(len(m))                                  # trades strictly before this one (same second ones are ignored)
    prev = cs[hi] - cs[lo]
    flow[order[m]] = prev * dd_                              # + = previous maker-long flow was on OUR side
out["flow10"] = flow
# market proxy: the mean Up price of the trades of the 5 seconds BEFORE this one (strictly earlier seconds) in the same window,
# seen from the maker's side (Up price or 1 - Up price); needs >= 3 such trades
mU = np.full(len(t), np.nan); mN = np.zeros(len(t))
u_o = A["u"][order]
for s in np.unique(st_o):
    m = np.where(st_o == s)[0]
    tt_, uu_ = ts_o[m], u_o[m]
    cs = np.concatenate([[0.0], np.cumsum(uu_)])
    lo = np.searchsorted(tt_, tt_ - 5, side="left")
    hi = np.searchsorted(tt_, tt_ - 1, side="right")       # trades with ts <= t - 1
    cnt = hi - lo
    ok_ = cnt >= 3
    mean = np.where(ok_, (cs[hi] - cs[lo]) / np.maximum(cnt, 1), np.nan)
    mU[order[m]] = mean; mN[order[m]] = cnt
out["mL"] = np.where(d == 1, mU, 1 - mU)
out["mN"] = mN
np.savez_compressed(OUT, **out)
ok = (out["rel"] >= 20) & (out["rel"] <= DUR - 70) & np.isfinite(out["fL"])
print("trades", len(t), "in rel 20..230 with a fair value", int(ok.sum()), "days", np.unique(out["day"]))
