"""Per event: the day's routine observations (UTC ts, local hour, temp C), the final qualifying max, the winner.
usage: python build_days.py   (needs events.json, obs_all.json with every report and stations.json with routine_min)"""
import json, sys, datetime
sys.path.insert(0, ".")
from wxlib import metar_temp, qualifying, parse_bucket, local_date, event_date
ev = json.load(open("events.json")); O = json.load(open("obs_all.json")); meta = json.load(open("stations.json"))
days = {}
for slug, e in ev.items():
    if not e["closed"] or not e["desc_ok"] or not e["station"]: continue
    s = e["station"]; tz = meta[s]["tz"]; d = event_date(e["date"]); rm = set(meta[s]["routine_min"])
    obs = []
    for o in O.get(s, []):
        if o["t"][-2:] not in rm: continue
        lt = local_date(o["t"], tz)
        if lt.date() != d: continue
        t = metar_temp(o["metar"])
        if t is None: continue
        ts = datetime.datetime.fromisoformat(o["t"]).replace(tzinfo=datetime.timezone.utc).timestamp()
        obs.append((ts, lt.hour + lt.minute / 60, t))
    if len(obs) < 12: continue
    obs.sort()
    F = max(t for _, _, t in obs)
    win = [m for m in e["markets"] if m["yes"] == 1.0]
    q = qualifying(F, e["unit"])
    if not win or not (parse_bucket(win[0]["label"])[0] <= q <= parse_bucket(win[0]["label"])[1]): continue
    start = datetime.datetime.combine(d, datetime.time(0), tzinfo=__import__("zoneinfo").ZoneInfo(tz)).timestamp()
    days[slug] = dict(station=s, date=str(d), unit=e["unit"], obs=obs, F=F, start=start,
                      buckets=[dict(label=m["label"], cid=m["cid"], lo=parse_bucket(m["label"])[0], hi=parse_bucket(m["label"])[1],
                                    won=m["yes"] == 1.0) for m in e["markets"]])
json.dump(days, open("days.json", "w"))
print("station-days", len(days), "dates", sorted({v["date"] for v in days.values()}))
