"""Binance 1 s klines of today (data-api.binance.vision) for a symbol -> bn/<SYM>-1s-today.csv (open_ms, close)."""
import json, subprocess, sys, time, calendar
sym = sys.argv[1]
t0 = calendar.timegm(time.gmtime()[:3] + (0, 0, 0)) * 1000
now = int(time.time() * 1000) - 5000
rows = []
t = t0
while t < now:
    u = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1s&startTime={t}&limit=1000"
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "30", u], capture_output=True, text=True).stdout
        try:
            d = json.loads(r); break
        except Exception:
            time.sleep(1 + k)
    if not d: break
    rows += [(x[0], x[4]) for x in d]
    t = d[-1][0] + 1000
with open(f"bn/{sym}-1s-today.csv", "w") as f:
    for a, c in rows: f.write(f"{a},{c}\n")
print(sym, len(rows), "until", time.strftime("%H:%M:%S", time.gmtime(rows[-1][0] / 1000)))
