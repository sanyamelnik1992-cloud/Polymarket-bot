"""Summarise a bot run from its journal (paper or live).

usage: python scripts/report_journal.py JOURNAL_RUN_DIR
"""
import json
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

pd.set_option("display.width", 200)


def lock_orders(ev):
    """Taker orders of a lock bot: how many found their ask, how long the exchange took, why the others failed.
    (Until 9 Nov a rejected FAK was an `order_error` without details: 26 of 42 live orders, reports/52.)"""
    ent = [e for e in ev if e["type"] == "lock_entry" and not e.get("maker")]
    miss = [e for e in ev if e["type"] == "lock_miss"]
    err = [e for e in ev if e["type"] in ("order_error", "order_failed", "order_pending")]
    rej = [e for e in ev if e["type"] == "order_rejected"]
    if not (ent or miss or err or rej):
        return
    clean = [e for e in miss if not e.get("error")]
    print(f"\nlock orders: sent {len(ent) + len(miss)}  filled {len(ent)}  ask gone {len(clean)}  "
          f"errors {len(miss) - len(clean)}")
    ms = sorted(e["ms"] for e in ent + clean if e.get("ms") is not None)
    if ms:
        print(f"  signal -> exchange answer, ms: median {ms[len(ms) // 2]:.0f}  p90 {ms[min(len(ms) - 1, int(0.9 * len(ms)))]:.0f}  "
              f"max {ms[-1]:.0f}")
    call = sorted(e["latency_ms"] for e in ev if e["type"] in ("order_filled", "order_rejected") and e.get("latency_ms") is not None)
    if call:                       # the exchange call alone: the rest of "signal -> answer" is spent inside our process
        print(f"  exchange call alone, ms: median {call[len(call) // 2]:.0f}  p90 {call[min(len(call) - 1, int(0.9 * len(call)))]:.0f}  "
              f"max {call[-1]:.0f}")
    q = sorted(e["queue_ms"] for e in ent + clean if e.get("queue_ms") is not None)
    if q:                          # decision -> the order task running (our own event loop)
        print(f"  decision -> order sent, ms: median {q[len(q) // 2]:.0f}  p90 {q[min(len(q) - 1, int(0.9 * len(q)))]:.0f}  max {q[-1]:.0f}")
    gcm = [e["stats"].get("gc_max_ms") for e in ev if e["type"] == "status" and isinstance(e.get("stats"), dict)]
    gcm = sorted(x for x in gcm if x is not None)
    if gcm:                        # the longest garbage-collector pause of each 30 s status
        print(f"  GC pause per 30 s, ms: median {gcm[len(gcm) // 2]:.0f}  p90 {gcm[min(len(gcm) - 1, int(0.9 * len(gcm)))]:.0f}  "
              f"max {gcm[-1]:.0f}")
    age = sorted(e["sig_age_ms"] for e in ent + clean if e.get("sig_age_ms") is not None)
    if age:                        # fair bots: how old the Binance tick was when the bot decided
        print(f"  signal age at the decision, ms: median {age[len(age) // 2]:.0f}  p90 {age[min(len(age) - 1, int(0.9 * len(age)))]:.0f}")
    warm = sorted(e["ms"] for e in ev if e["type"] == "prewarm" and e.get("ms") is not None)
    if warm:
        print(f"  market prewarm (2 lookups + signing), ms: median {warm[len(warm) // 2]:.0f}  max {warm[-1]:.0f}  n {len(warm)}")
    if ent:
        slip = [100 * (e["price"] - e["ask"]) for e in ent if e.get("ask") is not None and e.get("price") is not None]
        if slip:
            print(f"  fill price minus the ask we saw, cents: mean {sum(slip) / len(slip):+.2f}  worst {max(slip):+.1f}")
    if clean:
        left = [300 - e["rel"] for e in clean if e.get("rel") is not None]
        if left:
            print(f"  misses came {min(left):.0f}-{max(left):.0f} s before the end; model p {min(e.get('p', 0) for e in clean):.3f}-"
                  f"{max(e.get('p', 0) for e in clean):.3f}")
    codes = Counter(e.get("code", "?") for e in rej)
    if codes:
        print(f"  exchange rejections by code: {dict(codes)}")
    for e in err[-5:]:
        txt = e.get("error") or e.get("status") or ""
        print(f"  {e['type']} {e.get('key', '')}: {e.get('err', '')} {str(txt)[:160]}")


def fair_breakdown(ev):
    """Fair-value bots (reports/83 section 14): orders and entries by edge, ask size and signal age, with the outcome of every
    entry from its window's settle - which signals our speed still gets, and whether those earn."""
    ent = [e for e in ev if e["type"] == "lock_entry" and e.get("fair")]
    miss = [e for e in ev if e["type"] == "lock_miss" and e.get("fair") and not e.get("error")]
    if not ent and not miss:
        return
    win = {e["key"]: e.get("winner") for e in ev if e["type"] == "settle"}

    def bucket(x, edges):
        if x is None:
            return "?"
        for lo, hi in zip(edges, edges[1:]):
            if lo <= x < hi:
                return f"{lo:g}-{hi:g}"
        return f">={edges[-1]:g}"

    for name, field_, edges in (("edge, c", "edge", (0.15, 0.20, 0.25, 0.35)), ("ask size, shares", "ask_sz", (0, 10, 50, 200)),
                                ("signal age, ms", "sig_age_ms", (0, 100, 300, 1000)),
                                ("what woke the decision (section 17)", "wake", None)):
        rows = {}
        for e in ent + miss:
            x = e.get(field_)
            if edges is None:      # a category: timer / due / move / level / book
                k = str(x) if x is not None else "?"
            else:
                k = bucket(x * 100 if (field_ == "edge" and x is not None) else x,
                           tuple(v * 100 for v in edges) if field_ == "edge" else edges)
            r = rows.setdefault(k, {"orders": 0, "filled": 0, "settled": 0, "won": 0, "exp": 0.0, "pnl": 0.0})
            r["orders"] += 1
            if e["type"] == "lock_entry":
                r["filled"] += 1
                w = win.get(e["key"])
                if w and e.get("price") is not None:
                    won = (e["side"] == "U") == (w == "Up")
                    sh, px = float(e.get("shares") or 0), float(e["price"])
                    r["settled"] += 1
                    r["won"] += won
                    r["exp"] += px
                    r["pnl"] += sh * ((1.0 if won else 0.0) - px - 0.07 * px * (1 - px))
        if edges is None and set(rows) == {"?"}:
            continue               # journals from before the field existed
        print(f"\nfair entries by {name}: orders / filled / settled / won (expected at the paid prices) / pnl")
        for k in sorted(rows, key=lambda k: (k == "?", (float(k.split("-")[0].lstrip(">=")) if edges is not None else 0)
                                             if k != "?" else 0, k)):
            r = rows[k]
            print(f"  {k:>10s}: {r['orders']:4d} / {r['filled']:4d} ({100 * r['filled'] / max(r['orders'], 1):3.0f} %) / {r['settled']:4d} / "
                  f"{r['won']:4d} ({r['exp']:6.1f}) / {r['pnl']:+8.2f}")


def race_report(ev):
    """Fair bots (reports/83 section 17): the race for the asks we went for (lock_race). Share of the orders whose ask was still
    in our view of the book at each delay after the decision (paper: the fill rate a faster or slower order would get), and for
    the misses whether a public trade at or under our limit came before our answer (another taker was faster) or the ask went
    without one (its maker pulled it)."""
    rs = [e for e in ev if e["type"] == "lock_race" and e.get("avail")]
    if not rs:
        return
    offs = rs[0]["offs"]
    print(f"\nrace for the ask (reports/83 s. 17): orders whose ask is still in our book this long after the decision")
    print("  " + "".join(f"{o:>6d}" for o in offs) + "  ms")
    groups = (("all", rs), ("filled", [r for r in rs if (r.get("got") or 0) > 0]),
              ("missed", [r for r in rs if r.get("got") == 0 and not r.get("error")]))
    for name, grp in groups:
        if grp:
            share = [100.0 * sum(1 for r in grp if len(r["avail"]) > i and r["avail"][i] > 0) / len(grp) for i in range(len(offs))]
            print("  " + "".join(f"{x:5.0f}%" for x in share) + f"  {name} ({len(grp)})")
    miss = groups[2][1]
    if miss:
        firsts = []
        for r in miss:
            ans = r.get("ans_ms") or 0
            ts = [t[0] for t in r.get("trades", []) if t[2] == "m" and -200 <= t[0] <= ans]
            if ts:
                firsts.append(min(ts))
        firsts.sort()
        med = f", the first of them {firsts[len(firsts) // 2]} ms after our decision (median)" if firsts else ""
        print(f"  misses: {len(firsts)} of {len(miss)} had a public trade at or under our limit before our answer = another "
              f"taker was faster{med}; {len(miss) - len(firsts)} without one = the ask was pulled")
    ans = sorted(r["ans_ms"] for r in rs if r.get("ans_ms") is not None)
    if ans:
        print(f"  decision -> answer, ms: median {ans[len(ans) // 2]}  (paper: paper_exec_ms)")


def shock_breakdown(ev, fixed_bp=2.5):
    """Lock entries by the Binance jump of the last seconds (reports/88): the Sniper with the volatility pause enters up to
    max(2.5 bp, 3 sigma * sqrt(3)); entries above the fixed 2.5 bp are the ones only that pause allows."""
    ent = [e for e in ev if e["type"] == "lock_entry" and e.get("shock_bp") is not None]
    if not ent:
        return
    pnl = {e["key"]: e.get("pnl", 0.0) for e in ev if e["type"] == "settle"}
    print(f"\nentries by the Binance jump before them (fixed pause {fixed_bp} bp): entries / settled / lost / pnl")
    for name, sel in ((f"<= {fixed_bp} bp (the fixed pause allows too)", lambda x: x <= fixed_bp),
                      (f"> {fixed_bp} bp (only the volatility pause)", lambda x: x > fixed_bp)):
        es = [e for e in ent if sel(float(e["shock_bp"]))]
        done = [pnl[e["key"]] for e in es if e["key"] in pnl]
        print(f"  {name:45s}: {len(es):4d} / {len(done):4d} / {sum(1 for x in done if x < 0):3d} / {sum(done):+8.2f}")


MISS_ORDER = (("max_open", "сигнал был, но уже открыт вход (одно окно за раз)"),
              ("fak_miss", "заявка ушла, продажи уже не было"),
              ("guard", "фильтр Binance (движение против нас / скачок / пауза)"),
              ("no_seller", "модель уверена, но продавца по нашей цене нет"),
              ("day_stop", "дневной стоп / файл STOP"))


def missed_report(ev):
    """Lock bots (reports/90): windows the model liked that ended without an entry, by what stopped them. A window counts once,
    by the strongest reason: an entry, else a blocked signal (max_open), a missed FAK, a Binance guard, no seller at our
    price (the model at or above theta), the day stop."""
    states = [e for e in ev if e["type"] == "lock_state"]
    if not states or any(e["type"] == "mm_quote" for e in ev):
        return
    start = next((e for e in ev if e["type"] == "start"), {}) or {}
    theta = ((start.get("config") or {}).get("lock") or {}).get("theta", 0.9)
    entered = {e["key"] for e in ev if e["type"] == "lock_entry"}
    fak = {e["key"] for e in ev if e["type"] == "lock_miss" and not e.get("error")}
    why = {}
    for e in states:
        why.setdefault(e["key"], []).append(e)
    cls = Counter()
    blocked = []
    for key, es in why.items():
        if key in entered:
            cls["entered"] += 1
            continue
        ws = [e.get("why") or "" for e in es]
        if "risk:max_open" in ws:
            cls["max_open"] += 1
            b = next(e for e in es if e.get("why") == "risk:max_open")
            blocked.append((key, b.get("side"), b.get("p"), b.get("ask")))
        elif key in fak:
            cls["fak_miss"] += 1
        elif any(w in ("binance_against", "binance_jump", "shock") for w in ws):
            cls["guard"] += 1
        elif any(w == "ask_too_high" and (e.get("p") or 0) >= theta - 1e-9 for w, e in zip(ws, es)):
            cls["no_seller"] += 1
        elif any(w.startswith("risk:") for w in ws):
            cls["day_stop"] += 1
    if not sum(cls.values()):
        return
    print(f"\nокна, где модель была уверена (p >= {theta}): вход {cls['entered']}; без входа:")
    for k, name in MISS_ORDER:
        if cls[k]:
            print(f"  {cls[k]:4d}  {name}")
    for key, side, p_, ask in blocked[-10:]:
        print(f"    max_open: {key} {side} p {p_} ask {ask}")


def maker_report(ev):
    """Paper maker (reports/89): quotes, fills by our edge at the fill (fair value - price), the side's fair value 5 / 30 s later
    (markout: > 0 = the fill was not picked off), the outcome per share and the windows."""
    fills = [e for e in ev if e["type"] == "mm_fill"]
    quotes = [e for e in ev if e["type"] == "mm_quote"]
    if not quotes and not fills:
        return
    win = {e["key"]: e for e in ev if e["type"] == "settle"}
    mk = {}
    for e in ev:
        if e["type"] == "mm_markout" and e.get("d") is not None:
            mk.setdefault((e["key"], e["side"], e["price"], e["h"]), []).append(e["d"])
    why = Counter(e["why"] for e in ev if e["type"] == "mm_cancel")
    sh = sum(e["size"] for e in fills)
    print(f"\nmaker: quotes {len(quotes)} (moved {sum(1 for e in quotes if e.get('re'))}), pulls on a Binance jump "
          f"{sum(1 for e in ev if e['type'] == 'mm_pull')}, cancels {dict(why.most_common(6))}")
    print(f"  fills {len(fills)}: {sh:.0f} shares, ${sum(e['size'] * e['price'] for e in fills):.2f}")
    if not fills:
        return

    def row(name, fs):
        n = sum(f["size"] for f in fs)
        if not n:
            return
        edge = sum((f.get("edge") or 0) * f["size"] for f in fs) / n
        out = [name, f"{len(fs):5d}", f"{n:7.0f}", f"{100 * edge:+6.2f}"]
        for h in (5, 30):
            xs = [(mk[(f["key"], f["side"], f["price"], h)][0], f["size"]) for f in fs if (f["key"], f["side"], f["price"], h) in mk]
            w = sum(q for _, q in xs)
            out.append(f"{100 * sum(d * q for d, q in xs) / w:+6.2f}" if w else "     .")
        done = [f for f in fs if f["key"] in win]
        w = sum(f["size"] for f in done)
        res = sum(((1.0 if win[f["key"]]["winner"][0] == f["side"] else 0.0) - f["price"]) * f["size"] for f in done)
        out.append(f"{100 * res / w:+7.2f} ({w:.0f})" if w else "      .")
        print("  " + "  ".join(out))

    hdr = "fills  shares  edge c  mk5 c  mk30 c  by outcome c/share (settled shares)"
    print(f"  by our edge at the fill (fair - price):     {hdr}")
    for lo, hi in ((-1, 0), (0, 0.01), (0.01, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1)):
        row(f"{100 * lo:+4.0f}..{100 * hi:+4.0f} c" + " " * 30, [f for f in fills if f.get("edge") is not None and lo <= f["edge"] < hi])
    print(f"  by the Binance move of the 2 s before (bp): {hdr}")
    for name, sel in (("against us (< -0.5)", lambda v: v < -0.5), ("flat", lambda v: -0.5 <= v <= 0.5),
                      ("toward us (> 0.5)", lambda v: v > 0.5)):
        row(f"{name:42s}", [f for f in fills if f.get("bn2") is not None and sel(f["bn2"])])
    row(f"{'all':42s}", fills)
    ws = [e for e in win.values()]
    if ws:
        pnl = [e["pnl"] for e in ws]
        paired = sum(min(e["up"], e["down"]) for e in ws)
        unp = sum(abs(e["up"] - e["down"]) for e in ws)
        print(f"  windows settled {len(ws)}: pnl {sum(pnl):+.2f} (won {sum(1 for x in pnl if x > 0)}, lost "
              f"{sum(1 for x in pnl if x < 0)}, worst {min(pnl):+.2f}, best {max(pnl):+.2f}); shares paired {paired:.0f}, "
              f"unpaired {unp:.0f}")


def maker_live_report(ev):
    """Live maker test (reports/94): what the exchange did with our bids. Paper cannot show these numbers: the share of our
    orders that were filled at all, the place / cancel latency, refusals, the fills found only in the exchange's record,
    and the maker rebate the exchange credited for the day."""
    placed = [e for e in ev if e["type"] == "order_placed"]
    if not placed or not any(e["type"] == "mm_quote" for e in ev):
        return
    execs = [e for e in ev if e["type"] == "mm_exec"]
    filled = {e["order_id"] for e in execs}
    rej = Counter(e.get("code") for e in ev if e["type"] == "order_rejected")
    refused = Counter(e.get("why") for e in ev if e["type"] == "mm_refused")

    def q(xs, f):
        xs = sorted(x for x in xs if x is not None)
        return xs[min(len(xs) - 1, int(f * len(xs)))] if xs else None

    pl = [e.get("latency_ms") for e in placed]
    cl = [e.get("latency_ms") for e in ev if e["type"] == "order_cancelled"]
    print(f"\nmaker LIVE: orders placed {len(placed)}, filled at least partly {len(filled)} "
          f"({100 * len(filled) / len(placed):.1f} %), shares {sum(e['size'] for e in execs):.0f}")
    print(f"  place ms median {q(pl, 0.5)} p90 {q(pl, 0.9)}; cancel ms median {q(cl, 0.5)} p90 {q(cl, 0.9)}")
    print(f"  refused by the exchange {dict(rej)}; by our own limits (records, 1 per 10 s) {dict(refused)}")
    late = sum(1 for e in ev if e["type"] == "order_cancelled" and e.get("not_canceled"))
    src = Counter("adopted" if e.get("adopted") else ("record" if e.get("status") == "RECONCILED" else "stream") for e in execs)
    print(f"  cancels that came too late (already matched) {late}; fills from {dict(src)}; failed trades "
          f"{sum(1 for e in ev if e['type'] == 'fill_failed')}; sweeps {sum(1 for e in ev if e['type'] == 'mm_sweep')}")
    reb = [e for e in ev if e["type"] == "mm_rebate" and e.get("usd") is not None]
    if reb:
        print(f"  maker rebate of {reb[-1].get('day')} (the exchange's number, our address): ${reb[-1]['usd']:.4f}")
    errs = Counter(e.get("where") for e in ev if e["type"] == "mm_live_error")
    if errs:
        print(f"  errors {dict(errs)}")


def main(d):
    ev = [json.loads(line) for line in open(os.path.join(d, "events.jsonl")) if line.strip()]
    kinds = Counter(e["type"] for e in ev)
    start = next((e for e in ev if e["type"] == "start"), None)
    t0, t1 = ev[0]["t"], ev[-1]["t"]
    print(f"run: {d}\nmode: {start and start.get('mode')}  duration: {(t1 - t0) / 60:.1f} min  events: {dict(kinds)}")
    st = pd.DataFrame([e for e in ev if e["type"] == "settle"])
    fills = pd.DataFrame([e for e in ev if e["type"] == "fill"])
    quotes = pd.DataFrame([e for e in ev if e["type"] == "quote"])
    filt = pd.DataFrame([e for e in ev if e["type"] == "filters"])
    if not filt.empty:
        filt["asset"] = filt.key.str.split("-").str[0]
        r = Counter(x for rs in filt.reasons for x in rs)
        print("\nfilter reasons (state changes):", dict(r.most_common()))
    if not quotes.empty:
        print(f"quotes placed: {len(quotes)}")
    lock_orders(ev)
    fair_breakdown(ev)
    race_report(ev)
    shock_breakdown(ev)
    missed_report(ev)
    maker_report(ev)
    maker_live_report(ev)
    if fills.empty:
        print("no fills")
        return
    fills["asset"] = fills.key.str.split("-").str[0]
    fills["notional"] = fills.price * fills["size"]
    fills["rel"] = fills.ts / 1000 - fills.key.str.split("-").str[1].astype(int)
    print(f"\nfills: {len(fills)}  shares: {fills['size'].sum():.0f}  notional: ${fills.notional.sum():.2f}")
    print(fills.groupby("asset").agg(fills=("size", "size"), shares=("size", "sum"), notional=("notional", "sum"),
                                     avg_price=("price", "mean"), med_rel=("rel", "median")).round(3))
    if st.empty:
        print("no settled windows yet")
        return
    st["asset"] = st.key.str.split("-").str[0]
    st["pnl_ex_rebate"] = st.pnl - st.rebate
    g = st.groupby("asset")
    summ = pd.DataFrame({
        "windows": g.size(), "cost": g.cost.sum(), "pnl": g.pnl.sum(), "pnl_ex_rebate": g.pnl_ex_rebate.sum(),
        "rebate": g.rebate.sum(), "win_rate": g.pnl.apply(lambda x: (x > 0).mean()),
        "worst": g.pnl.min(), "best": g.pnl.max()})
    summ["roi"] = summ.pnl / summ.cost
    print("\nsettled windows:")
    print(summ.round(3).to_string())
    tot = st.pnl.sum()
    tstat = st.pnl.mean() / st.pnl.std() * np.sqrt(len(st)) if len(st) > 2 and st.pnl.std() > 0 else float("nan")
    print(f"\nTOTAL pnl ${tot:+.2f} (ex-rebate ${st.pnl_ex_rebate.sum():+.2f}) on cost ${st.cost.sum():.2f} "
          f"-> ROI {tot / st.cost.sum():+.2%}; windows {len(st)}, t-stat {tstat:+.2f}")
    st = st.sort_values("t")
    eq = st.pnl.cumsum()
    print(f"max drawdown ${(eq - eq.cummax()).min():.2f}; hedged windows (both sides filled): "
          f"{((st.up > 0) & (st.down > 0)).sum()}")


if __name__ == "__main__":
    main(sys.argv[1])
