"""Binance 1 s klines (data.binance.vision daily zips) -> one array of closes per second, forward-filled. Usage: bn_load.py DIR OUT.npz"""
import sys, zipfile, io, glob, os, calendar
import numpy as np
src, out = sys.argv[1], sys.argv[2]
t0 = calendar.timegm((2026, 11, 8, 0, 0, 0)); n = 9 * 86400
res = {}
for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
    px = np.full(n, np.nan)
    for f in sorted(glob.glob(os.path.join(src, f"{sym}-1s-*.zip"))):
        with zipfile.ZipFile(f) as z:
            raw = z.read(z.namelist()[0]).decode()
        a = np.loadtxt(io.StringIO(raw), delimiter=",", usecols=(0, 4), ndmin=2, comments=None) if raw[0].isdigit() else None
        if a is None:
            a = np.loadtxt(io.StringIO(raw), delimiter=",", usecols=(0, 4), skiprows=1, ndmin=2)
        ts = a[:, 0]
        ts = ts / 1e6 if ts[0] > 1e14 else ts / 1e3          # microseconds since 2025, milliseconds before
        i = ts.astype(np.int64) - t0
        ok = (i >= 0) & (i < n)
        px[i[ok]] = a[ok, 1]
    # forward fill
    idx = np.where(~np.isnan(px), np.arange(n), 0); np.maximum.accumulate(idx, out=idx); px = px[idx]
    res[sym] = px
    print(sym, "seconds with data", int(np.sum(~np.isnan(px))), "first", px[0], "last", px[-1])
np.savez_compressed(out, t0=t0, **res)
