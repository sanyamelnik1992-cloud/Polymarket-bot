"""Replay recorder output through the REAL bot (Engine + filters + risk + PaperBroker) on a simulated clock.

Recorder files (pmbot/recorder.py): markets.jsonl, clob.jsonl (compact), chainlink.jsonl, binance.jsonl.
Events are merged by local receive time `rx`; the engine decides every `tick_ms` of simulated time.
Strikes follow the live rules: Chainlink TWAP estimate once >= min_strike_seconds are present, the exact
published priceToBeat from `exact_strikes` (if given) from start+90s.

Latency profiles re-time every event as if the bot ran on another server: Polymarket events at their exchange
timestamp + `pm` ms, Chainlink prints at oracle time + `cl` ms, Binance ticks at recorded rx + `bn_shift` ms
(recorded here with ~71 ms from Tokyo), orders reach the exchange `order_ms` after the decision, and the engine
decides every `tick_ms`. profile=None keeps the recorded receive times.
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


HERE_BN_MS = 71       # measured one-way Binance (Tokyo) -> recording machine
HERE_PM_MS = 50       # measured median CLOB engine ts -> recording machine
PROFILES = {
    # one-way latencies in ms; cl = Chainlink oracle ts -> receipt via RTDS (pipeline ~1.25 s, location-insensitive)
    "here_slow": {"pm": None, "bn_shift": 0, "cl": None, "order_ms": 150, "tick_ms": 200},   # what we tested so far
    "here_fast": {"pm": None, "bn_shift": 0, "cl": None, "order_ms": 50, "tick_ms": 50},
    "dublin":    {"pm": 6, "bn_shift": 115 - HERE_BN_MS, "cl": 1260, "order_ms": 7, "tick_ms": 50},
    "london":    {"pm": 1, "bn_shift": 108 - HERE_BN_MS, "cl": 1255, "order_ms": 2, "tick_ms": 50},
    "tokyo":     {"pm": 105, "bn_shift": 2 - HERE_BN_MS, "cl": 1300, "order_ms": 106, "tick_ms": 50},
    # two servers: the bot in London + a relay next to Binance (Tokyo) forwarding over a dedicated low-latency line
    # (vendor claims ~132-150 ms RTT Tokyo<->London, i.e. ~66-75 ms one way, vs ~100-108 ms over the public internet)
    "london_relay": {"pm": 1, "bn_shift": 68 - HERE_BN_MS, "cl": 1255, "order_ms": 2, "tick_ms": 50},
    # theoretical ceiling: Binance teleported to London (2 ms) -- no relay can beat this
    "london_bn0":   {"pm": 1, "bn_shift": 2 - HERE_BN_MS, "cl": 1255, "order_ms": 2, "tick_ms": 50},
    # same servers with a 10 ms decision tick instead of 50 ms: is the gap Dublin -> London geography or reaction time?
    "dublin_t10":   {"pm": 6, "bn_shift": 115 - HERE_BN_MS, "cl": 1260, "order_ms": 7, "tick_ms": 10},
    "london_t10":   {"pm": 1, "bn_shift": 108 - HERE_BN_MS, "cl": 1255, "order_ms": 2, "tick_ms": 10},
}


def _retime(gen, fn, window_ms=3000):
    """Re-time a (rx, kind, ev) stream with fn(rx, ev) and restore order with a bounded reorder buffer."""
    heap, seq = [], 0
    for rx, kind, ev in gen:
        t = fn(rx, ev)
        heapq.heappush(heap, (t, seq, kind, ev))
        seq += 1
        while heap and heap[0][0] <= t - window_ms:
            t0, _, k0, e0 = heapq.heappop(heap)
            yield (t0, k0, e0)
    while heap:
        t0, _, k0, e0 = heapq.heappop(heap)
        yield (t0, k0, e0)


def _profile_streams(rec_dir, prof):
    clob = _jsonl(f"{rec_dir}/clob.jsonl", "c")
    cl = _jsonl(f"{rec_dir}/chainlink.jsonl", "l")
    bn = _jsonl(f"{rec_dir}/binance.jsonl", "b")
    if prof is None:
        return clob, cl, bn
    pm, cl_ms, bshift = prof["pm"], prof["cl"], prof["bn_shift"]

    def t_clob(rx, ev):
        if pm is None:
            return rx
        ts = ev.get("ts")
        if ts and 0 <= rx - ts < 1000:       # live update: exchange time + our latency to the exchange
            return ts + pm
        return rx - HERE_PM_MS + pm          # snapshots on (re)subscribe carry old ts; conn markers have none

    def t_cl(rx, ev):
        return rx if cl_ms is None else ev["ts"] + cl_ms

    return (_retime(clob, t_clob), _retime(cl, t_cl), _retime(bn, lambda rx, ev: rx + bshift))


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


async def replay(rec_dir, cfg, journal_dir, winners: dict, exact_strikes: dict | None = None,
                 vol_seed: dict | None = None, assets=None, profile: dict | None = None):
    """winners: slug -> 'Up'/'Down'. Returns the engine (risk manager holds the results)."""
    assets = assets or cfg.strategy.assets
    cfg.risk.state_file = ""          # a replay never reads or writes the live risk state
    if profile is not None:
        cfg.execution.place_latency_ms = profile["order_ms"]
        cfg.execution.cancel_latency_ms = profile["order_ms"]
        cfg.execution.tick_ms = profile["tick_ms"]
    clock = Clock()
    journal = Journal(journal_dir)

    def factory(on_fill, journal):
        return PaperBroker(cfg.execution.place_latency_ms, cfg.execution.cancel_latency_ms, on_fill=on_fill,
                           journal=journal, fill_at_touch=cfg.execution.paper_fill_at_touch)

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
    stream = heapq.merge(*_profile_streams(rec_dir, profile), key=lambda x: x[0])
    tick = cfg.execution.tick_ms
    next_tick = None
    added = set()
    for rx, kind, ev in stream:
        clock.t = rx
        if next_tick is None:
            next_tick = rx + tick
        while rx >= next_tick:
            clock.t = next_tick
            _settle_due(eng, winners, journal, next_tick)
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
                if st.on_tick is not None:
                    st.on_tick(a)
                    if eng._wake.is_set():          # fast reaction, exactly like the live decide loop
                        eng._wake.clear()
                        _strikes(eng, markets, exact_strikes, rx, cfg)
                        await eng.decide_once(rx)
        else:
            _clob(eng, ev, rx, assets)
    # settle what is left (windows that ended too close to the end of the recording)
    _settle_due(eng, winners, journal, None)
    journal.log("final", pnl=eng.risk.realized_total, rebates=eng.risk.rebates_total, stats=eng.stats)
    journal.close()
    return eng


SETTLE_DELAY_S = 90      # live: the settle loop waits >= 60 s after the window end, then polls Gamma for the winner


def _settle_due(eng, winners, journal, now_ms, delay_s=SETTLE_DELAY_S):
    """Settle ended windows the way the live settle loop does: shortly after the end, not at the end of the replay.
    Without this, positions pile up in the open worst-case-loss budget and throttle quoting later in a recording.
    now_ms=None settles everything left (end of the recording)."""
    for key, w in list(eng.to_settle.items()):
        if now_ms is not None and now_ms / 1000 < w.end + delay_s:
            continue
        win = winners.get(w.slug)
        if win:
            res = eng.risk.on_settle(key, win, ts=w.end + delay_s)
            if res:
                journal.log("settle", key=key, winner=win, at_ms=now_ms if now_ms is not None else -1,
                            **{k: round(v, 4) for k, v in res.items()})
        eng.to_settle.pop(key, None)


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


def run_replay(rec_dir, cfg, journal_dir, winners, exact_strikes=None, vol_seed=None, profile=None):
    return asyncio.run(replay(rec_dir, cfg, journal_dir, winners, exact_strikes, vol_seed, profile=profile))
