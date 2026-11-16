"""Temperature events of a date range (Gamma): buckets, winner, station, unit.
usage: python collect_events.py 2026-11-03 2026-11-16   (writes events.json in the current folder)"""
import json, subprocess, re, time, sys, os, datetime
from concurrent.futures import ThreadPoolExecutor
def get(u):
    for k in range(3):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return []
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.weather.sources import discover_cities  # noqa: E402
cities = discover_cities()
d0, d1 = datetime.date.fromisoformat(sys.argv[1]), datetime.date.fromisoformat(sys.argv[2])
dates = [d0 + datetime.timedelta(days=i) for i in range((d1 - d0).days + 1)]
def slug(c, d): return f"highest-temperature-in-{c}-on-{d.strftime('%B').lower()}-{d.day}-{d.year}"
slugs = [slug(c, d) for c in cities for d in dates]
def fetch(batch):
    q = "&".join(f"slug={s}" for s in batch)
    return get(f"https://gamma-api.polymarket.com/events?{q}&limit=50") or []
out = {}
with ThreadPoolExecutor(6) as ex:
    for evs in ex.map(fetch, [slugs[i:i + 20] for i in range(0, len(slugs), 20)]):
        for e in evs if isinstance(evs, list) else []:
            ms = []
            for m in e.get("markets") or []:
                op = json.loads(m.get("outcomePrices") or "[]")
                ms.append(dict(label=m.get("groupItemTitle"), cid=m["conditionId"], tokens=json.loads(m.get("clobTokenIds") or "[]"),
                               yes=float(op[0]) if op else None, closed=bool(m.get("closed")), vol=float(m.get("volume") or 0)))
            src = (e["markets"][0].get("resolutionSource") or "") if e.get("markets") else ""
            st = re.search(r"ids=([A-Z0-9]{4})", src)
            date = re.search(r"-on-(.*)$", e["slug"]).group(1)
            out[e["slug"]] = dict(city=re.sub(r"highest-temperature-in-(.*?)-on-.*", r"\1", e["slug"]), date=date,
                                  station=st.group(1) if st else None, closed=bool(e.get("closed")), end=e.get("endDate"),
                                  unit="F" if any("°F" in (m["label"] or "") for m in ms) else "C", markets=ms,
                                  volume=float(e.get("volume") or 0), desc_ok="nearest whole degree" in (e.get("description") or ""))
json.dump(out, open("events.json", "w"))
print("cities", len(cities), "dates", len(dates), "events found", len(out), "closed", sum(v["closed"] for v in out.values()),
      "stations", len({v["station"] for v in out.values()}), "rules as expected", sum(v["desc_ok"] for v in out.values()))
