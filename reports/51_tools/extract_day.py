"""extract_tail.py for a stretch of recordings: plain or .gz clob.jsonl files, lines with rx >= START_MS.
Writes OUT_tob.csv (top of book, 2 levels per side, 100 ms buckets, deduped) and OUT_trades.csv for the late part of
each window (rel in [LO, HI] s). Lines before START are skipped by a regex on the line head (no json parse).
Book state is rebuilt per file (each recording starts with fresh snapshots).

usage: python extract_day.py OUT START_MS LO HI clob1 [clob2 ...]
"""
import gzip
import json
import re
import sys

outp, START_MS, LO, HI = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
inputs = sys.argv[5:]
RX = re.compile(r'"rx":(\d+)')
fo = open(outp + "_tob.csv", "w")
fo.write("rx,k,bid,bid_sz,bid2,bid2_sz,ask,ask_sz,ask2,ask2_sz\n")
ft = open(outp + "_trades.csv", "w")
ft.write("rx,ts,k,price,size,side\n")
wsof = {}


def ws_of(k):
    w = wsof.get(k)
    if w is None:
        try:
            w = int(k.split("-")[1])
        except Exception:
            w = -1
        wsof[k] = w
    return w


for path in inputs:
    books, last_written, pending = {}, {}, {}

    def top(k):
        b, a = books[k]
        if b:
            bp = max(b)
            bs = b[bp]
            rest = [p for p in b if p != bp]
            b2 = max(rest) if rest else ""
            b2s = b[b2] if rest else ""
        else:
            bp = bs = b2 = b2s = ""
        if a:
            ap = min(a)
            as_ = a[ap]
            rest = [p for p in a if p != ap]
            a2 = min(rest) if rest else ""
            a2s = a[a2] if rest else ""
        else:
            ap = as_ = a2 = a2s = ""
        return (bp, bs, b2, b2s, ap, as_, a2, a2s)

    def flush(k):
        pb = pending.pop(k, None)
        if pb is None:
            return
        _, rx, row = pb
        if last_written.get(k) != row:
            last_written[k] = row
            fo.write(f"{rx},{k}," + ",".join(str(x) for x in row) + "\n")

    def mark(k, rx):
        w = ws_of(k)
        if w < 0:
            return
        rel = rx / 1000.0 - w
        if rel < LO or rel > HI:
            return
        bucket = rx // 100
        pb = pending.get(k)
        if pb is not None and pb[0] != bucket:
            flush(k)
        pending[k] = (bucket, rx, top(k))

    n = 0
    started = False
    f = gzip.open(path, "rt") if path.endswith(".gz") else open(path)
    print("file", path, flush=True)
    for line in f:
        n += 1
        if n % 5000000 == 0:
            print("lines", n, "started", started, flush=True)
        if not started:
            m = RX.search(line[:120])
            if not m or int(m.group(1)) < START_MS:
                continue
            started = True
        try:
            ev = json.loads(line)
        except Exception:
            continue
        e = ev.get("e")
        rx = ev.get("rx", 0)
        if e == "pc":
            touched = set()
            for c in ev["c"]:
                k, p, s, side = c[0], c[1], c[2], c[3]
                bk = books.get(k)
                if bk is None:
                    bk = books[k] = ({}, {})
                book = bk[0] if side == "B" else bk[1]
                if s <= 0:
                    book.pop(p, None)
                else:
                    book[p] = s
                touched.add(k)
            for k in touched:
                mark(k, rx)
        elif e == "book":
            k = ev["k"]
            books[k] = ({p: s for p, s in ev["b"] if s > 0}, {p: s for p, s in ev["a"] if s > 0})
            mark(k, rx)
        elif e == "trade":
            k = ev["k"]
            w = ws_of(k)
            if w >= 0 and LO <= rx / 1000.0 - w <= HI:
                ft.write(f"{rx},{ev.get('ts', 0)},{k},{ev['p']},{ev['s']},{ev.get('side')}\n")
        elif e == "conn" and ev.get("state") == "down":
            a = ev.get("asset", "")
            for k in [k for k in books if k.startswith(a + "-")]:
                books[k] = ({}, {})
                mark(k, rx)
            fo.write(f"{rx},GAP-{a},,,,,,,,\n")
    for k in list(pending):
        flush(k)
    f.close()
    print("done", path, n, flush=True)
fo.close()
ft.close()
print("all done")
