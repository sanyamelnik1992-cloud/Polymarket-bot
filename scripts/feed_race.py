"""Analyse the price-source race recordings (reports/87): which source shows a move first, by how many ms, when the
Polymarket makers react. Reads <data>/YYYYmmdd-HH.csv[.gz] (scripts/record_feeds.py) one hour at a time, caches the events
of finished hours in <data>/events/, writes <out>/summary.txt and <out>/events.csv.gz. Run hourly by deploy/push_reports.sh
(nice 19; never fails the report).

    python scripts/feed_race.py data/feeds <out dir>
"""
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd  # noqa: E402

from pmbot.feedrace import analyse, feed_stats, read, summary  # noqa: E402


def hour_of(path):
    return os.path.basename(path).split(".")[0]


def main(data, out):
    files = sorted(glob.glob(os.path.join(data, "*.csv")) + glob.glob(os.path.join(data, "*.csv.gz")), key=hour_of)
    if not files:
        print("no recordings")
        return 1
    cache = os.path.join(data, "events")
    os.makedirs(cache, exist_ok=True)
    parts, stats, first, last = [], {}, None, None
    for i, f in enumerate(files):
        h = hour_of(f)
        c = os.path.join(cache, h + ".csv")
        done = f.endswith(".gz")
        if done and os.path.exists(c) and os.path.getmtime(c) >= os.path.getmtime(f):
            try:
                ev = pd.read_csv(c)
            except pd.errors.EmptyDataError:
                ev = pd.DataFrame()
            df = None
        else:
            df = read(f)
            ev = analyse(df)
            if done:
                ev.to_csv(c, index=False)
        if i == len(files) - 1:              # feed health from the newest hour only (cheap)
            df = df if df is not None else read(f)
            stats = feed_stats(df)
            last = float(df.rx.max()) if len(df) else None
        if first is None:
            if df is None:
                df = read(f)
            first = float(df.rx.min()) if len(df) else None
        if not ev.empty:
            parts.append(ev)
    ev = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    os.makedirs(out, exist_ok=True)
    text = summary(ev, stats, first or 0.0, last or 0.0)
    with open(os.path.join(out, "summary.txt"), "w") as fo:
        fo.write(text + "\n")
    if not ev.empty:
        ev.round(1).to_csv(os.path.join(out, "events.csv.gz"), index=False, compression={"method": "gzip", "mtime": 0})
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
