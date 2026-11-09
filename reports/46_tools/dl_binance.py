"""BTCUSDT 1s closes from data-api.binance.vision. usage: dl_binance.py SYMBOL FROM_TS TO_TS OUT.json -> {sec: close}"""
import json, subprocess, sys
sym, t0, t1, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
res, s = {}, t0 * 1000
while s < t1 * 1000:
    r = subprocess.run(["curl", "-s", "--max-time", "30", f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1s&startTime={s}&limit=1000"], capture_output=True, text=True)
    d = json.loads(r.stdout)
    if not d: break
    for k in d: res[k[0] // 1000] = float(k[4])
    s = d[-1][0] + 1000
json.dump(res, open(out, "w")); print(out, len(res))
