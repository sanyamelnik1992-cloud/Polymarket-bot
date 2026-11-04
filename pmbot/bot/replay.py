"""Replay recorder output through the REAL bot (Engine + filters + risk + PaperBroker) on a simulated clock.

Recorder files (pmbot/recorder.py): markets.jsonl, clob.jsonl (compact), chainlink.jsonl, binance.jsonl.
Events are merged by local receive time `rx`; the engine decides every `tick_ms` of simulated time.
Strikes follow the live rules: Chainlink TWAP estimate once >= min_strike_seconds are present, the exact
published priceToBeat from `exact_strikes` (if given) from start+90s.
"""
import asyncio
import heapq
import json

from ..config import BINANCE_SYMBOL, CHAINLINK_SYMBOL
from .engine import Engine
from .journal import Journal
from .paper import PaperBroker
from .state import Window


def _jsonl(path, kind):
    with open(path) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            rx = ev.get("rx")
            if rx is not None:
                yield (rx, kind, ev)


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


async def replay(rec_dir, cfg, journal_dir, winners: dict, exact_strikes: dict | None = None,
                 vol_seed: dict | None = None, assets=None):
    """winners: slug -> 'Up'/'Down'. Returns the engine (risk manager holds the results)."""
    assets = assets or cfg.strategy.assets
    cfg.risk.state_file = ""          # a replay never reads or writes the live risk state
    clock = Clock()
    journal = Journal(journal_dir)

    def factory(on_fill, journal):
        return PaperBroker(cfg.execution.place_latency_ms, cfg.execution.cancel_latency_ms, on_fill=on_fill,
                           journal=journal)

    eng = Engine(cfg, factory, journal, clock=clock)
    st = eng.st
    markets = {}
    with open(f"{rec_dir}/markets.jsonl") as f:
        for line in f:
            m = json.loads(line)
            if m["asset"] in assets:
                markets[f'{m["asset"]}-{m["window_start"]}'] = m
    if vol_seed:
        for a, closes in vol_seed.items():
            if a in st.prices:
                st.prices[a].seed_vol(closes)
    sym_cl = {CHAINLINK_SYMBOL[a]: a for a in assets}
    sym_bn = {BINANCE_SYMBOL[a]: a for a in assets}
    stream = heapq.merge(_jsonl(f"{rec_dir}/clob.jsonl", "c"), _jsonl(f"{rec_dir}/chainlink.jsonl", "l"),
                         _jsonl(f"{rec_dir}/binance.jsonl", "b"), key=lambda x: x[0])
    tick = cfg.execution.tick_ms
    next_tick = None
    added = set()
    for rx, kind, ev in stream:
        clock.t = rx
        if next_tick is None:
            next_tick = rx + tick
        while rx >= next_tick:
            clock.t = next_tick
            _strikes(eng, markets, exact_strikes, next_tick, cfg)
            if not await eng.decide_once(next_tick):
                break
            next_tick += tick
        clock.t = rx
        # windows become known 20s before start (recorder subscription), like live discovery
        for key, m in markets.items():
            if key not in added and rx / 1000 >= m["window_start"] - 25:
                w = Window(asset=m["asset"], start=m["window_start"], slug=m["slug"], condition_id=m["condition_id"],
                           token_up=f"{key}-U", token_down=f"{key}-D")
                st.add_window(w)
                added.add(key)
        if kind == "l":
            a = sym_cl.get(ev["sym"])
            if a:
                st.prices[a].on_chainlink(ev["v"], ev["ts"], rx)
        elif kind == "b":
            a = sym_bn.get(ev["sym"])
            if a:
                st.prices[a].on_binance((ev["bid"] + ev["ask"]) / 2, rx)
        else:
            _clob(eng, ev, rx, assets)
    # settle everything with the known outcomes
    for key, w in list(eng.to_settle.items()):
        win = winners.get(w.slug)
        if win:
            res = eng.risk.on_settle(key, win, ts=w.end + 60)
            if res:
                journal.log("settle", key=key, winner=win, **{k: round(v, 4) for k, v in res.items()})
    journal.log("final", pnl=eng.risk.realized_total, rebates=eng.risk.rebates_total, stats=eng.stats)
    journal.close()
    return eng


def _strikes(eng, markets, exact, now_ms, cfg):
    for w in eng.st.windows.values():
        if w.strike_src == "gamma" or now_ms / 1000 < w.start:
            continue
        if exact and w.slug in exact and now_ms / 1000 >= w.start + 90:
            w.strike, w.strike_src = exact[w.slug], "gamma"
            continue
        if w.strike is None:
            v, n = eng.st.prices[w.asset].cl_twap(w.start)
            if v and n >= cfg.filters.min_strike_seconds:
                w.strike, w.strike_src = v, "chainlink"


def _clob(eng, ev, rx, assets):
    st = eng.st
    e = ev.get("e")
    if e == "conn":
        if ev.get("state") == "down":
            for t, b in st.books.items():
                if t.startswith(ev["asset"] + "-"):
                    b.clear()
            st.feed_alive["clob"][ev["asset"]] = 0
        return
    k = ev.get("k")
    if e == "pc":
        for kk, p, s, side, _bb, _ba in ev["c"]:
            b = st.books.get(kk)
            if b is None:
                continue
            book = b.bids if side == "B" else b.asks
            if s <= 0:
                book.pop(p, None)
            else:
                book[p] = s
            b.last_rx = rx
            st.feed_alive["clob"][kk.split("-")[0]] = rx
        return
    if k is None:
        return
    asset = k.split("-")[0]
    if asset not in assets:
        return
    st.feed_alive["clob"][asset] = rx
    if e == "book":
        b = st.books.get(k)
        if b is not None:
            b.bids = {p: s for p, s in ev["b"] if s > 0}
            b.asks = {p: s for p, s in ev["a"] if s > 0}
            b.last_rx = rx
    elif e == "trade":
        eng.on_public_trade(k, ev.get("side"), ev["p"], ev["s"], ev["ts"], rx)


def run_replay(rec_dir, cfg, journal_dir, winners, exact_strikes=None, vol_seed=None):
    return asyncio.run(replay(rec_dir, cfg, journal_dir, winners, exact_strikes, vol_seed))
