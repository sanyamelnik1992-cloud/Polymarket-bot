"""TWAP-lock bot ("L"): in the last minute of each 5m (or 15m) Up/Down window buy the side whose closing Chainlink
TWAP is already (almost) decided while the book still sells it below our probability (reports/44_twap_lock.md).
15m windows use the same rule (Gamma twapLookbackSeconds = 60; 30/30 windows checked on 9 Nov): strike = mean of the
prints in [T-62, T-3], close = mean over [end-62, end-3]. One duration per process (lock.duration); times inside the
closing minute are measured from the anchor Tc = end - 300, so the 5m model and its limits apply unchanged.

Rule (all must hold at the decision moment, window second rel in [rel_lo, rel_hi]):
  * empirical lock probability with the Binance nowcast  p >= theta (0.99) for the side
  * the same model on received Chainlink prints only     p_cl >= agree (0.90)   (no trading on a Binance glitch)
  * expected TWAP - strike in our favour >= dmin_bp (0.5 bp) and the nowcast spot on our side of the strike
  * Binance did not move against us by > adv1_bp in 1 s / adv3_bp in 3 s, and |1 s move| <= jump_bp
  * best ask of the side in [a_min, a_max] and <= p - min_edge - taker fee
Then one FAK buy of `shares` with that limit (paper: matched against the book paper_exec_ms later). One entry per
window, held to resolution. Risk: $ per entry, $ open across windows, daily loss stop, kill file STOP.
"""
import asyncio
import json
import logging
import math
import os
import time
import tomllib
from dataclasses import asdict, dataclass, field, fields

from ..fees import fee_per_share
from ..gamma import fetch_event, parse_event, slug_for, window_start
from ..lockmodel import (ClSigma, Tables, close_from_prints, expected_dist_bp, lock_state, prob_up, strike_from_prints)
from .feeds import ClobFeed, binance_feed, polybolt_feed, rtds_feed
from .paper import PaperBroker
from .state import State, Window, now_ms

log = logging.getLogger("pmbot.lock")


# ------------------------------------------------------------------------------------------------ config
DURATIONS = {300: "5m", 900: "15m"}     # window length -> slug tag (btc-updown-15m-<start>)
PROV_MIN_BP = 0.5        # provisional result with forward-filled prints: |TWAP / strike - 1| must be at least this (bp)


@dataclass
class LockCfg:
    assets: list = field(default_factory=lambda: ["btc", "eth", "sol", "xrp"])
    theta: float = 0.99
    agree: float = 0.90
    dmin_bp: float = 0.5
    adv1_bp: float = 1.0
    adv3_bp: float = 2.0
    jump_bp: float = 5.0
    a_min: float = 0.50
    a_max: float = 0.98
    min_edge: float = 0.005
    rel_lo: float = 240.0
    rel_hi: float = 299.5
    shares: float = 5.0
    max_entries_per_window: int = 1
    retry_ms: int = 1000
    cl_max_age_ms: int = 2600
    bn_max_age_ms: int = 1500
    book_max_age_ms: int = 4000
    min_sigma_prints: int = 900          # 15 min of Chainlink prints before sigma is trusted (restored after restarts)
    strike_tol_bp: float = 0.3           # published priceToBeat vs our [T-62, T-3] mean: skip the window if further
    tables: str = "config/lock_tables.npz"
    tick_ms: int = 50
    duration: int = 300                  # window length in s: 300 (5m markets) or 900 (15m markets)
    entry: str = "taker"                 # "taker": FAK at the ask (L); "maker": resting bid for the decided side (LM)
    tiers: list = field(default_factory=list)   # [[p_min, ask_max], ...]: the lower the model probability, the cheaper the ask must
                                         # be (L3, reports/47). Empty = the plain rule (p >= theta, ask <= min(a_max, p - edge - fee))
    sigma_floor_bp: float = 0.0          # the model never uses a 1 s sigma below this (bp). The EWMA sigma of a calm market
                                         # (0.1-0.3 bp) makes a 1-2 bp lead look decided. Replay 7-10 Nov with the live limits and
                                         # Gamma outcomes: 0.5 bp -> 110 entries, 3 lost, +$32 (plain L3: 262, 13 lost, +$46); with
                                         # fills 0.5 s late +$27 vs +$23 (reports/52). 0 = off
    floor_free_p: float = 0.0            # with a sigma floor: a side the UNFLOORED model gives >= this (0.99) is judged by the
                                         # unfloored model, i.e. the original L rule. The floor moved those states into the
                                         # cheaper tiers and lost them: 57 entries, 0 lost on 7-10 Nov (reports/55). 0 = off
    shock_bp: float = 0.0                # no entry while a 3 s Binance move larger than this (bp, either way) ended within the
    shock_s: float = 10.0                # last shock_s seconds: after a jump the 15-minute sigma is stale and the price often
                                         # snaps back (live loss 10 Nov 10:25: -2.9 bp in 3 s, entry 3 s later). 0 = off
    shock_sigmas: float = 0.0            # > 0: the pause threshold grows with volatility, max(shock_bp, shock_sigmas * sigma * sqrt(3))
                                         # with the model's 1 s sigma. A fixed 2.5 bp is a shock at sigma 0.5 bp but an ordinary 3 s
                                         # move at sigma 2 bp: on 10 Nov from 13:00 UTC the pause was on 50-95 % of the time and
                                         # live made no entry for hours (reports/58). 0 = the fixed threshold


@dataclass
class LockRisk:
    max_usd_per_entry: float = 5.0
    max_open_usd: float = 10.0           # cost of entries in windows not resolved yet (all assets end together)
    daily_loss_limit: float = 5.0        # realized + provisional losses of the UTC day
    kill_file: str = "STOP"
    state_file: str = ""
    resume_next_day: bool = True         # paper: idle after the stop and resume at 00:00 UTC; live must be False


@dataclass
class LockExec:
    mode: str = "paper"
    paper_exec_ms: int = 300             # our ~60-90 ms to the exchange + its 150 ms taker delay, rounded up
    paper_place_ms: int = 60             # maker: bid goes live (live 8 Nov: place median 52-62 ms)
    paper_cancel_ms: int = 50            # maker: cancel takes effect (cancel median 40-49 ms)
    cl_feed: str = "rtds"                # Chainlink prints: "rtds" (legacy socket), "polybolt" (new, ~0.25 s earlier, needs the
                                         # CLOB credentials) or "both" (first arrival wins, RTDS is the fallback; reports/57)


@dataclass
class LockConfig:
    lock: LockCfg = field(default_factory=LockCfg)
    risk: LockRisk = field(default_factory=LockRisk)
    execution: LockExec = field(default_factory=LockExec)
    journal_dir: str = "data/journal-lock"


def _apply(obj, data, path=""):
    names = {f.name for f in fields(obj)}
    for k, v in data.items():
        if k not in names:
            raise ValueError(f"unknown config key: {path}{k}")
        cur = getattr(obj, k)
        if hasattr(cur, "__dataclass_fields__"):
            _apply(cur, v, f"{path}{k}.")
        elif isinstance(cur, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{path}{k} must be true/false")
            setattr(obj, k, v)
        elif isinstance(cur, (int, float)):
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{path}{k} must be a number")
            setattr(obj, k, type(cur)(v))
        else:
            setattr(obj, k, v)


def load_lock_config(path=None, overrides=None):
    cfg = LockConfig()
    if path:
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f))
    if overrides:
        _apply(cfg, overrides)
    c, r, e = cfg.lock, cfg.risk, cfg.execution
    if e.mode not in ("paper", "live"):
        raise ValueError("execution.mode must be 'paper' or 'live'")
    if e.cl_feed not in ("rtds", "polybolt", "both"):
        raise ValueError("execution.cl_feed must be 'rtds', 'polybolt' or 'both'")
    if not 0.5 < c.agree <= c.theta < 1:
        raise ValueError("lock: 0.5 < agree <= theta < 1")
    if not 0 < c.a_min < c.a_max < 1:
        raise ValueError("lock: 0 < a_min < a_max < 1")
    if not 238 <= c.rel_lo < c.rel_hi < 300:
        raise ValueError("lock: 238 <= rel_lo < rel_hi < 300 (the model is valid inside the closing TWAP window only)")
    if c.shares < 1:
        raise ValueError("lock.shares must be >= 1")
    if c.duration not in DURATIONS:
        raise ValueError(f"lock.duration must be one of {sorted(DURATIONS)}")
    if not 0 <= c.sigma_floor_bp <= 5:
        raise ValueError("lock.sigma_floor_bp must be in [0, 5] (bp of a 1 s return)")
    if c.floor_free_p and not 0.9 <= c.floor_free_p < 1:
        raise ValueError("lock.floor_free_p must be 0 (off) or in [0.9, 1)")
    if c.shock_bp < 0 or not 1 <= c.shock_s <= 60:
        raise ValueError("lock: shock_bp >= 0 and 1 <= shock_s <= 60")
    if c.shock_sigmas and not 2 <= c.shock_sigmas <= 10:
        raise ValueError("lock.shock_sigmas must be 0 (off) or in [2, 10]")
    if c.entry not in ("taker", "maker"):
        raise ValueError("lock.entry must be 'taker' or 'maker'")
    if c.tiers:
        ts = sorted((float(t[0]), float(t[1])) for t in c.tiers)
        if any(len(t) != 2 for t in c.tiers) or any(not (0.5 < pm < 1 and c.a_min <= am <= c.a_max) for pm, am in ts):
            raise ValueError("lock.tiers: [[p_min in (0.5,1), ask_max in [a_min, a_max]], ...]")
        if any(b[1] < a[1] for a, b in zip(ts, ts[1:])):
            raise ValueError("lock.tiers: a higher probability must not allow a cheaper ask")
        if ts[0][0] < c.theta - 1e-12:
            raise ValueError("lock.tiers: theta must be <= the lowest tier probability (the state filter runs first)")
    if c.entry == "maker" and e.mode != "paper":
        raise ValueError("lock.entry = 'maker' is paper-only for now (no live maker broker for the lock bot yet)")
    for name in ("max_usd_per_entry", "max_open_usd", "daily_loss_limit"):
        if getattr(r, name) <= 0:
            raise ValueError(f"risk.{name} must be > 0")
    if r.max_usd_per_entry > r.max_open_usd:
        raise ValueError("risk.max_usd_per_entry cannot exceed risk.max_open_usd")
    if e.mode == "live" and (not r.state_file or r.resume_next_day):
        raise ValueError("live: risk.state_file is required and risk.resume_next_day must be false")
    bad = [a for a in c.assets if a not in ("btc", "eth", "sol", "xrp")]
    if bad:
        raise ValueError(f"unsupported assets: {bad}")
    return cfg


# ------------------------------------------------------------------------------------------------ decision
def tick_floor(x, tick):
    return math.floor(x / tick + 1e-9) * tick


def shock_from_samples(mids, look_s):
    """Largest |3 s move| (bp) that ended in the last look_s seconds. mids[j] = Binance mid 0.5 * j seconds ago
    (None = unknown); needs 2 * look_s + 7 samples."""
    worst = 0.0
    for k in range(0, int(look_s * 2) + 1):
        a0, a3 = mids[k] if k < len(mids) else None, mids[k + 6] if k + 6 < len(mids) else None
        if a0 and a3:
            worst = max(worst, abs(math.log(a0 / a3)) * 1e4)
    return worst


def bn_shock_bp(bn_at, now_ms_, look_s):
    """shock_from_samples for a lookup function bn_at(ms) -> mid (replay, tests)."""
    return shock_from_samples([bn_at(now_ms_ - k * 500) for k in range(0, int(look_s * 2) + 7)], look_s)


def hist_samples(hist, now_ms_, n, step_ms=500):
    """Mids as of now - j * step for j = 0..n-1 from (rx_ms, mid) ticks in time order, in one backward pass.
    Like AssetPrices.bn_mid_at: the last tick at or before the time; older than the history = its oldest tick."""
    out, j, oldest = [None] * n, 0, None
    for rx, mid in reversed(hist):
        oldest = mid
        while j < n and rx <= now_ms_ - j * step_ms:
            out[j] = mid
            j += 1
        if j >= n:
            break
    return [oldest if x is None else x for x in out]


def decide_state(c: LockCfg, tables, asset, T, K, now_ms_, cl_by_sec, cl_last, sigma, bn_now, bn_at_last, bn_1s,
                 bn_3s, shock=None):
    """The book-independent part of the rule (time, strike, model, agreement, distance, spot, Binance guards).
    Returns (side "U"/"D" or None, p, context dict); None = no trade, the reason is ctx["why"]."""
    now_s = now_ms_ / 1000.0
    rel = now_s - T
    ctx = {"rel": round(rel, 2)}
    if not (c.rel_lo <= rel <= c.rel_hi):
        return None, 0.0, {**ctx, "why": "time"}
    if K is None:
        return None, 0.0, {**ctx, "why": "no_strike"}
    if sigma is None:
        return None, 0.0, {**ctx, "why": "no_sigma"}
    if cl_last is None or now_ms_ - cl_last[2] > c.cl_max_age_ms:
        return None, 0.0, {**ctx, "why": "cl_stale"}
    if not bn_now or not bn_at_last or not bn_1s or not bn_3s:
        return None, 0.0, {**ctx, "why": "no_binance"}
    sigma_raw = sigma
    sigma = max(sigma, c.sigma_floor_bp * 1e-4)
    st = lock_state(cl_by_sec, cl_last[1] // 1000, cl_last[0], T, K, now_s, bn_now, bn_at_last)
    p_nc = prob_up(tables, asset, st["A_nc"], st["m_nc"], st["S_nc"], K, sigma)
    p_cl = prob_up(tables, asset, st["A"], st["m_cl"], st["S_cl"], K, sigma)
    up = p_nc >= 0.5
    sgn = 1.0 if up else -1.0
    p = p_nc if up else 1 - p_nc
    pc = p_cl if up else 1 - p_cl
    if c.floor_free_p and sigma > sigma_raw:
        # the top tier is the original L rule: when the unfloored model is that sure, it decides (reports/55)
        q = prob_up(tables, asset, st["A_nc"], st["m_nc"], st["S_nc"], K, sigma_raw)
        q = q if up else 1 - q
        if q >= c.floor_free_p:
            qc = prob_up(tables, asset, st["A"], st["m_cl"], st["S_cl"], K, sigma_raw)
            p, pc, sigma = q, (qc if up else 1 - qc), sigma_raw
    dist = sgn * expected_dist_bp(st["A_nc"], st["m_nc"], st["S_nc"], K)
    spot = sgn * (st["S_nc"] / K - 1) * 1e4
    mv1 = math.log(bn_now / bn_1s) * 1e4
    mv3 = math.log(bn_now / bn_3s) * 1e4
    ctx.update(side="U" if up else "D", p=round(p, 6), p_cl=round(pc, 6), dist_bp=round(dist, 3),
               spot_bp=round(spot, 3), mv1=round(mv1, 3), mv3=round(mv3, 3), n=st["n"], m=st["m_nc"],
               sigma_bp=round(sigma * 1e4, 4), sigma_raw_bp=round(sigma_raw * 1e4, 4))
    if p < c.theta:
        return None, 0.0, {**ctx, "why": "p"}
    if pc < c.agree:
        return None, 0.0, {**ctx, "why": "agree"}
    if dist < c.dmin_bp:
        return None, 0.0, {**ctx, "why": "dist"}
    if spot < 0:
        return None, 0.0, {**ctx, "why": "spot_side"}
    if -sgn * mv1 > c.adv1_bp or -sgn * mv3 > c.adv3_bp:
        return None, 0.0, {**ctx, "why": "binance_against"}
    if abs(mv1) > c.jump_bp:
        return None, 0.0, {**ctx, "why": "binance_jump"}
    if c.shock_bp > 0 and shock is not None:           # last and lazy: computed only when everything else passes
        sh = shock() if callable(shock) else shock
        ctx["shock_bp"] = round(sh, 3)
        lim = c.shock_bp
        if c.shock_sigmas > 0:                         # a 3 s move of a random walk has std sigma * sqrt(3)
            lim = max(lim, c.shock_sigmas * sigma * 1e4 * math.sqrt(3))
            ctx["shock_lim"] = round(lim, 3)
        if sh > lim:
            return None, 0.0, {**ctx, "why": "shock"}
    return ("U" if up else "D"), p, ctx


def tier_limit(c: LockCfg, p):
    """Highest ask accepted at model probability p: the cheapest-tier rule of c.tiers that p reaches, None if p reaches none."""
    best = None
    for pm, am in c.tiers:
        if p >= pm - 1e-12 and (best is None or am > best):
            best = am
    return best


def decide(c: LockCfg, tables, asset, T, K, now_ms_, cl_by_sec, cl_last, sigma, bn_now, bn_at_last, bn_1s, bn_3s,
           ask_up, ask_dn, shock=None):
    """Pure taker decision. cl_last = (value, ts_ms, rx_ms); ask_* = (price, size) or None.
    Returns (signal or None, context dict). signal = dict(side, limit, ask, p, ...)."""
    side, p, ctx = decide_state(c, tables, asset, T, K, now_ms_, cl_by_sec, cl_last, sigma, bn_now, bn_at_last, bn_1s,
                                bn_3s, shock)
    if side is None:
        return None, ctx
    ask = ask_up if side == "U" else ask_dn
    if not ask:
        return None, {**ctx, "why": "no_ask"}
    a = ask[0]
    limit = min(c.a_max, p - c.min_edge - fee_per_share(a))
    if c.tiers:                                 # L3: the margin is in the tier (a lower p must come cheaper), not in p - edge - fee
        tl = tier_limit(c, p)
        limit = min(c.a_max, tl) if tl is not None else 0.0
    ctx.update(ask=a, ask_sz=ask[1], limit=round(limit, 4))
    if a < c.a_min:
        return None, {**ctx, "why": "ask_too_low"}
    if a > limit + 1e-9:
        return None, {**ctx, "why": "ask_too_high"}
    return {"side": side, "limit": limit, "ask": a, "p": p}, ctx


def maker_bid(c: LockCfg, p, best_ask):
    """Maker price for the decided side: p - min_edge (a maker pays no taker fee), capped at a_max and one cent under
    the best ask (post-only), on the 0.01 grid (valid at tick 0.01 and 0.001). None below a_min.
    With lock.tiers (LM3, reports/52) the price is the tier limit of p instead: the bid a taker of L3 would pay."""
    top = p - c.min_edge
    if c.tiers:
        top = tier_limit(c, p)
        if top is None:
            return None
    b = tick_floor(min(c.a_max, top), 0.01)
    if best_ask is not None:
        b = min(b, tick_floor(best_ask - 0.01, 0.01))
    b = round(b, 2)
    return b if b >= c.a_min - 1e-9 else None


# ------------------------------------------------------------------------------------------------ brokers
class PaperTaker:
    mode = "paper"

    def __init__(self, st: State, exec_ms=300):
        self.st, self.exec_ms = st, exec_ms

    async def start(self):
        return True

    async def stop(self):
        return None

    async def buy(self, token, key, side, limit, shares):
        """FAK against the book `exec_ms` later: every ask level <= limit, up to `shares`. Returns (avg px, shares, fee)."""
        await asyncio.sleep(self.exec_ms / 1000)
        b = self.st.books.get(token)
        if not b:
            return 0.0, 0.0, 0.0
        got = cost = fee = 0.0
        for px in sorted(b.asks):
            if px > limit + 1e-9 or got >= shares - 1e-9:
                break
            q = min(b.asks[px], shares - got)
            got += q
            cost += q * px
            fee += q * fee_per_share(px)
        return (cost / got if got else 0.0), got, fee


def fak_budget(shares, limit):
    """All-in budget for a FAK BUY of `shares` capped at `limit`: the notional plus the taker fee at the cap, plus a cent.

    With the budget equal to the notional the SDK takes the fee out of the notional, and 5 shares at 0.95 become an
    order for 4.9789 shares: below the exchange minimum of 5 shares, and with 5 decimals (4.97894) once the market's tick
    is 0.001 (it switches when the price passes 0.96). That is the likely cause of the two "invalid amounts" rejections
    of 9-10 Nov (caps 0.94-0.95): the exchange has long refused market BUYs with more than 4 decimals of shares
    ("taker amount a max of 4 decimals"). Not confirmed on the server; with the fee on top the order is for exactly
    `shares`, valid at any tick. Cost per entry: 5 x 0.91 + $0.03 fee = $4.58 (was $4.55 for 4.97 shares)."""
    return math.ceil((shares * limit + shares * fee_per_share(limit)) * 100 - 1e-9) / 100 + 0.01


class LiveTaker:
    mode = "live"

    def __init__(self, max_usd, journal):
        from .live import TakerLiveBroker
        self.b = TakerLiveBroker(max_usd, journal=journal)

    async def start(self):
        return await self.b.start()

    async def stop(self):
        return None

    async def prewarm(self, token):
        return await self.b.prewarm(token)

    async def ping(self, token):
        return await self.b.ping(token)

    async def buy(self, token, key, side, limit, shares):
        from .live import NO_MATCH_CODES
        usd = round(shares * limit, 2)
        r = await self.b.place_fak(token, key, "BUY", limit, usd, max_spend=fak_budget(shares, limit))
        if not r:
            why = self.b.last_reject
            if why not in NO_MATCH_CODES:
                # refused for another reason (balance, market state) or accepted and still pending: a second order
                # could double the position or repeat the same error every second -> the caller blocks the window
                raise RuntimeError(f"order not filled, not safe to repeat: {why or 'no reason given'}")
            return 0.0, 0.0, 0.0      # clean miss: the ask was gone when the order matched, nothing was bought
        # counted on top of the spend like pmbot/arb_live.py (conservative if the exchange already included it);
        # the wallet balance (pmbot-wallet.timer) is the final check of live PnL
        return r["price"], r["size"], r["size"] * fee_per_share(r["price"])


# ------------------------------------------------------------------------------------------------ bot
class LockBot:
    def __init__(self, cfg: LockConfig, journal, broker=None, tables=None):
        self.cfg = cfg
        c = cfg.lock
        self.j = journal
        self.st = State(c.assets)
        self.tables = tables or Tables(c.tables)
        self.sig = {a: ClSigma() for a in c.assets}
        self.maker = c.entry == "maker"
        self.bids = {}             # maker: window key -> {"oid", "side", "price", "size", "token", "p"}
        if broker is not None:
            self.broker = broker
        elif self.maker:           # paper only (load_lock_config): queue model of the paper maker bots
            self.broker = PaperBroker(cfg.execution.paper_place_ms, cfg.execution.paper_cancel_ms,
                                      on_fill=self._on_maker_fill, queue=True)
        elif cfg.execution.mode == "live":
            self.broker = LiveTaker(cfg.risk.max_usd_per_entry, journal)
        else:
            self.broker = PaperTaker(self.st, cfg.execution.paper_exec_ms)
        self.feeds = {a: ClobFeed(self.st, a, on_trade=self._on_public_trade if self.maker else None,
                                  alive=self.alive) for a in c.assets}
        self.meta = {}             # window key -> parsed market (from Gamma, fetched ahead)
        self.strikes = {}          # window key -> K
        self.entries = {}          # window key -> list of entry dicts (open until settled)
        self.busy = set()          # window keys with an order in flight
        self.pending = {}          # window key -> $ of an order in flight (counted in the open-risk limit)
        self.warmed = set()        # window keys whose market metadata the live client already has (prewarm)
        self._bg = set()           # background tasks (prewarm): keep a reference until they finish
        self.next_try = {}         # window key -> ms
        self._shock_cache = {}     # asset -> (ms, bp)
        self._cl_seen, self._cl_lags = {}, []      # print delay samples of the status interval (stats.cl_lag_ms = median)
        self.last_why = {}
        self.stats = {"signals": 0, "orders": 0, "filled": 0, "missed": 0, "skipped_risk": 0,
                      "lag_max_ms": 0, "lag_over_100ms": 0, "decide_max_ms": 0}   # lag/decide: per 30 s status
        self.dur = c.duration
        self.tag = DURATIONS[c.duration]
        self._stop = False
        self.risk = {"realized_by_day": {}, "provisional": {}, "killed_day": None, "kill_reason": "", "settled": []}
        self._load_state()

    # ---------------------------------------------------------------- state
    def _state_path(self):
        return self.cfg.risk.state_file

    def _load_state(self):
        p = self._state_path()
        if not p or not os.path.exists(p):
            return
        try:
            d = json.load(open(p))
        except (OSError, ValueError):
            return
        self.risk.update({k: d.get(k, v) for k, v in self.risk.items()})
        self.entries = {k: v for k, v in (d.get("open") or {}).items()}
        sg = d.get("sigma") or {}
        for a, s in sg.items():
            if a in self.sig and time.time() - s.get("saved", 0) < 300:
                o = self.sig[a]
                o.num, o.den, o.n, o.last_sec, o.last_v = s["num"], s["den"], s["n"], s["last_sec"], s["last_v"]

    def _save_state(self):
        p = self._state_path()
        if not p:
            return
        d = dict(self.risk)
        d["open"] = self.entries
        d["settled"] = d["settled"][-500:]
        d["sigma"] = {a: {"num": o.num, "den": o.den, "n": o.n, "last_sec": o.last_sec, "last_v": o.last_v,
                          "saved": time.time()} for a, o in self.sig.items() if o.last_sec is not None}
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, p)

    def anchor(self, start):
        """Tc = end - 300: the closing TWAP is [Tc+238, Tc+297] and decision times are Tc+rel_lo..Tc+rel_hi."""
        return int(start) + self.dur - 300

    def end_of(self, key):
        return int(key.rsplit("-", 1)[1]) + self.dur

    @staticmethod
    def today(ts=None):
        return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))

    def day_pnl(self):
        d = self.today()
        return self.risk["realized_by_day"].get(d, 0.0) + sum(
            v for k, v in self.risk["provisional"].items() if k not in self.risk["settled"]
            and self.today(self.end_of(k)) == d)

    def open_cost(self):
        """Cost still at risk: entries whose window outcome is not known yet. An entry stops counting once its
        provisional result (Chainlink TWAP rule, seconds after the window end) is in day_pnl, not only at the Gamma
        settle (median 5.4 min after the end on 9 Nov: with max_open_usd = 5 that blocked every next window)."""
        prov = self.risk["provisional"]
        return sum(e["cost"] + e["fee"] for k, es in self.entries.items() if k not in prov for e in es)

    def filled(self, key):
        return sum(e["shares"] for e in self.entries.get(key, []))

    def alive(self):
        return not self._stop

    def can_enter(self):
        r = self.cfg.risk
        if os.path.exists(r.kill_file):
            return False, "kill_file"
        if self.risk.get("killed_day") == self.today():
            return False, "killed"
        if self.day_pnl() <= -r.daily_loss_limit:
            self._kill(f"daily loss limit {r.daily_loss_limit} reached")
            return False, "killed"
        return True, ""

    def _kill(self, reason):
        if self.risk.get("killed_day") == self.today():
            return
        self.risk["killed_day"], self.risk["kill_reason"] = self.today(), reason
        self._save_state()
        self.j.log("kill", reason=reason, pnl_day=round(self.day_pnl(), 4))
        log.error("KILL: %s", reason)
        if not self.cfg.risk.resume_next_day:
            self._stop = True

    # ---------------------------------------------------------------- discovery
    async def discover_loop(self):
        while self.alive():
            now = time.time()
            ws0 = window_start(now, self.dur)
            for start in (ws0, ws0 + self.dur):
                for a in self.cfg.lock.assets:
                    key = f"{a}-{start}"
                    if key in self.meta:
                        continue
                    try:
                        e = await asyncio.to_thread(fetch_event, slug_for(a, start, self.tag))
                    except Exception as ex:  # noqa: BLE001
                        log.warning("discover %s: %s", key, ex)
                        continue
                    if e:
                        m = parse_event(e)
                        mk = e["markets"][0]
                        m["tick"] = float(mk.get("orderPriceMinTickSize") or 0.01)
                        self.meta[key] = m
            # books only around the last minute (light feed): add 100 s before the end, drop after the end
            for key, m in list(self.meta.items()):
                a, start = key.rsplit("-", 1)
                start = int(start)
                rel = now - start
                if self.dur - 100 <= rel < self.dur + 5 and key not in self.st.windows:
                    self.st.add_window(Window(asset=a, start=start, slug=m["slug"], condition_id=m["condition_id"],
                                              token_up=m["token_up"], token_down=m["token_down"], tick=m["tick"],
                                              end=start + self.dur))
                    self._prewarm(key, m["token_up"])
                if rel >= self.dur + 5 and key in self.st.windows:
                    self.st.drop_window(key)
                if rel > self.dur + 600 and key not in self.entries:
                    self.meta.pop(key, None)
                    self.strikes.pop(key, None)
                    self.warmed.discard(key)
                    self.next_try.pop(key, None)
                    self.last_why.pop(key, None)
            for f in self.feeds.values():
                await f.sync()
            await asyncio.sleep(2)

    def _prewarm(self, key, token):
        """Live: ~40 s before the closing minute let the exchange client look up the market (tick size, fees) so the
        first order of the window does not wait for two extra HTTP round trips. One token warms both sides."""
        fn = getattr(self.broker, "prewarm", None)
        if fn is None or key in self.warmed:
            return
        self.warmed.add(key)

        async def run():
            for _ in range(3):
                ms = await fn(token)
                if ms is not None:
                    self.j.log("prewarm", key=key, ms=ms)
                    return
                await asyncio.sleep(2)

        t = asyncio.get_running_loop().create_task(run())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def warm_loop(self, period_s=8.0):
        """Live: keep the exchange connection open through the closing minute (TakerLiveBroker.ping). From 15 s before
        the first decision to the window end, one cheap request every 8 s; the best time of the status interval is
        reported as stats["ping_ms"] (the round trip to the exchange on a warm connection)."""
        fn = getattr(self.broker, "ping", None)
        if fn is None:
            return
        while self.alive():
            try:
                now = time.time()
                w = next((w for w in self.st.windows.values()
                          if self.cfg.lock.rel_lo - 15 <= now - self.anchor(w.start) <= self.cfg.lock.rel_hi), None)
                if w is not None:
                    ms = await fn(w.token_up)
                    if ms is not None and (not self.stats.get("ping_ms") or ms < self.stats["ping_ms"]):
                        self.stats["ping_ms"] = ms
            except Exception as e:  # noqa: BLE001
                log.warning("warm ping: %s", e)
            await asyncio.sleep(period_s)

    def strike(self, key, asset, T):
        K = self.strikes.get(key)
        if K is None:
            K = strike_from_prints(self.st.prices[asset].cl_by_sec, T)
            if K is not None:
                self.strikes[key] = K
                self.j.log("strike", key=key, K=K)
        return K

    async def strike_check_loop(self):
        """Published priceToBeat (when Gamma has it) must agree with our estimate; otherwise skip the window."""
        while self.alive():
            for key, K in list(self.strikes.items()):
                m = self.meta.get(key)
                if not m or m.get("_checked") or K is None:
                    continue
                a, T = key.rsplit("-", 1)
                if time.time() - int(T) > self.dur:
                    continue
                try:
                    e = await asyncio.to_thread(fetch_event, m["slug"])
                    v = ((e or {}).get("eventMetadata") or {}).get("priceToBeat")
                except Exception:  # noqa: BLE001
                    v = None
                if v:
                    m["_checked"] = True
                    diff = abs(float(v) / K - 1) * 1e4
                    self.j.log("strike_check", key=key, ours=K, published=float(v), diff_bp=round(diff, 4))
                    if diff > self.cfg.lock.strike_tol_bp:
                        self.strikes[key] = None          # unknown strike: never trade this window
                        self.j.log("strike_mismatch", key=key, diff_bp=round(diff, 4))
            await asyncio.sleep(10)

    # ---------------------------------------------------------------- decisions
    def _update_sigma(self):
        for a in self.cfg.lock.assets:
            px = self.st.prices[a]
            if not px.cl_last:
                continue
            if px.cl_last[1] != self._cl_seen.get(a):          # a new print: how late it arrived (arrival - print time)
                self._cl_seen[a] = px.cl_last[1]
                self._cl_lags.append(px.cl_last[2] - px.cl_last[1])
            last = px.cl_last[1] // 1000
            o = self.sig[a]
            if o.last_sec is None:
                o.on_print(last, px.cl_last[0])
                continue
            for s in range(o.last_sec + 1, last + 1):
                v = px.cl_by_sec.get(s, o.last_v)
                o.on_print(s, v)

    def _shock(self, asset, now):
        """Largest 3 s Binance move of the last shock_s seconds (bp), cached for 200 ms per asset."""
        c = self._shock_cache.get(asset)
        if c and now - c[0] < 200:
            return c[1]
        look = self.cfg.lock.shock_s
        v = shock_from_samples(hist_samples(self.st.prices[asset].bn_hist, now, int(look * 2) + 7), look)
        self._shock_cache[asset] = (now, v)
        return v

    def _book_ask(self, token):
        b = self.st.books.get(token)
        if not b or not b.asks:
            return None
        if now_ms() - self.st.feed_alive["clob"].get(self._asset_of_token(token), 0) > self.cfg.lock.book_max_age_ms:
            return None
        a = min(b.asks)
        return a, b.asks[a]

    def _asset_of_token(self, token):
        w = self.st.token_window.get(token)
        return w.asset if w else ""

    async def decide_loop(self):
        c = self.cfg.lock
        while self.alive():
            t0 = time.time()
            try:
                self._update_sigma()
                await self.decide_once(now_ms())
            except Exception as e:  # noqa: BLE001
                log.exception("decide error: %s", e)
                self.j.log("error", where="decide", error=str(e)[:300])
            took = int((time.time() - t0) * 1000)
            if took > self.stats["decide_max_ms"]:
                self.stats["decide_max_ms"] = took
            await asyncio.sleep(max(0.0, c.tick_ms / 1000 - (time.time() - t0)))

    async def lag_loop(self, period_s=0.05):
        """Event-loop lag (a busy or starved process sees the market late): max per status interval, and how many
        wake-ups came > 100 ms late. On 9 Nov the VPS lost 12-32 % of its CPU time to the hypervisor (steal)."""
        while self.alive():
            t = time.perf_counter()
            await asyncio.sleep(period_s)
            lag = int((time.perf_counter() - t - period_s) * 1000)
            if lag > self.stats["lag_max_ms"]:
                self.stats["lag_max_ms"] = lag
            if lag > 100:
                self.stats["lag_over_100ms"] += 1

    async def decide_once(self, now):
        if self.maker:
            return await self.maker_once(now)
        c = self.cfg.lock
        ok, why_risk = self.can_enter()
        cands = []                 # signals of this tick: (margin p - ask, window, signal, shares, ctx)
        for key, w in list(self.st.windows.items()):
            Tc = self.anchor(w.start)
            rel = now / 1000 - Tc
            if not (c.rel_lo <= rel <= c.rel_hi):
                continue
            if key in self.busy or len(self.entries.get(key, [])) >= c.max_entries_per_window:
                continue
            if now < self.next_try.get(key, 0):
                continue
            px = self.st.prices[w.asset]
            if now - self.st.feed_alive.get("binance", 0) > c.bn_max_age_ms:
                self._why(key, "bn_stale")
                continue
            K = self.strike(key, w.asset, w.start)
            cl = px.cl_last
            bn_now = px.bn_mid()
            bn_at_last = px.bn_mid_at(cl[1] + 999) if cl else None
            sig, ctx = decide(c, self.tables, w.asset, Tc, K, now, px.cl_by_sec, cl, self.sig[w.asset].sigma()
                              if self.sig[w.asset].n >= c.min_sigma_prints else None, bn_now, bn_at_last,
                              px.bn_mid_at(now - 1000), px.bn_mid_at(now - 3000),
                              self._book_ask(w.token_up), self._book_ask(w.token_down),
                              shock=lambda a=w.asset: self._shock(a, now))
            if not sig:
                self._why(key, ctx.get("why"), ctx)
                continue
            self.stats["signals"] += 1
            if not ok:
                self._why(key, "risk:" + why_risk, ctx)
                continue
            shares = min(c.shares, math.floor(self.cfg.risk.max_usd_per_entry / sig["limit"] * 100) / 100)
            cands.append((sig["p"] - sig["ask"], w, sig, shares, ctx))
        # Open risk counts the orders still in flight: all coins become eligible in the same tick at rel_lo, and an
        # order takes ~0.3 s to come back, so without the reservation two or three coins went out together past
        # max_open_usd (the limit that makes live "one entry at a time"). The widest margin goes first.
        for _, w, sig, shares, ctx in sorted(cands, key=lambda x: -x[0]):
            need = shares * sig["limit"]
            if self.open_cost() + sum(self.pending.values()) + need > self.cfg.risk.max_open_usd + 1e-9 or shares < 1:
                self.stats["skipped_risk"] += 1
                self._why(w.key, "risk:max_open", ctx)
                continue
            self.busy.add(w.key)
            self.pending[w.key] = need
            asyncio.get_running_loop().create_task(self._execute(w, sig, shares, ctx))

    # ---------------------------------------------------------------- maker entry (LM, paper)
    def _on_public_trade(self, token, side, price, size, ts_ms, rx):
        w = self.st.token_window.get(token)
        if w is not None:
            self.broker.on_public_trade(token, w.other, side, price, size, ts_ms)

    def _on_maker_fill(self, fill):
        key, q, px = fill["key"], float(fill["size"]), float(fill["price"])
        b = self.bids.get(key)
        e = {"side": fill["side"], "shares": round(q, 4), "price": round(px, 4), "cost": round(px * q, 6),
             "fee": 0.0, "t": time.time(), "limit": px, "p": b["p"] if b else None}
        self.entries.setdefault(key, []).append(e)
        if b and b["oid"] == fill["order_id"]:
            b["size"] -= q
            if b["size"] <= 1e-9:
                self.bids.pop(key, None)
        self.stats["filled"] += 1
        self._save_state()
        self.j.log("fill", key=key, side=fill["side"], price=round(px, 4), size=round(q, 4), ts=fill["ts"], fee=0.0,
                   order_id=fill["order_id"], mode=self.broker.mode)
        self.j.log("lock_entry", key=key, maker=True, price=round(px, 4), shares=round(q, 4))
        log.info("MAKER FILL %s %s %.2f sh @ %.2f", key, fill["side"], q, px)

    async def _cancel_bid(self, key, now, why):
        b = self.bids.pop(key, None)
        if b:
            await self.broker.cancel([b["oid"]], now)
            self.j.log("maker_cancel", key=key, why=why, price=b["price"], side=b["side"])

    def resting_cost(self, skip=None):
        return sum(b["price"] * b["size"] for k, b in self.bids.items() if k != skip)

    async def maker_once(self, now):
        """LM: while the rule holds, keep one bid for the decided side at maker_bid(); pull it the moment the rule (or a
        feed, or risk) fails. One lot of `shares` per window (partial fills count), held to resolution."""
        c = self.cfg.lock
        ok, why_risk = self.can_enter()
        for key, w in list(self.st.windows.items()):
            Tc = self.anchor(w.start)
            rel = now / 1000 - Tc
            have = self.filled(key)
            if not (c.rel_lo <= rel <= c.rel_hi) or have >= c.shares - 1e-9:
                await self._cancel_bid(key, now, "time" if have < c.shares - 1e-9 else "full")
                continue
            px = self.st.prices[w.asset]
            if now - self.st.feed_alive.get("binance", 0) > c.bn_max_age_ms:
                await self._cancel_bid(key, now, "bn_stale")
                self._why(key, "bn_stale")
                continue
            K = self.strike(key, w.asset, w.start)
            cl = px.cl_last
            side, p, ctx = decide_state(c, self.tables, w.asset, Tc, K, now, px.cl_by_sec, cl,
                                        self.sig[w.asset].sigma() if self.sig[w.asset].n >= c.min_sigma_prints
                                        else None, px.bn_mid(), px.bn_mid_at(cl[1] + 999) if cl else None,
                                        px.bn_mid_at(now - 1000), px.bn_mid_at(now - 3000),
                                        shock=lambda a=w.asset: self._shock(a, now))
            if side is None or not ok:
                why = ctx.get("why") if side is None else "risk:" + why_risk
                await self._cancel_bid(key, now, why)
                self._why(key, why, ctx)
                continue
            cur = self.bids.get(key)
            if cur and cur["side"] != side:
                await self._cancel_bid(key, now, "side_flip")
                cur = None
            token = w.token_up if side == "U" else w.token_down
            ask = self._book_ask(token)
            b = maker_bid(c, p, ask[0] if ask else None)
            if b is None:
                await self._cancel_bid(key, now, "bid_too_low")
                self._why(key, "bid_too_low", ctx)
                continue
            if cur and abs(cur["price"] - b) < 1e-9:
                continue                                   # already resting at the right price
            size = min(c.shares, math.floor(self.cfg.risk.max_usd_per_entry / b * 100) / 100) - have
            if size < max(w.min_size, 5) - 1e-9:         # exchange minimum: a partly filled lot is not topped up
                await self._cancel_bid(key, now, "min_size")
                continue
            if self.open_cost() + self.resting_cost(skip=key) + size * b > self.cfg.risk.max_open_usd + 1e-9:
                self.stats["skipped_risk"] += 1
                await self._cancel_bid(key, now, "risk:max_open")
                self._why(key, "risk:max_open", ctx)
                continue
            if cur:
                await self._cancel_bid(key, now, "requote")
            book = self.st.books.get(token)
            q_ahead = book.bids.get(b, 0.0) if book else 0.0
            oid = await self.broker.place(token, key, side, b, size, now, queue_ahead=q_ahead)
            self.bids[key] = {"oid": oid, "side": side, "price": b, "size": size, "token": token, "p": p}
            self.stats["signals"] += 1
            self.stats["orders"] += 1
            self._why(key, "bid", ctx)
            self.j.log("maker_bid", key=key, side=side, price=b, size=size, q_ahead=q_ahead,
                       ask=ask[0] if ask else None, **{k: v for k, v in ctx.items() if k != "side"})
        for key in [k for k in self.bids if k not in self.st.windows]:
            await self._cancel_bid(key, now, "window_gone")
        gc = getattr(self.broker, "gc", None)
        if gc:
            gc(now)

    def _why(self, key, why, ctx=None):
        if self.last_why.get(key) != why:
            self.last_why[key] = why
            if ctx and why not in ("time",):
                self.j.log("lock_state", key=key, why=why, **{k: v for k, v in ctx.items() if k != "why"})

    async def _execute(self, w, sig, shares, ctx):
        key = w.key
        token = w.token_up if sig["side"] == "U" else w.token_down
        # always on the 0.01 grid: valid whether the market tick is 0.01 or 0.001 (a 3-decimal cap on a 0.01 market
        # is rejected by the client); the limit is only a cap, fills happen at the resting asks
        limit = round(tick_floor(sig["limit"], 0.01), 2)
        t_sent = now_ms()
        self.stats["orders"] += 1
        err = False
        try:
            px, got, fee = await self.broker.buy(token, key, sig["side"], limit, shares)
        except Exception as e:  # noqa: BLE001
            self.j.log("order_error", key=key, error=str(e)[:300])
            px, got, fee, err = 0.0, 0.0, 0.0, True
        finally:
            self.busy.discard(key)
            self.pending.pop(key, None)
        # the decision context plus what was sent. One dict: ctx already has "side" and "limit" (the cap before the
        # 0.01 grid), and passing them again as keywords raised TypeError inside this task, so until 9 Nov no
        # lock_entry / lock_miss line was ever written (paper and live) and the miss rate was invisible
        rec = {**ctx, "key": key, "side": sig["side"], "limit": limit, "ms": now_ms() - t_sent}
        if got <= 1e-9:
            self.stats["missed"] += 1
            if err and self.broker.mode == "live":
                # a timeout / dropped reply may hide a matched order: never send a second one in this window
                # (worst case one unknown entry <= max_usd_per_entry; the wallet balance shows it)
                self.next_try[key] = float("inf")
                self.j.log("window_blocked", key=key, reason="order_error")
            else:
                self.next_try[key] = now_ms() + self.cfg.lock.retry_ms
            self.j.log("lock_miss", **{**rec, "error": err})
            return
        self.stats["filled"] += 1
        e = {"side": sig["side"], "shares": round(got, 4), "price": round(px, 4), "cost": round(px * got, 6),
             "fee": round(fee, 6), "t": time.time(), "limit": limit, "p": sig["p"]}
        self.entries.setdefault(key, []).append(e)
        self._save_state()
        self.j.log("fill", key=key, side=sig["side"], price=round(px, 4), size=round(got, 4), ts=now_ms(),
                   fee=round(fee, 6), order_id="fak", mode=self.broker.mode)
        self.j.log("lock_entry", **{**rec, "price": round(px, 4), "shares": round(got, 4)})
        log.info("ENTRY %s %s %.2f sh @ %.3f (p=%.4f ask=%.3f limit=%.3f rel=%.1f)", key, sig["side"], got, px,
                 sig["p"], sig["ask"], limit, ctx.get("rel", 0))

    # ---------------------------------------------------------------- settlement
    def _provisional(self, key):
        """Our own outcome once the closing prints are in (minutes before Gamma): count a loss at once and free the
        open-risk slot. Up to 3 dropped prints are forward-filled like the strike (RTDS drops ~1% of the seconds, so a
        third of the windows miss one: without the fill those windows had no provisional result and, with
        max_open_usd = 5, kept the next window blocked until Gamma settled). A filled TWAP closer than
        PROV_MIN_BP to the strike is too close to call: wait for Gamma."""
        if key in self.risk["provisional"] or key in self.risk["settled"]:
            return
        a, T = key.rsplit("-", 1)
        Tc = self.anchor(T)
        K = self.strikes.get(key)
        if K is None:
            return
        C, miss = close_from_prints(self.st.prices[a].cl_by_sec, Tc)
        if C is None or (miss and abs(C / K - 1) * 1e4 < PROV_MIN_BP):
            return
        up = C >= K
        pnl = 0.0
        for e in self.entries.get(key, []):
            won = (e["side"] == "U") == up
            pnl += (e["shares"] if won else 0.0) - e["cost"] - e["fee"]
        self.risk["provisional"][key] = round(pnl, 6)
        self.j.log("provisional", key=key, winner="Up" if up else "Down", pnl=round(pnl, 4), missing=miss)
        self._save_state()

    async def settle_loop(self):
        while self.alive() or self.entries:
            now = time.time()
            for key in list(self.entries):
                a, T = key.rsplit("-", 1)
                T = int(T)
                Tc = self.anchor(T)
                if now >= Tc + 302:
                    self._provisional(key)
                if now < Tc + 330:
                    continue
                m = self.meta.get(key)
                slug = m["slug"] if m else slug_for(a, T, self.tag)
                try:
                    e = await asyncio.to_thread(fetch_event, slug)
                    mm = parse_event(e) if e else None
                except Exception as ex:  # noqa: BLE001
                    log.warning("settle %s: %s", key, ex)
                    continue
                if not mm or not mm.get("winner"):
                    if now > Tc + 3600:
                        self.j.log("settle_timeout", key=key)
                    continue
                self._settle(key, mm["winner"])
            if not self.alive() and not self.entries:
                break
            await asyncio.sleep(10)

    def _settle(self, key, winner):
        es = self.entries.pop(key, [])
        up = sum(e["shares"] for e in es if e["side"] == "U")
        dn = sum(e["shares"] for e in es if e["side"] == "D")
        cost = sum(e["cost"] + e["fee"] for e in es)
        payoff = up if winner == "Up" else dn
        pnl = payoff - cost
        day = self.today(self.end_of(key))
        self.risk["realized_by_day"][day] = round(self.risk["realized_by_day"].get(day, 0.0) + pnl, 6)
        self.risk["settled"].append(key)
        self.risk["provisional"].pop(key, None)
        self._save_state()
        self.j.log("settle", key=key, winner=winner, up=round(up, 4), down=round(dn, 4), cost=round(cost, 4),
                   payoff=round(payoff, 4), rebate=0.0, pnl=round(pnl, 4))
        log.info("SETTLE %s %s pnl=%+.3f day=%+.2f", key, winner, pnl, self.day_pnl())
        ok, _ = self.can_enter()

    # ---------------------------------------------------------------- status / run
    async def status_loop(self):
        while self.alive():
            await asyncio.sleep(30)
            sg = {a: round((o.sigma() or 0) * 1e4, 3) for a, o in self.sig.items()}
            if self._cl_lags:
                xs = sorted(self._cl_lags)
                self.stats["cl_lag_ms"] = xs[len(xs) // 2]
                self._cl_lags = []
            msg = (f"day={self.day_pnl():+.2f} open=${self.open_cost():.2f} stats={self.stats} sigma_bp={sg} "
                   f"windows={sorted(self.st.windows)} killed={self.risk.get('killed_day') == self.today()}")
            log.info(msg)
            self.j.log("status", pnl=round(self.day_pnl(), 4), open_worst=round(self.open_cost(), 4),
                       stats=dict(self.stats), sigma_bp=sg)
            self.stats["lag_max_ms"] = self.stats["decide_max_ms"] = 0      # maxima per 30 s interval
            self.stats.pop("ping_ms", None)
            self.stats.pop("cl_lag_ms", None)
            self._save_state()

    async def run(self):
        await self.broker.start()
        self.j.log("start", mode=self.broker.mode, config=asdict(self.cfg))
        feed = self.cfg.execution.cl_feed
        cl_feeds = ([rtds_feed(self.st, self.alive)] if feed in ("rtds", "both") else []) + (
            [polybolt_feed(self.st, self.alive)] if feed in ("polybolt", "both") else [])
        tasks = [asyncio.create_task(x) for x in (
            self.discover_loop(), *cl_feeds, binance_feed(self.st, self.alive),
            self.decide_loop(), self.status_loop(), self.strike_check_loop(), self.lag_loop(),
            self.warm_loop())]
        tasks += [asyncio.create_task(f.run()) for f in self.feeds.values()]
        settle = asyncio.create_task(self.settle_loop())
        try:
            while self.alive():
                await asyncio.sleep(1)
                dead = [t for t in tasks if t.done() and t.exception()]
                if dead:
                    raise dead[0].exception()
        finally:
            self._stop = True
            for t in tasks:
                t.cancel()
            self._save_state()
            self.j.log("stopped", pnl=round(self.day_pnl(), 4), stats=dict(self.stats))
            try:
                await asyncio.wait_for(settle, timeout=900)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                settle.cancel()
            await self.broker.stop()
            self.j.log("final", pnl=round(self.day_pnl(), 4), open=len(self.entries))
