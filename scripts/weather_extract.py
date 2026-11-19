"""Compact table of the weather recorder for analysis off the server (reports/78, reports/84).

One row per (event, decision time, bucket) from data/weather/DATE/{events,metar,books,forecasts}.jsonl.gz. A decision time is
H hours before the end of the event's local day (H in H_BEFORE). Columns:
  slug, city, station, unit, date, h, ts (the decision time, UTC s), label, lo, hi (bucket bounds in the event's unit, empty =
  open), bid, ask, bid_sz, ask_sz (the last book snapshot within 30 min before ts), fc_t (fetch time of the forecast used),
  fc_ecmwf_ifs025, fc_gfs_seamless, fc_icon_seamless (each model's max of the event's local day, deg C, from the latest
  forecast fetched before ts), obs_c, n_obs (qualifying max of the routine METARs of the local day received before ts, deg C),
  won (1 / 0, the final winner).
Events without a final winner are skipped. Streams the files (the server has 2 GB): only the rows needed are kept.
One file per event date, OUT_DIR/extract-DATE.csv.gz, rewritten only when its content changes (the server-reports branch keeps
every version, so a finished date costs its ~150 KB once).
Usage: weather_extract.py SRC_DIR OUT_DIR [DAYS_BACK]
"""
import csv
import datetime
import glob
import gzip
import io
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.weather.rules import day_max  # noqa: E402

H_BEFORE = (36, 24, 18, 12, 8, 6, 4, 2)
MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")
STATIONS = os.path.join(os.path.dirname(__file__), "..", "config", "weather_stations.json")
COLS = ["slug", "city", "station", "unit", "date", "h", "ts", "label", "lo", "hi", "bid", "ask", "bid_sz", "ask_sz", "fc_t"] + \
       [f"fc_{m}" for m in MODELS] + ["obs_c", "n_obs", "won"]


def read(path):
    """Records of one gzipped JSON-lines file; a half-written last line (the recorder is appending) is skipped."""
    if not os.path.exists(path):
        return
    try:
        with gzip.open(path, "rt") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (OSError, EOFError):
        return


def day_end(date, tz):
    d = datetime.date.fromisoformat(date) + datetime.timedelta(days=1)
    return datetime.datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tz)).timestamp()


def extract(src, out, days_back=14, now=None):
    now = now or time.time()
    tzs = {k: v.get("tz") for k, v in json.load(open(STATIONS)).items()}
    dirs = sorted(glob.glob(os.path.join(src, "????-??-??")))[-days_back:]
    events = {}
    for d in dirs:
        for e in read(os.path.join(d, "events.jsonl.gz")):
            events[e["slug"]] = e                                   # the latest state of an event wins
    dec = {}                                                         # slug -> {h: ts}
    for s, e in events.items():
        won = [b for b in e.get("markets") or [] if b.get("won")]
        tz = tzs.get(e.get("station") or "")
        if len(won) != 1 or not tz:
            continue
        end = day_end(e["date"], tz)
        dec[s] = {h: end - 3600 * h for h in H_BEFORE if end - 3600 * h <= now}
    book = {}                                                        # (slug, label, h) -> latest snapshot <= ts within 30 min
    for d in dirs:
        for b in read(os.path.join(d, "books.jsonl.gz")):
            for h, ts in dec.get(b.get("slug"), {}).items():
                if ts - 1800 < b["t"] <= ts:
                    k = (b["slug"], b["label"], h)
                    if k not in book or book[k]["t"] < b["t"]:
                        book[k] = b
    by_station = {}
    for s, e in events.items():
        if s in dec:
            by_station.setdefault(e["station"], []).append(s)
    fc = {}                                                          # (slug, h) -> (fetch time, {model: day max})
    for d in dirs:
        for f in read(os.path.join(d, "forecasts.jsonl.gz")):
            slugs = by_station.get(f.get("station"))
            if not slugs or not f.get("time"):
                continue
            days = {}
            for i, tt in enumerate(f["time"]):
                days.setdefault(tt[:10], []).append(i)
            for s in slugs:
                idx = days.get(events[s]["date"])
                if not idx:
                    continue
                mx = {}
                for m in MODELS:
                    xs = [v for v in ((f.get("temps") or {}).get(m) or [None] * len(f["time"]))[idx[0]:idx[-1] + 1] if v is not None]
                    mx[m] = max(xs) if xs else None
                for h, ts in dec[s].items():
                    if f["t"] <= ts and ((s, h) not in fc or fc[(s, h)][0] < f["t"]):
                        fc[(s, h)] = (f["t"], mx)
    lines = {}                                                       # station -> [(received t, raw line)]
    for d in dirs:
        for m in read(os.path.join(d, "metar.jsonl.gz")):
            if m.get("station") in by_station:
                lines.setdefault(m["station"], []).append((m["t"], m["line"]))
    rows_by_date = {}
    for s in sorted(dec):
        e = events[s]
        tz = tzs[e["station"]]
        date = datetime.date.fromisoformat(e["date"])
        for h, ts in sorted(dec[s].items(), reverse=True):
            ft, mx = fc.get((s, h), (None, {}))
            seen = [ln for t, ln in lines.get(e["station"], []) if t <= ts]
            obs, nobs = day_max(seen, tz, date, now=ts) if seen else (None, 0)
            for b in e["markets"]:
                k = book.get((s, b["label"], h)) or {}
                rows_by_date.setdefault(e["date"], []).append(
                    [s, e["city"], e["station"], e["unit"], e["date"], h, int(ts), b["label"],
                     "" if b.get("lo") is None else b["lo"], "" if b.get("hi") is None else b["hi"],
                     k.get("bid", ""), k.get("ask", ""), k.get("bid_sz", ""), k.get("ask_sz", ""),
                     "" if ft is None else int(ft)] + ["" if mx.get(m) is None else mx[m] for m in MODELS] +
                    ["" if obs is None else obs, nobs, 1 if b.get("won") else 0])
    os.makedirs(out, exist_ok=True)
    for date, rows in rows_by_date.items():
        path = os.path.join(out, f"extract-{date}.csv.gz")
        buf = io.StringIO()
        wr = csv.writer(buf, lineterminator="\n")
        wr.writerow(COLS)
        wr.writerows(rows)
        text = buf.getvalue()
        try:
            with gzip.open(path, "rt") as f:
                if f.read() == text:
                    continue                                         # unchanged: keep the file (and its git blob)
        except (OSError, EOFError):
            pass
        with gzip.open(path + ".tmp", "wt") as fo:
            fo.write(text)
        os.replace(path + ".tmp", path)
    return {d: len(r) for d, r in rows_by_date.items()}

if __name__ == "__main__":
    got = extract(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 14)
    print("weather extract:", {d: n for d, n in sorted(got.items())})
