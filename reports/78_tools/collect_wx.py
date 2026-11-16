"""Station metadata (aviationweather), observations (IEM ASOS archive, routine METAR with raw text) and forecasts
(Open-Meteo previous-runs: forecasts issued 0/1/2 days earlier) for the stations of events.json.
usage: python collect_wx.py 2026 11 1 2026 11 17   (stations.json, obs.json, fc.json; Open-Meteo previous-runs has a daily quota)"""
import json, subprocess, time, sys, csv, io
from concurrent.futures import ThreadPoolExecutor
def get(u, as_json=True):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "90", u], capture_output=True, text=True).stdout
        if not as_json and r and not r.startswith("<"): return r
        try: return json.loads(r)
        except Exception: time.sleep(2 + 2 * k)
    return None
ev = json.load(open("events.json"))
st = sorted({v["station"] for v in ev.values() if v["station"]})
info = get("https://aviationweather.gov/api/data/stationinfo?ids=" + ",".join(st) + "&format=json") or []
meta = {x["icaoId"]: dict(lat=x["lat"], lon=x["lon"], elev=x.get("elev"), site=x.get("site"), country=x.get("country")) for x in info}
print("stations", len(st), "with metadata", len(meta), "missing", [s for s in st if s not in meta])
y0, m0, d0, y1, m1, d1 = (int(x) for x in sys.argv[1:7])
def obs(s):
    u = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={s}&data=tmpc&data=metar&year1={y0}&month1={m0}&day1={d0}"
         f"&year2={y1}&month2={m1}&day2={d1}&tz=Etc/UTC&format=onlycomma&latlon=no&missing=M&trace=T&direct=no&report_type=3")
    txt = get(u, as_json=False) or ""
    rows = list(csv.DictReader(io.StringIO(txt)))
    return s, [dict(t=r["valid"], tmpc=r["tmpc"], metar=r["metar"]) for r in rows]
def fc(s):
    m = meta.get(s)
    if not m: return s, None
    u = (f"https://previous-runs-api.open-meteo.com/v1/forecast?latitude={m['lat']}&longitude={m['lon']}&timezone=auto&past_days=15&forecast_days=3"
         f"&hourly=temperature_2m,temperature_2m_previous_day1,temperature_2m_previous_day2")
    return s, get(u)
O, F = {}, {}
with ThreadPoolExecutor(4) as ex:
    for s, rows in ex.map(obs, st): O[s] = rows
with ThreadPoolExecutor(3) as ex:
    for s, j in ex.map(fc, st): F[s] = j
json.dump(meta, open("stations.json", "w")); json.dump(O, open("obs.json", "w")); json.dump(F, open("fc.json", "w"))
print("obs rows", {s: len(v) for s, v in list(O.items())[:8]}, "... total", sum(len(v) for v in O.values()))
bad = [s for s, j in F.items() if not j or "hourly" not in j]
print("forecasts ok", len(F) - len(bad), "bad", bad[:10], (F[bad[0]] if bad else ""))
print("tz sample", {s: F[s].get("timezone") for s in list(F)[:6] if F[s]})
