"""Daily table of the wallet in the screenshot's format, by several time zones."""
import json, collections, time, sys
D = json.load(open(sys.argv[1])); rows, O = D["rows"], D["oc"]
T = {"11-09": (17, 0, 999.55, 11.34), "11-10": (32, 1, 2398.13, -63.90), "11-11": (20, 0, 2322.86, 27.68), "11-12": (27, 0, 2603.79, 38.50),
     "11-13": (43, 0, 4272.38, 54.15), "11-14": (39, 0, 2861.85, 36.68), "11-15": (52, 0, 3975.21, 56.68)}
def fee(p): return 0.07 * p * (1 - p)
for tz in (int(a) for a in sys.argv[2:]):
    day = collections.defaultdict(lambda: [0, 0, 0.0, 0.0, 0.0])
    for r in rows:
        k = time.strftime("%m-%d", time.gmtime(r["t"] + tz * 3600))
        win = O.get(r["s"], {}).get("win")
        cost = r["sz"] * r["px"]
        pay = r["sz"] if win == r["o"] else 0.0
        a = day[k]; a[0] += 1; a[1] += (pay == 0); a[2] += cost; a[3] += pay - cost; a[4] += r["sz"] * fee(r["px"])
    print(f"tz {tz:+d}:  day  n lost invested pnl fee  | screenshot")
    for k in sorted(day):
        a = day[k]; t = T.get(k, ("", "", "", ""))
        print(f"  {k} {a[0]:3d} {a[1]:2d} {a[2]:9.2f} {a[3]:+8.2f} {a[4]:5.2f} | {t}")
