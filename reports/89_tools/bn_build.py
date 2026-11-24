"""bn/*.zip (+ bn/<SYM>-1s-today.csv) -> bn1s.npz: one close per second from T0 (forward-filled)."""
import sys, zipfile, io, glob, os, calendar, time
import numpy as np
T0 = calendar.timegm((2026, 11, 10, 0, 0, 0)); N = 15 * 86400
res = {}
for sym in sys.argv[1:]:
    px = np.full(N, np.nan)
    srcs = sorted(glob.glob(f"bn/{sym}-1s-2026-*.zip")) + sorted(glob.glob(f"bn/{sym}-1s-today.csv"))
    for f in srcs:
        if f.endswith(".zip"):
            with zipfile.ZipFile(f) as z:
                raw = z.read(z.namelist()[0]).decode()
        else:
            raw = open(f).read()
        a = np.loadtxt(io.StringIO(raw), delimiter=",", usecols=(0, 4) if f.endswith(".zip") else (0, 1), ndmin=2)
        ts = a[:, 0]; ts = ts / 1e6 if ts[0] > 1e14 else ts / 1e3
        i = ts.astype(np.int64) - T0
        ok = (i >= 0) & (i < N)
        px[i[ok]] = a[ok, 1]
    last = int(np.max(np.where(~np.isnan(px))[0]))
    idx = np.where(~np.isnan(px), np.arange(N), 0); np.maximum.accumulate(idx, out=idx); px = px[idx]
    px[last + 1:] = np.nan
    res[sym] = px
    print(sym, "seconds", int(np.sum(~np.isnan(px))), "last", time.strftime("%m-%d %H:%M:%S", time.gmtime(T0 + last)))
np.savez_compressed("bn1s.npz", t0=T0, **res)
