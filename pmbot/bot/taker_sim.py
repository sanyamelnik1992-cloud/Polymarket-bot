"""5m TAKER (stale-quote sniping) simulation on recorded books under a server-location latency profile.

At every tick the Chainlink-nowcast fair value (pmbot.bot.fair) is compared with the best ask of Up and Down.
If fair - ask - taker_fee >= min_edge, a FAK buy (limit = that ask, budget `size_usd`) is sent: it reaches the
engine after `order_ms`, waits the crypto taker delay (150 ms), and then fills only against asks that are still
<= limit in the book at that moment (book rebuilt on the same re-timed event stream). Positions are held to
resolution. Per-window budget and a cooldown limit repeated entries.
"""
import asyncio
import heapq
import json
from dataclasses import dataclass, field

from ..config import BINANCE_SYMBOL, CHAINLINK_SYMBOL
from ..fees import fee_per_share
from .fair import fair_value
from .replay import _clob, _profile_streams, _strikes
from .state import State, Window

TAKER_DELAY_MS = 150


@dataclass
class TakerCfg:
    min_edge: float = 0.05
    size_usd: float = 10.0
    max_cost_per_window: float = 30.0
    cooldown_ms: int = 2000
    start_s: float = 5.0
    stop_s: float = 290.0
    p_min: float = 0.03
    p_max: float = 0.97
    vol_mult: float = 1.6
    max_cl_age_ms: int = 3500
    one_side: bool = True


@dataclass
class Pos:
    side: str | None = None
    shares: float = 0.0
    cost: float = 0.0
    last_ms: int = 0
    fills: list = field(default_factory=list)


class _Eng:
    """Minimal stand-in so replay helpers (_clob/_strikes) can update state."""

    def __init__(self, assets):
        self.st = State(assets)
        self.trades = []

    def on_public_trade(self, *a, **k):
        pass


async def simulate(rec_dir, profile, tc: TakerCfg, winners, exact_strikes=None, vol_seed=None,
                   assets=("btc", "eth", "sol"), min_strike_seconds=50):
    eng = _Eng(list(assets))
    st = eng.st
    if vol_seed:
        for a, closes in vol_seed.items():
            if a in st.prices:
                st.prices[a].seed_vol(closes)
    markets = {}
    with open(f"{rec_dir}/markets.jsonl") as f:
        for line in f:
            m = json.loads(line)
            if m["asset"] in assets:
                markets[f'{m["asset"]}-{m["window_start"]}'] = m

    class _C:  # cfg shim for _strikes
        class filters:
            pass
    _C.filters.min_strike_seconds = min_strike_seconds
    sym_cl = {CHAINLINK_SYMBOL[a]: a for a in assets}
    sym_bn = {BINANCE_SYMBOL[a]: a for a in assets}
    stream = heapq.merge(*_profile_streams(rec_dir, profile), key=lambda x: x[0])
    tick = profile["tick_ms"] if profile else 100
    order_ms = profile["order_ms"] if profile else 50
    pos, pending, added = {}, [], set()
    next_tick = None
    signals = misses = 0
    for t, kind, ev in stream:
        if next_tick is None:
            next_tick = t + tick
        while t >= next_tick:
            now = next_tick
            _strikes(eng, markets, exact_strikes, now, _C)
            # fills of orders whose match time has come
            keep = []
            for due, key, token, side, limit in pending:
                if now < due:
                    keep.append((due, key, token, side, limit))
                    continue
                book = st.books.get(token)
                budget, shares, cost = tc.size_usd, 0.0, 0.0
                for px, sz in sorted((book.asks if book else {}).items()):
                    if px > limit + 1e-9 or budget <= 1e-9:
                        break
                    unit = px + fee_per_share(px)
                    q = min(sz, budget / unit)
                    shares += q
                    cost += q * unit
                    budget -= q * unit
                p = pos.setdefault(key, Pos())
                if shares > 0:
                    p.side = side
                    p.shares += shares
                    p.cost += cost
                    p.fills.append((now, side, cost / shares, shares))
                else:
                    misses += 1
            pending = keep
            for w in list(st.windows.values()):
                rel = now / 1000 - w.start
                if not (tc.start_s <= rel < tc.stop_s) or w.strike is None:
                    continue
                px = st.prices[w.asset]
                if px.cl_last is None or now - px.cl_last[2] > tc.max_cl_age_ms:
                    continue
                fv = fair_value(w, px, now, tc.vol_mult, 4.0)
                if fv is None or not (tc.p_min <= fv.p_up <= tc.p_max):
                    continue
                p = pos.setdefault(w.key, Pos())
                if now - p.last_ms < tc.cooldown_ms or p.cost + tc.size_usd > tc.max_cost_per_window:
                    continue
                for side, ptok, tok in (("U", fv.p_up, w.token_up), ("D", 1 - fv.p_up, w.token_down)):
                    if tc.one_side and p.side and side != p.side:
                        continue
                    b = st.books.get(tok)
                    ask = b.best_ask() if b else None
                    if ask is None or not (0.02 <= ask <= 0.98):
                        continue
                    if ptok - ask - fee_per_share(ask) >= tc.min_edge:
                        pending.append((now + order_ms + TAKER_DELAY_MS, w.key, tok, side, ask))
                        p.last_ms = now
                        signals += 1
                        break
            next_tick += tick
        for key, m in markets.items():
            if key not in added and t / 1000 >= m["window_start"] - 25:
                st.add_window(Window(asset=m["asset"], start=m["window_start"], slug=m["slug"],
                                     condition_id=m["condition_id"], token_up=f"{key}-U", token_down=f"{key}-D"))
                added.add(key)
        if kind == "l":
            a = sym_cl.get(ev["sym"])
            if a:
                st.prices[a].on_chainlink(ev["v"], ev["ts"], t)
        elif kind == "b":
            a = sym_bn.get(ev["sym"])
            if a:
                st.prices[a].on_binance((ev["bid"] + ev["ask"]) / 2, t)
        else:
            _clob(eng, ev, t, list(assets))
    rows = []
    for key, p in pos.items():
        if p.shares <= 0:
            continue
        slug = markets[key]["slug"]
        win = winners.get(slug)
        if not win:
            continue
        won = (p.side == "U") == (win == "Up")
        rows.append({"key": key, "side": p.side, "shares": p.shares, "cost": p.cost,
                     "pnl": (p.shares if won else 0.0) - p.cost, "entries": len(p.fills)})
    return rows, {"signals": signals, "misses": misses}


def run(rec_dir, profile, tc, winners, exact_strikes=None, vol_seed=None):
    return asyncio.run(simulate(rec_dir, profile, tc, winners, exact_strikes, vol_seed))
