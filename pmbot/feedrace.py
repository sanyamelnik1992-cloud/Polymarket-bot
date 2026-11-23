"""Analysis of the price-source race recordings (reports/87, scripts/record_feeds.py -> scripts/feed_race.py).

A move = the mid of one source moved >= JUMP_BP[asset] within WINDOW_MS (first update past the threshold, then that source
is quiet for REFRACTORY_MS). Moves of the same asset and direction on several sources within HORIZON_MS of the earliest
one form an event; an event counts when >= MIN_SOURCES sources saw it and no loop stall of the recorder touched it. For
every event: the source that showed it first (by OUR receive time: what matters for a bot on this server), how far each
other source was behind, and when the Polymarket Up book of that coin's 5m window moved the same way (the makers'
reaction). A taker that reads source X can still hit a stale Polymarket quote only if its order is matched before the
quote moves: X's arrival + ~15 ms to the exchange + 150 ms taker delay + ~20 ms (the book feed reaches us ~20 ms after the
exchange) < the Polymarket move as we see it, i.e. pm - X > TAKER_GAP_MS.
"""
import math

import numpy as np
import pandas as pd

WINDOW_MS = 300.0
REFRACTORY_MS = 3000.0
HORIZON_MS = 1500.0
GAP_MS = 2000.0
MIN_SOURCES = 3
JUMP_BP = {"btc": 2.0, "eth": 2.5, "sol": 4.0, "xrp": 4.0}
PM_REACT = 0.01                    # the Up mid must settle >= 1 c the event's way to count as the makers' reaction
PM_HOLD_MS = 300.0                 # ... and stay past half-way that long (the top of the book flickers)
TAKER_GAP_MS = 185.0
REF = "bn"                         # the bots' signal today: Binance spot
NON_PRICE = ("pm", "cl", "lag", "sys")
COLS = ["rx", "src", "asset", "bid", "ask", "ts", "extra"]


def read(path):
    """One hour file (.csv or .csv.gz) -> DataFrame with COLS (a truncated last line of a live file is dropped)."""
    df = pd.read_csv(path, names=COLS, header=None, dtype={"src": str, "asset": str, "extra": str},
                     on_bad_lines="skip", engine="c")
    df["rx"] = pd.to_numeric(df["rx"], errors="coerce")
    for c in ("bid", "ask", "ts"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["rx"])


def stalls(df):
    """Recorder loop stalls as (start, end) ms: receive times inside them are late."""
    s = df[df.src == "lag"]
    return [(r, r + 10.0 + b) for r, b in zip(s.rx.to_numpy(), s.bid.to_numpy()) if b == b]


def jumps(t, mid, thr_bp, window_ms=WINDOW_MS, refractory_ms=REFRACTORY_MS):
    """[(t, sign, bp)]: first update where the mid is >= thr_bp away from its value window_ms earlier."""
    if len(t) < 2:
        return []
    j = np.searchsorted(t, t - window_ms, side="right") - 1
    ok = j >= 0
    r = np.zeros(len(t))
    r[ok] = np.log(mid[ok] / mid[j[ok]]) * 1e4
    out, last = [], -math.inf
    for i in np.nonzero(np.abs(r) >= thr_bp)[0]:
        if t[i] - last >= refractory_ms:
            out.append((float(t[i]), 1 if r[i] > 0 else -1, float(r[i])))
            last = t[i]
    return out


def events(js_by_src, horizon_ms=HORIZON_MS, gap_ms=GAP_MS):
    """Group the moves of one asset into events: [(t0, sign, {src: t})], t0 = the earliest source's time."""
    allj = sorted((t, s, src) for src, js in js_by_src.items() for t, s, _ in js)
    used, out, last = set(), [], -math.inf
    for i, (t, s, src) in enumerate(allj):
        if i in used or t - last < gap_ms:
            continue
        ev = {src: t}
        used.add(i)
        for k in range(i + 1, len(allj)):
            t2, s2, src2 = allj[k]
            if t2 - t > horizon_ms:
                break
            if s2 == s and src2 not in ev and k not in used:
                ev[src2] = t2
                used.add(k)
        out.append((t, s, ev))
        last = t
    return out


def pm_reaction(pm, t0, sign):
    """ms from t0 until the Polymarket Up mid of the running window settled half-way to its new level, None without a clear
    move. The book top flickers (BTC: ~100 changes a second), so a single tick is no reaction: the level before is the median
    mid of [t0 - 1.5 s, t0 - 0.2 s], the level after the median of [t0 + 1.5 s, t0 + 3 s]; it must have moved >= PM_REACT the
    event's way, and the time is the first update past half-way that stays past it for PM_HOLD_MS. pm = (rx, mid, window start)
    arrays of one asset, sorted by rx. Negative = the book moved before our first source showed the move."""
    rx, mid, start = pm
    if len(rx) == 0:
        return None
    i0 = np.searchsorted(rx, t0, side="right") - 1
    if i0 < 0:
        return None
    w = start[i0]
    same = start == w
    pre = same & (rx >= t0 - 1500) & (rx <= t0 - 200)
    post = same & (rx >= t0 + 1500) & (rx <= t0 + 3000)
    if not pre.any() or not post.any():
        return None
    ref, fin = float(np.median(mid[pre])), float(np.median(mid[post]))
    if (fin - ref) * sign < PM_REACT - 1e-9:
        return None
    half = ref + (fin - ref) / 2
    lo = np.searchsorted(rx, t0 - 1000, side="left")
    hi = np.searchsorted(rx, t0 + 3000, side="right")
    for i in range(lo, hi):
        if not same[i] or (mid[i] - half) * sign < 0:
            continue
        j = i + 1
        while j < hi and rx[j] <= rx[i] + PM_HOLD_MS and (mid[j] - half) * sign >= 0:
            j += 1
        if j >= hi or rx[j] > rx[i] + PM_HOLD_MS:
            return float(rx[i] - t0)
    return None


def analyse(df):
    """One recording chunk -> DataFrame of events: t0, asset, sign, n, first, stall, d_<src> (ms after t0), pm (ms after t0)."""
    st = stalls(df)
    st_starts = np.array([a for a, _ in st]) if st else np.array([])
    st_ends = np.array([b for _, b in st]) if st else np.array([])
    rows = []
    price = df[~df.src.isin(NON_PRICE)]
    pmdf = df[df.src == "pm"]
    for asset, g in price.groupby("asset"):
        thr = JUMP_BP.get(asset)
        if thr is None:
            continue
        js = {}
        for src, h in g.groupby("src"):
            h = h.sort_values("rx")
            mid = ((h.bid + h.ask) / 2).to_numpy()
            js[src] = jumps(h.rx.to_numpy(), mid, thr)
        p = pmdf[pmdf.asset == asset].sort_values("rx")
        pm = (p.rx.to_numpy(), ((p.bid + p.ask) / 2).to_numpy(), p.extra.to_numpy())
        for t0, s, ev in events(js):
            near = bool(len(st_starts)) and bool(np.any((st_starts <= t0 + HORIZON_MS) & (st_ends >= t0 - 50)))
            r = {"t0": t0, "asset": asset, "sign": s, "n": len(ev), "first": min(ev, key=ev.get), "stall": near,
                 "pm": pm_reaction(pm, t0, s)}
            for src, t in ev.items():
                r["d_" + src] = t - t0
            rows.append(r)
    return pd.DataFrame(rows)


def feed_stats(df):
    """Per source: rows per minute per asset, median and p90 of (our receive - venue timestamp) where the venue sends one,
    reconnects."""
    out = {}
    span_min = max((df.rx.max() - df.rx.min()) / 60000.0, 1e-9) if len(df) else 1.0
    for src, g in df[~df.src.isin(("lag", "sys"))].groupby("src"):
        lat = (g.rx - g.ts).dropna()
        out[src] = {"rows_min": len(g) / span_min / max(g.asset.nunique(), 1),
                    "lat_med": float(lat.median()) if len(lat) else None,
                    "lat_p90": float(lat.quantile(0.9)) if len(lat) else None}
    sysr = df[df.src == "sys"]
    for name, g in sysr.groupby("asset"):
        if name in ("start", "stop"):
            continue
        out.setdefault(name, {})["connects"] = int((g.extra.astype(str) == "connect").sum())
    return out


def summary(ev, stats, first_ms, last_ms):
    """Plain-text report of the events (owner-facing numbers, English labels like the other server reports)."""
    hours = (last_ms - first_ms) / 3.6e6 if last_ms > first_ms else 0.0
    t = lambda ms: pd.Timestamp(ms, unit="ms").strftime("%Y-%m-%d %H:%M")  # noqa: E731
    lines = [f"price-source race {t(first_ms)} - {t(last_ms)} UTC ({hours:.1f} h); moves >= {JUMP_BP} bp within "
             f"{WINDOW_MS:.0f} ms seen by >= {MIN_SOURCES} sources, no recorder stall",
             "", "feeds: rows/min per asset, venue -> us ms (median / p90, venue clock), connects"]
    for src in sorted(stats):
        s = stats[src]
        lat = f"{s['lat_med']:7.1f} / {s['lat_p90']:7.1f}" if s.get("lat_med") is not None else "      - /       -"
        lines.append(f"  {src:8s} {s.get('rows_min', 0):8.1f}   {lat}   {s.get('connects', 0)}")
    if ev is None or ev.empty:
        lines.append("\nno events yet")
        return "\n".join(lines)
    good = ev[(ev.n >= MIN_SOURCES) & (~ev.stall)]
    srcs = sorted({c[2:] for c in ev.columns if c.startswith("d_")})
    for asset, g in good.groupby("asset"):
        lines.append(f"\n{asset}: {len(g)} events ({int((g.sign > 0).sum())} up; the Polymarket book settled >= 1 c the same "
                     f"way in {int(g.pm.notna().sum())})")
        lines.append(f"  {'src':8s} {'seen':>5s} {'first':>6s} {'behind 1st':>10s}   {'ahead of ' + REF + ' (med, >=50, >=100 ms)':>34s}"
                     f"   {'beats makers':>12s}")
        for src in srcs:
            col = "d_" + src
            if col not in g:
                continue
            seen = g[col].notna()
            if not seen.any():
                continue
            first = (g["first"] == src).mean() * 100
            behind = g.loc[seen, col].median()
            ahead = ""
            if "d_" + REF in g and src != REF:
                both = seen & g["d_" + REF].notna()
                if both.any():
                    lead = g.loc[both, "d_" + REF] - g.loc[both, col]
                    ahead = (f"{lead.median():+8.0f} ms {100 * (lead >= 50).mean():5.0f} % {100 * (lead >= 100).mean():5.0f} %"
                             f"  (n {int(both.sum())})")
            pmok = seen & g.pm.notna()
            beat = ""
            if pmok.any():
                gap = g.loc[pmok, "pm"] - g.loc[pmok, col]
                beat = f"{100 * (gap > TAKER_GAP_MS).mean():5.0f} % of {int(pmok.sum())}"
            lines.append(f"  {src:8s} {100 * seen.mean():4.0f}% {first:5.0f}% {behind:8.0f} ms   {ahead:>34s}   {beat:>12s}")
        pm = g.pm.dropna()
        if len(pm):
            lines.append(f"  polymarket Up book half-way to its new level {pm.median():.0f} ms after the first source (median; p25 "
                         f"{pm.quantile(0.25):.0f}, p75 {pm.quantile(0.75):.0f}); before the first source in {100 * (pm < 0).mean():.0f} %")
    lines.append(f"\nbeats makers = the share of events where an order sent on that source's update could still be matched "
                 f"before the Polymarket quote moved (pm - src > {TAKER_GAP_MS:.0f} ms: ~15 ms to the exchange + 150 ms "
                 f"taker delay + ~20 ms book feed); faster takers are not counted.")
    return "\n".join(lines)
