"""Binance 1 s klines (data.binance.vision daily zips) -> one array of closes per second from 31 Oct, forward-filled. Usage: bn_load_all.py DIR OUT.npz"""
import sys, zipfile, io, glob, os, calendar
import numpy as np
src, out = sys.argv[1], sys.argv[2]
t0 = calendar.timegm((2026, 10, 31, 0, 0, 0)); n = 18 * 86400
res = {}
for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
    px = np.full(n, np.nan)
    for f in sorted(glob.glob(os.path.join(src, f"{sym}-1s-*.zip"))):
        with zipfile.ZipFile(f) as z:
            raw = z.read(z.namelist()[0]).decode()
        skip = 0 if raw[0].isdigit() else 1
        a = np.loadtxt(io.StringIO(raw), delimiter=",", usecols=(0, 4), skiprows=skip, ndmin=2)
        ts = a[:, 0]
        ts = ts / 1e6 if ts[0] > 1e14 else ts / 1e3
        i = ts.astype(np.int64) - t0
        ok = (i >= 0) & (i < n)
        px[i[ok]] = a[ok, 1]
    have = ~np.isnan(px)
    first = int(np.argmax(have))
    idx = np.where(have, np.arange(n), first); np.maximum.accumulate(idx, out=idx); px = px[idx]
    res[sym] = px
    print(sym, "seconds with data", int(have.sum()), "of", n)
np.savez_compressed(out, t0=t0, **res)
