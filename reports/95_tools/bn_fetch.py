"""Binance 1 s closes -> bn1s.npz (forward-filled, one close per second from T0).

  python bn_fetch.py FROM TO OUT.npz SYMBOL...        FROM / TO = YYYY-MM-DD (UTC, TO exclusive)

Daily zips from data.binance.vision (public); the current day comes from the klines API. Cached in ./bn."""
import calendar, io, json, os, subprocess, sys, time, zipfile
import numpy as np

FROM, TO, OUT, SYMS = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]


def day(s):
    y, m, d = map(int, s.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0))


T0, T1 = day(FROM), day(TO)
N = T1 - T0
os.makedirs("bn", exist_ok=True)
res = {}
for sym in SYMS:
    px = np.full(N, np.nan)
    for d0 in range(T0, T1, 86400):
        ds = time.strftime("%Y-%m-%d", time.gmtime(d0))
        z = f"bn/{sym}-1s-{ds}.zip"
        if not os.path.exists(z):
            u = f"https://data.binance.vision/data/spot/daily/klines/{sym}/1s/{sym}-1s-{ds}.zip"
            subprocess.run(["curl", "-sS", "-m", "300", "-f", "-o", z, u], capture_output=True)
        if os.path.exists(z) and os.path.getsize(z) > 1000:
            with zipfile.ZipFile(z) as zf:
                raw = zf.read(zf.namelist()[0]).decode()
            a = np.loadtxt(io.StringIO(raw), delimiter=",", usecols=(0, 4), ndmin=2)
            ts = a[:, 0]
            ts = ts / 1e6 if ts[0] > 1e14 else ts / 1e3
            i = ts.astype(np.int64) - T0
            ok = (i >= 0) & (i < N)
            px[i[ok]] = a[ok, 1]
        else:                                           # the day has no zip yet: klines API (1000 s a call)
            t = d0 * 1000
            end = min((d0 + 86400) * 1000, int(time.time() * 1000) - 5000)
            while t < end:
                r = subprocess.run(["curl", "-sS", "-m", "30", f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1s&startTime={t}&limit=1000"],
                                   capture_output=True, text=True).stdout
                try:
                    k = json.loads(r)
                except Exception:
                    time.sleep(1)
                    continue
                if not k:
                    break
                for x in k:
                    j = x[0] // 1000 - T0
                    if 0 <= j < N:
                        px[j] = float(x[4])
                t = k[-1][0] + 1000
    last = int(np.max(np.where(~np.isnan(px))[0]))
    idx = np.where(~np.isnan(px), np.arange(N), 0)
    np.maximum.accumulate(idx, out=idx)
    px = px[idx]
    px[last + 1:] = np.nan
    res[sym] = px
    print(sym, "seconds", int(np.sum(~np.isnan(px))), "last", time.strftime("%m-%d %H:%M:%S", time.gmtime(T0 + last)), flush=True)
np.savez_compressed(OUT, t0=T0, **res)
