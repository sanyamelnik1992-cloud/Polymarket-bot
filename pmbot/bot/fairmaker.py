"""Мейкер ("fair-value market maker") on 5m Up/Down windows — paper (reports/89) and a LIVE test at the exchange minimum
(reports/94: pmbot/bot/maker_live.py, config/maker_live.toml, only with execution.mode = "live" AND the --live flag).

Who makes the money on these markets (reports/89, 24 Nov): 26 of the week's top 60 crypto wallets are BTC 5m / 15m makers,
+$409k in 7 days on $17.2M of turnover (+2.4 %, of it 0.2-0.4 % exchange maker rebates), 83 % of their days positive. They
rest bids on BOTH sides all window long, $3-7 a fill, and are filled 0.6-2.4 c better than the Binance fair value; their fills
barely move against them in the next 5-30 s, except the ones right after a Binance jump against them. The exchange holds every
taker order 150 ms, long enough for a maker who sees the same Binance move to cancel. On all 1.36M BTC 5m taker trades of
18-21 Nov a maker filled h cents better than our fair value earned about h cents a share by the window's outcome.

This bot does the same on paper, with the Перехватчик's calibrated fair value (reports/83, report 89 section 5):
  fair_up = Phi(ln(S / K) / (sigma * sqrt(tau0 + 19.5)))   S = Binance mid, K = mean Binance mid of [T0-62, T0-3],
                                                            sigma = BlockSigma (10 s returns of the last hour, per 1 s),
                                                            tau0 = seconds to the closing TWAP
Every window of lock.assets, from window second lock.rel_lo to lock.rel_hi (<= 238, the closing TWAP), it keeps
  * a bid for Up at ref_up - half_spread and a bid for Down at ref_down - half_spread, `shares` each, on the 0.01 grid,
    post-only (one cent under that side's best ask when the target would cross it); ref = the fair value (maker.ref = "fair")
    or min(fair value, the side's book mid) (maker.ref = "min": the market's own price caps us);
  * a resting bid is moved when its target moved by >= requote_c;
  * both bids of a coin are pulled for pause_s after a Binance move of >= max(pull_bp, pull_sigmas * sigma * sqrt(pull_s))
    within pull_s seconds (a jump for the current volatility: the takers who come now know more);
  * inventory: at most max_side_shares bought per side and window; once |Up - Down| >= max_unpaired the long side gets no bid,
    below that its bid is skew_c lower and the short side's skew_c higher (up to its reference) per `shares` of imbalance;
  * no bid on a side whose fair value is outside [p_lo, p_hi].
Fills: PaperBroker with the queue model (pmbot/bot/paper.py): a public trade that passes through our price fills us, a trade at
our price first eats the size that rested there when we placed; an order is live paper_place_ms after the decision and gone
paper_cancel_ms after its cancel, compared on the exchange's match time (a taker order waits 150 ms at the exchange, so a cancel
that arrives inside that wait is in time). Each trade is reported once, on the taker's token (checked 24 Nov), and the Up / Down
books are mirrors (an Up bid at b is a Down ask at 1 - b), which the broker's "complement BUY at q = our bid at 1 - q" rule uses.
What paper cannot show: other makers do not see our paper bid, so nobody steps in front of it as they would in live.
Positions are held to resolution (an Up + Down pair pays $1). Makers pay no fee; the exchange's maker rebate is NOT counted.
Journal: mm_quote, mm_cancel, mm_pull, fill + mm_fill (the side's fair value and our edge at the fill, the Binance move of the
last 2 s toward our side), mm_markout (the side's fair value markout_s later), settle (from LockBot).
"""
import asyncio
import logging
import math
import os
import time
import tomllib
from dataclasses import dataclass, field

from ..lockmodel import TW_LO, early_z
from .feeds import ClobFeed
from .live import LiveGateError
from .lock import LockBot, _apply, load_lock_config, tick_floor
from .maker_live import MakerLiveBroker, MakerLiveCfg
from .makerreport import Interval, next_boundary
from .paper import PaperBroker
from .state import now_ms

log = logging.getLogger("pmbot.maker")
MIN_BID = 0.05             # the live broker refuses bids under 0.05 (MakerLiveBroker.PRICE_LO): paper does not quote them either
REPORT_COUNTERS = ("quotes", "requotes", "pulls", "guard", "lag_over_100ms", "skipped_risk")   # mm_report: by difference
REPORT_BROKER = ("placed", "rejected", "crossed", "cancel_late", "failed", "adopted", "reconciled")
UP_HYST = 0.003            # a resting bid is raised only when the raw target is this far past the next cent (a fair value that
                           # flickers around a cent boundary would re-place the bid back and forth and lose its queue place);
                           # it is lowered at once (that is the side where a stale bid gets picked off)


@dataclass
class MakerCfg:
    half_spread: float = 0.02            # each bid = reference price of its side - half_spread (then down to the 0.01 grid)
    ref: str = "fair"                    # reference price: "fair" = the Binance fair value; "min" = min(fair value, the side's book
                                         # mid): never bid above where the market values the side (local run 24 Nov: both losing
                                         # windows were fills where the model valued the side 3-4 c above the book and the book won)
    shares: float = 5.0                  # size of each bid (the exchange minimum is 5)
    max_side_shares: float = 25.0        # at most this many shares bought per side and window
    max_unpaired: float = 10.0           # |Up - Down| shares filled in a window: from here on no bid on the long side
    skew_c: float = 0.01                 # per `shares` of imbalance the long side's bid is this much lower and the short side's this
                                         # much higher (never above its reference: a pair is completed at the reference at most)
    requote_c: float = 0.01              # move a resting bid when its target moved by at least this
    pull_bp: float = 1.5                 # pull both bids of a coin when Binance moved max(pull_bp, pull_sigmas * sigma *
    pull_sigmas: float = 2.5             # sqrt(pull_s)) bp within pull_s seconds (sigma = the model's 1 s sigma): an unusual move for
    pull_s: float = 2.0                  # the current volatility. A fixed 1.5 bp pulled every 3-4 s at sigma 1.3 bp (24 Nov, local
                                         # run): the fair value's own risk over our reaction time does not grow with sigma
    pause_s: float = 2.0                 # and quote that coin again only this long after the last such move
    p_lo: float = 0.10                   # no bid on a side whose fair value is below p_lo or above p_hi (nearly decided:
    p_hi: float = 0.90                   # a 1-2 c spread is all the edge there and the tail risk is the whole bid)
    dog_spread: float = 0.0              # > 0: the discount of a side whose MODEL fair value is below fav_p (the underdog) instead of
    fav_p: float = 0.5                   # half_spread (reports/95: on 8 days of public trades a symmetric bid loses on the underdog
                                         # side, -2..-4 c a share, the model is too high there; 0.10 = it only catches dislocations)
    gate_bp: float = 0.0                 # > 0: no bid on a side while Binance moved AGAINST it by more than gate_bp over the last gate_s
    gate_s: float = 3.0                  # seconds (reports/95: fills after such a move are the toxic ones, the good ones follow a move
                                         # toward us); the other side keeps quoting. 0 = off
    markout_s: list = field(default_factory=lambda: [5, 30])    # mm_markout: the side's fair value this many s after a fill
    report_every_s: float = 1800.0       # one `mm_report` record (Telegram in live) every this many s, at wall-clock multiples; 0 = off


def _parse_maker(path=None, overrides=None):
    """[maker] -> MakerCfg, [live] -> a dict, everything else a LockConfig (zone "fair": the Binance fair value and its sigma).
    The lock loader's live rules are the Перехватчик's taker rules: the maker is checked as paper there and by its own rules
    here (load_maker_config / load_maker_live_config)."""
    data = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict):
            data[k] = {**data.get(k, {}), **v}
        else:
            data[k] = v
    mm = MakerCfg()
    _apply(mm, dict(data.pop("maker", {})), "maker.")
    live = dict(data.pop("live", {}) or {})
    ex = dict(data.get("execution") or {})
    mode = ex.get("mode", "paper")
    data["execution"] = {**ex, "mode": "paper"}
    cfg = load_lock_config(None, overrides=data)
    cfg.execution.mode = mode
    _check_maker(cfg, mm)
    return cfg, mm, live


def load_maker_config(path=None, overrides=None):
    """The paper maker: (LockConfig, MakerCfg). A live config is refused here (scripts/run_maker.py --live)."""
    cfg, mm, live = _parse_maker(path, overrides)
    if cfg.execution.mode != "paper":
        raise ValueError("this config is not paper: a live maker runs only through scripts/run_maker.py --live (reports/94)")
    if live:
        raise ValueError("[live] belongs to a live maker config")
    return cfg, mm


# The live maker is a TEST at the exchange minimum (reports/94): these caps cannot be raised from a config. Growing it into a
# bot is the owner's decision and a change of this code, reviewed like every real-money change.
LIVE_CAPS = {"shares": 5.0, "max_side_shares": 15.0, "max_unpaired": 10.0, "max_resting_usd": 10.0, "daily_loss_limit": 15.0,
             "max_open_usd": 40.0, "max_orders_hour": 5000, "min_reserve_usd": 40.0}


def load_maker_live_config(path, live_flag=False, overrides=None):
    """The LIVE maker test: (LockConfig, MakerCfg, MakerLiveCfg). Refuses a config that is not marked live, refuses to run it
    without the --live flag, and refuses limits above LIVE_CAPS."""
    cfg, mm, live = _parse_maker(path, overrides)
    if cfg.execution.mode != "live":
        raise ValueError("a live maker config needs execution.mode = \"live\" (paper runs without --live)")
    if not live_flag:
        raise LiveGateError("the config says live but the --live flag is missing: refusing to trade real money")
    lc = MakerLiveCfg()
    _apply(lc, live, "live.")
    c, r, k = cfg.lock, cfg.risk, LIVE_CAPS
    if c.assets != ["btc"]:
        raise ValueError("live maker: BTC only (the coin the paper maker was measured on)")
    if not (mm.shares == lc.max_shares == k["shares"] and mm.max_side_shares <= k["max_side_shares"]
            and mm.max_unpaired <= k["max_unpaired"]):
        raise ValueError(f"live maker: shares = live.max_shares = {k['shares']:g}, max_side_shares <= "
                         f"{k['max_side_shares']:g}, max_unpaired <= {k['max_unpaired']:g} (a test at the exchange minimum)")
    if not 2 * mm.shares * mm.p_hi <= lc.max_resting_usd <= k["max_resting_usd"]:
        raise ValueError(f"live maker: two bids (2 x shares x p_hi) <= live.max_resting_usd <= {k['max_resting_usd']:g}")
    if not (0 < r.daily_loss_limit <= k["daily_loss_limit"] and r.max_open_usd <= k["max_open_usd"]):
        raise ValueError(f"live maker: 0 < risk.daily_loss_limit <= {k['daily_loss_limit']:g}, risk.max_open_usd <= "
                         f"{k['max_open_usd']:g}")
    if r.resume_next_day or not r.kill_file or not r.state_file:
        raise ValueError("live maker: resume_next_day = false (a human restarts it), a kill file and a state file")
    if not (lc.reserve_usd >= k["min_reserve_usd"] and 1 <= lc.max_orders_hour <= k["max_orders_hour"]
            and 2 <= lc.heartbeat_s <= 6 and 181 <= lc.ttl_s <= 600 and 0 <= lc.cross_pause_ms <= 5000
            and 1 <= lc.reconcile_every_s <= 120):
        raise ValueError("live maker: reserve_usd >= 40, 1 <= max_orders_hour <= 5000, 2 <= heartbeat_s <= 6, "
                         "181 <= ttl_s <= 600, 0 <= cross_pause_ms <= 5000, 1 <= reconcile_every_s <= 120")
    return cfg, mm, lc


def _check_maker(cfg, mm):
    c = cfg.lock
    if c.zone != "fair" or c.entry != "taker" or c.duration != 300:
        raise ValueError("maker: lock.zone = 'fair' (the Binance fair value), lock.entry = 'taker' (unused) and 5m windows")
    if not 0 <= c.rel_lo < c.rel_hi <= 238:
        raise ValueError("maker: 0 <= lock.rel_lo < lock.rel_hi <= 238 (quotes end when the closing TWAP starts)")
    if not 0.005 <= mm.half_spread <= 0.2:
        raise ValueError("maker.half_spread must be in [0.005, 0.2]")
    if mm.ref not in ("fair", "min"):
        raise ValueError("maker.ref must be 'fair' or 'min'")
    if mm.shares < 5 or not float(mm.shares).is_integer() or not mm.shares <= mm.max_side_shares <= 200:
        raise ValueError("maker: whole shares >= 5 (exchange minimum) and shares <= max_side_shares <= 200")
    if not 0 < mm.max_unpaired <= mm.max_side_shares or not 0 <= mm.skew_c <= 0.1:
        raise ValueError("maker: 0 < max_unpaired <= max_side_shares, 0 <= skew_c <= 0.1")
    if not 0.01 <= mm.requote_c <= 0.1:
        raise ValueError("maker.requote_c must be in [0.01, 0.1]")
    if not (0 < mm.pull_bp <= 20 and 0 <= mm.pull_sigmas <= 10 and 0.2 <= mm.pull_s <= 30 and 0 <= mm.pause_s <= 60):
        raise ValueError("maker: 0 < pull_bp <= 20, 0 <= pull_sigmas <= 10, 0.2 <= pull_s <= 30, 0 <= pause_s <= 60")
    if not 0 < mm.p_lo < mm.p_hi < 1:
        raise ValueError("maker: 0 < p_lo < p_hi < 1")
    if not (0 <= mm.dog_spread <= 0.5 and 0.3 <= mm.fav_p <= 0.7 and 0 <= mm.gate_bp <= 10 and 1 <= mm.gate_s <= 30):
        raise ValueError("maker: 0 <= dog_spread <= 0.5, 0.3 <= fav_p <= 0.7, 0 <= gate_bp <= 10, 1 <= gate_s <= 30")
    if not mm.markout_s or any(not (isinstance(h, int) and 1 <= h <= 120) for h in mm.markout_s):
        raise ValueError("maker.markout_s: whole seconds in [1, 120]")
    if not (mm.report_every_s == 0 or 300 <= mm.report_every_s <= 7200):
        raise ValueError("maker.report_every_s: 0 (off) or 300..7200 seconds")
    if cfg.risk.max_open_usd < 2 * mm.shares * mm.p_hi:
        raise ValueError("risk.max_open_usd must fit the two bids of a window")
    return cfg, mm


def base_spread(mm: MakerCfg, fav=True):
    """The side's own half spread: half_spread, or dog_spread for the underdog (fav = the model's fair value >= fav_p)."""
    return mm.half_spread if fav or mm.dog_spread <= 0 else mm.dog_spread


def discount(mm: MakerCfg, long_by, fav=True):
    """How far under its reference a side is bid: its base spread, plus skew_c per `shares` this side is long, minus the same
    for each `shares` it is short (down to 0: a pair is completed at the reference at most). The classic inventory skew of a
    maker: long Down = Down bid lower and (an Up bid is a Down ask) Down offered cheaper."""
    base = base_spread(mm, fav)
    k = mm.skew_c * abs(long_by) / mm.shares
    return base + k if long_by > 0 else base - min(base, k)


def bid_target(mm: MakerCfg, p, have, long_by, best_ask, fav=True):
    """Price of our bid for a side whose reference price is p, or (None, why). have = shares of that side bought in the
    window, long_by = (this side - the other side) shares, best_ask = that side's best ask or None, fav = the side is the
    favourite (its base spread is half_spread, the underdog's dog_spread)."""
    if not mm.p_lo <= p <= mm.p_hi:
        return None, "price"
    if have >= mm.max_side_shares - 1e-9:
        return None, "side_full"
    if long_by >= mm.max_unpaired - 1e-9:
        return None, "unpaired"
    b = tick_floor(p - discount(mm, long_by, fav), 0.01)
    if best_ask is not None and b >= best_ask - 1e-9:
        b = tick_floor(best_ask - 0.01, 0.01)          # post-only: a bid at or over the ask would take it
    b = round(b, 2)
    if b < MIN_BID:
        return None, "too_low"
    return b, ""


def keep_bid(mm: MakerCfg, cur, tgt, p, long_by, fav=True):
    """True when a bid resting at `cur` may stay although the target is `tgt`: lower targets move it at once (>= requote_c),
    higher ones only when the unrounded target is UP_HYST past cur + requote_c."""
    if tgt <= cur - mm.requote_c + 1e-9:
        return False
    if tgt < cur + mm.requote_c - 1e-9:
        return True
    return p - discount(mm, long_by, fav) < cur + mm.requote_c + UP_HYST


class FairMaker(LockBot):
    """LockBot's windows, feeds, Binance fair value, journal, risk and settlement; its decision pass is the maker's."""

    def __init__(self, cfg, mm: MakerCfg, journal, broker=None, live: MakerLiveCfg = None):
        super().__init__(cfg, journal, broker=broker or type("_Unset", (), {"mode": "paper"})())
        self.mm = mm
        if broker is not None:
            self.broker = broker
        elif cfg.execution.mode == "live":       # load_maker_live_config only (the --live flag and the caps were checked there)
            if live is None:
                raise LiveGateError("a live maker needs its [live] limits (load_maker_live_config)")
            self.broker = MakerLiveBroker(live, on_fill=self._on_mm_fill, on_unfill=self._on_mm_unfill, journal=journal)
        else:
            self.broker = PaperBroker(cfg.execution.paper_place_ms, cfg.execution.paper_cancel_ms,
                                      on_fill=self._on_mm_fill, queue=True)
        self.live = self.broker.mode == "live"
        from dataclasses import asdict
        journal.log("mm_config", maker=asdict(mm), live=asdict(live) if live is not None else None, mode=self.broker.mode)
        # the public trades go to the paper broker (LockBot gave the fair rule's race log the feed)
        self.feeds = {a: ClobFeed(self.st, a, alive=self.alive, on_trade=self._on_public_trade) for a in cfg.lock.assets}
        self.mm_bids = {}          # (window key, side) -> {"oid", "price", "size", "token", "p", "t"}
        self.pause_until = {}      # asset -> ms: no bids until then (a Binance jump)
        self._guard_busy = {}      # asset -> an urgent guard task is cancelling (live)
        self._guard_mid = {}       # asset -> the Binance mid of the guard's last look (it looks again after a 0.1 bp move)
        self.marks = []            # pending markouts: (due ms, key, side, h, price, size, fair value at the fill)
        self._fv_t = {}            # window key -> ms of the last "fv" record (the hourly report's markout-60 reads the book mid)
        self.iv = Interval(time.time())    # the report interval in progress (pmbot/bot/makerreport.py)
        self._rep_prev = {}        # cumulative counters at the last report (the report carries the difference)
        self.stats.update(quotes=0, requotes=0, cancels=0, pulls=0, fills=0, shares_up=0.0, shares_dn=0.0)

    # ---------------------------------------------------------------- positions and risk
    def held(self, key):
        es = self.entries.get(key, [])
        return (sum(e["shares"] for e in es if e["side"] == "U"), sum(e["shares"] for e in es if e["side"] == "D"))

    def worst(self, key):
        """The most this window can lose: what we paid minus the payout of the side we hold less of."""
        es = self.entries.get(key, [])
        up, dn = self.held(key)
        return max(0.0, sum(e["cost"] + e["fee"] for e in es) - min(up, dn))

    def open_cost(self):
        """Worst case of the windows whose outcome is not known yet (LockBot's status reports it as open_worst)."""
        prov = self.risk["provisional"]
        return sum(self.worst(k) for k, es in self.entries.items() if k not in prov and es)

    def exposure(self, skip=None):
        """Open worst case + every resting bid as if it filled (the risk.max_open_usd check)."""
        return self.open_cost() + sum(b["price"] * b["size"] for k, b in self.mm_bids.items() if k != skip)

    # ---------------------------------------------------------------- model
    def fair_up(self, key, w, now):
        """(fair value of Up, Binance mid) or (None, why)."""
        c = self.cfg.lock
        K = self.bn_strike(key, w.asset, w.start)
        if not K:
            return None, "no_strike"
        sg = self.bsig[w.asset].sigma(c.fair_min_sigma_s)
        if not sg:
            return None, "no_sigma"
        S = self.sig_px(w.asset).bn_mid()
        if not S:
            return None, "no_binance"
        tau0 = (self.anchor(w.start) + TW_LO) - now / 1000.0
        if tau0 <= 0:
            return None, "time"
        z = early_z(S, K, sg, tau0)
        return 0.5 * math.erfc(-z / math.sqrt(2)), S

    def pull_limit_bp(self, asset):
        m = self.mm
        sg = self.bsig[asset].sigma(self.cfg.lock.fair_min_sigma_s) if asset in self.bsig else None
        return max(m.pull_bp, m.pull_sigmas * (sg or 0.0) * 1e4 * math.sqrt(m.pull_s))

    def jump_bp(self, asset, now):
        px = self.sig_px(asset)
        a, b = px.bn_mid(), px.bn_mid_at(now - int(self.mm.pull_s * 1000))
        return abs(math.log(a / b)) * 1e4 if a and b else 0.0

    def move_toward_bp(self, asset, side, ms=2000):
        """Binance move of the last `ms` toward the side (bp): > 0 = the side became worth more."""
        px = self.sig_px(asset)
        a, b = px.bn_mid(), px.bn_mid_at(now_ms() - ms)
        if not a or not b:
            return None
        r = math.log(a / b) * 1e4
        return r if side == "U" else -r

    # ---------------------------------------------------------------- orders
    async def _cancel_sides(self, key, sides, now, why):
        """Cancel our bids of a window's sides in ONE call (a Binance jump pulls both at once: in live every call is a
        round trip). Returns (ok, late): ok = False when the cancel itself failed (the bids are kept and tried again next
        pass); late = a bid was matched before the cancel reached the exchange (its fill is counted already: no re-bid now)."""
        bs = []
        for s in sides:                                # CLAIM the bids first: the urgent guard (below) and the decision pass may both
            b = self.mm_bids.pop((key, s), None)       # cancel the same side; the second one finds nothing to do
            if b:
                bs.append((s, b))
        if not bs:
            return True, False
        try:
            late = await self.broker.cancel([b["oid"] for _, b in bs], now)
        except Exception as e:  # noqa: BLE001  (live: still resting as far as we know, the next pass cancels again)
            for s, b in bs:
                self.mm_bids.setdefault((key, s), b)
            self.j.log("error", where="mm_cancel", error=str(e)[:200], key=key, why=why)
            return False, True
        for s, b in bs:
            self.stats["cancels"] += 1
            self._note("cancel", why)
            if why != "requote":
                self.j.log("mm_cancel", key=key, side=s, why=why, price=b["price"])
        return True, bool(late)

    async def _mm_cancel(self, key, side, now, why):
        return await self._cancel_sides(key, (side,), now, why)

    async def _mm_pull(self, key, now, why):
        await self._cancel_sides(key, ("U", "D"), now, why)
        self.last_why[key] = why

    async def _on_stopping(self):
        """LockBot.run: the bot stops (Ctrl+C, SIGTERM, the day stop): pull every bid NOW, not after the settle wait."""
        for key in sorted({k for k, _ in self.mm_bids}):
            await self._mm_pull(key, now_ms(), "stop")
        q = getattr(self.broker, "quiesce", None)
        if q:
            await q("stop")

    def _mid_wo_own(self, token, other):
        """The side's book mid without our own live bids (an Up bid at b also shows as a Down ask at 1 - b): with ref "min"
        our own bid inside the spread would raise the mid it is priced from, a step every requote (the G ratchet, CLAUDE.md).
        Paper bids are not in the public book, so paper reads the book as it is."""
        book = self.st.books.get(token)
        if book is None or not book.bids or not book.asks:
            return None
        own_b, own_a = {}, {}
        if self.live:
            for b in self.mm_bids.values():
                if b["token"] == token:
                    own_b[round(b["price"], 2)] = own_b.get(round(b["price"], 2), 0.0) + b["size"]
                elif b["token"] == other:
                    q = round(1.0 - b["price"], 2)
                    own_a[q] = own_a.get(q, 0.0) + b["size"]
        bb = next((p for p in sorted(book.bids, reverse=True) if book.bids[p] - own_b.get(round(p, 2), 0.0) > 1e-9), None)
        ba = next((p for p in sorted(book.asks) if book.asks[p] - own_a.get(round(p, 2), 0.0) > 1e-9), None)
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    def gate_blocks(self, asset, side):
        """maker.gate_bp > 0: Binance moved against this side by more than gate_bp over the last gate_s seconds -> no bid on it
        (the takers who come now know more; reports/95: after such a move the fills lose, after a move toward the side they
        earn most). The other side keeps quoting."""
        m = self.mm
        if m.gate_bp <= 0:
            return False
        mv = self.move_toward_bp(asset, side, int(m.gate_s * 1000))
        return mv is not None and mv < -m.gate_bp

    # ---------------------------------------------------------------- the urgent guard (live)
    def _on_tick(self, asset):
        """Live: a Binance tick that makes a resting bid dangerous pulls it NOW, from the feed's own callback, instead of waiting for
        the decision pass to finish its place / cancel calls (each ~50-250 ms at the exchange). Paper needs none: its calls are
        instant. The pass applies the same predicates (jump pull, gate), so the two agree."""
        super()._on_tick(asset)
        if not self.live or not self.mm_bids or self._guard_busy.get(asset):
            return
        try:
            mid = self.sig_px(asset).bn_mid()
            ref = self._guard_mid.get(asset)
            if not mid or (ref is not None and abs(mid / ref - 1.0) < 1e-5):
                return                              # bookTicker sends hundreds of updates a second: look again after a 0.1 bp move
            self._guard_mid[asset] = mid
            now = now_ms()
            m = self.mm
            mine = [k for k in self.mm_bids if k[0].rsplit("-", 1)[0] == asset]
            if not mine:
                return
            jump, lim = self.jump_bp(asset, now), self.pull_limit_bp(asset)
            if jump >= lim:
                sides = ("U", "D")
            else:
                sides = tuple(sd for sd in ("U", "D") if any(k[1] == sd for k in mine) and self.gate_blocks(asset, sd))
                jump = 0.0
            if sides:
                self._guard_busy[asset] = True
                asyncio.get_running_loop().create_task(self._guard(asset, sides, jump, lim))
        except Exception as e:  # noqa: BLE001  (inside the Binance feed: never raises)
            log.warning("guard %s: %s", asset, e)

    async def _guard(self, asset, sides, jump, lim):
        try:
            now = now_ms()
            if jump:
                if now >= self.pause_until.get(asset, 0):
                    self.stats["pulls"] += 1
                    self.j.log("mm_pull", asset=asset, key="*", move_bp=round(jump, 3), limit_bp=round(lim, 3), guard=True)
                self.pause_until[asset] = now + int(self.mm.pause_s * 1000)
            for key in sorted({k[0] for k in self.mm_bids if k[0].rsplit("-", 1)[0] == asset}):
                await self._cancel_sides(key, sides, now_ms(), "pause" if jump else "gate")
                self.stats["guard"] = self.stats.get("guard", 0) + 1
        except Exception as e:  # noqa: BLE001
            self.j.log("error", where="guard", error=str(e)[:200])
        finally:
            self._guard_busy[asset] = False

    async def decide_once(self, now):
        c, m = self.cfg.lock, self.mm
        ok, why_risk = self.can_enter()
        health = getattr(self.broker, "health", None)
        b_ok, b_why = health() if health else (True, "")
        bstats = getattr(self.broker, "stats", None)
        if bstats:
            self.stats["ex"] = dict(bstats)          # live: orders, fills, latency of the exchange calls (status every 30 s)
        if b_why == "broker_fatal":                  # 401 / 403 or the order-rate cap: stop the test (a human restarts it)
            self._kill(f"broker: {getattr(self.broker, 'fatal', '')}"[:200])
        self._markouts(now)
        for key, w in list(self.st.windows.items()):
            rel = now / 1000.0 - self.anchor(w.start)
            if not c.rel_lo <= rel <= c.rel_hi:
                await self._mm_pull(key, now, "time")
                continue
            if not ok:
                await self._mm_pull(key, now, "risk:" + why_risk)
                continue
            if not b_ok:                              # live: stream / heartbeat / API trouble, an order of unknown outcome
                await self._mm_pull(key, now, "broker:" + b_why)
                continue
            if now - self.st.feed_alive.get("binance", 0) > c.bn_max_age_ms:
                await self._mm_pull(key, now, "bn_stale")
                continue
            if now - self.st.feed_alive["clob"].get(w.asset, 0) > c.book_max_age_ms:
                await self._mm_pull(key, now, "book_stale")
                continue
            pu, S = self.fair_up(key, w, now)
            if pu is None:
                await self._mm_pull(key, now, S)
                continue
            self.strike(key, w.asset, w.start)            # the Chainlink strike: provisional result right after the window
            if now - self._fv_t.get(key, 0) >= 5000:      # ops.quality: markout-60 of the fills against the book mid
                bu = self.st.books.get(w.token_up)
                if bu is not None and bu.bids and bu.asks:
                    self._fv_t[key] = now
                    self.j.log("fv", key=key, mid=round((max(bu.bids) + min(bu.asks)) / 2, 4), p=round(pu, 4))
            self._react_mid[w.asset] = S                  # the next fair_react_bp move of Binance wakes the loop again
            jump = self.jump_bp(w.asset, now)
            lim = self.pull_limit_bp(w.asset)
            if jump >= lim:
                if now >= self.pause_until.get(w.asset, 0):
                    self.stats["pulls"] += 1
                    self.j.log("mm_pull", asset=w.asset, key=key, move_bp=round(jump, 3), limit_bp=round(lim, 3), rel=round(rel, 1))
                self.pause_until[w.asset] = now + int(m.pause_s * 1000)
            if now < self.pause_until.get(w.asset, 0):
                await self._mm_pull(key, now, "pause")
                continue
            up, dn = self.held(key)
            for side, p, token, other, have, long_by in (("U", pu, w.token_up, w.token_down, up, up - dn),
                                                         ("D", 1.0 - pu, w.token_down, w.token_up, dn, dn - up)):
                ask = self._book_ask(token)
                book = self.st.books.get(token)
                ref = p
                if m.ref == "min" and ask:
                    mid = self._mid_wo_own(token, other)
                    if mid is not None:
                        ref = min(p, mid)                                 # never above the side's book mid
                fav = p >= m.fav_p                                   # the MODEL's favourite (not the min() reference)
                tgt, why = bid_target(m, ref, have, long_by, ask[0] if ask else None, fav)
                if tgt is not None and self.gate_blocks(w.asset, side):
                    tgt, why = None, "gate"
                cur = self.mm_bids.get((key, side))
                resting = getattr(self.broker, "is_resting", None)
                if cur and resting and not resting(cur["oid"]):   # live: gone at the exchange (a heartbeat lapse, expiry)
                    self.mm_bids.pop((key, side), None)
                    self.j.log("mm_cancel", key=key, side=side, why="gone", price=cur["price"])
                    cur = None
                if tgt is None:
                    if cur:
                        await self._mm_cancel(key, side, now, why)
                    continue
                if cur and keep_bid(m, cur["price"], tgt, ref, long_by, fav):
                    continue                              # resting close enough to the target
                size = min(m.shares, math.floor(m.max_side_shares - have + 1e-9))
                if size < 5:
                    if cur:
                        await self._mm_cancel(key, side, now, "side_full")
                    continue
                if self.exposure(skip=(key, side)) + tgt * size > self.cfg.risk.max_open_usd + 1e-9:
                    self.stats["skipped_risk"] += 1
                    if cur:
                        await self._mm_cancel(key, side, now, "risk:max_open")
                    continue
                if cur:
                    done, late = await self._mm_cancel(key, side, now, "requote")
                    self.stats["requotes"] += 1
                    if late:                              # live: filled while we moved it, or the cancel failed: decide again
                        continue                          # next pass with the new position (paper never gets here)
                q_ahead = book.bids.get(tgt, 0.0) if book else 0.0
                oid = await self.broker.place(token, key, side, tgt, size, now, queue_ahead=q_ahead, end_s=w.end)
                if oid is None:                           # live: refused by our limits or the exchange, nothing rests
                    continue
                self.mm_bids[(key, side)] = {"oid": oid, "price": tgt, "size": size, "token": token, "p": p, "t": now}
                self.stats["quotes"] += 1
                self._note("quote", key)
                bb = book.best_bid() if book is not None and hasattr(book, "best_bid") else None
                self.j.log("mm_quote", key=key, side=side, price=tgt, size=size, p=round(p, 4), ref=round(ref, 4), q=round(q_ahead, 1),
                           bb=bb, ba=ask[0] if ask else None, rel=round(rel, 1), up=up, dn=dn, re=bool(cur), fav=bool(fav))
        for key, side in [k for k in self.mm_bids if k[0] not in self.st.windows]:
            await self._mm_cancel(key, side, now, "window_gone")
        gc_ = getattr(self.broker, "gc", None)
        if gc_:
            gc_(now)

    # ---------------------------------------------------------------- fills and markouts
    def _side_fair(self, key, side, now):
        w = self.st.windows.get(key)
        if w is None:
            return None
        pu, _ = self.fair_up(key, w, now)
        if pu is None:
            return None
        return pu if side == "U" else 1.0 - pu

    def _on_mm_fill(self, fill):
        key, side, q, px = fill["key"], fill["side"], float(fill["size"]), float(fill["price"])
        b = self.mm_bids.get((key, side))
        if b and b["oid"] == fill["order_id"]:
            b["size"] -= q
            if b["size"] <= 1e-9:
                self.mm_bids.pop((key, side), None)
        self.entries.setdefault(key, []).append({"side": side, "shares": round(q, 4), "price": round(px, 4),
                                                 "cost": round(px * q, 6), "fee": 0.0, "t": time.time(), "limit": px,
                                                 "p": round(b["p"], 4) if b else None, "oid": fill["order_id"]})
        now = now_ms()
        p = self._side_fair(key, side, now)
        up, dn = self.held(key)
        w = self.st.windows.get(key)
        mv = self.move_toward_bp(w.asset, side) if w else None
        self.stats["fills"] += 1
        self.stats["shares_up" if side == "U" else "shares_dn"] += q
        self._note("fill", key, side, px, q, None if b is None else b["p"] >= self.mm.fav_p, None if p is None else p - px)
        self._save_state()
        self.j.log("fill", key=key, side=side, price=round(px, 4), size=round(q, 4), ts=fill["ts"], fee=0.0,
                   order_id=fill["order_id"], mode=self.broker.mode)
        self.j.log("mm_fill", key=key, side=side, price=round(px, 4), size=round(q, 4), p=None if p is None else round(p, 4),
                   edge=None if p is None else round(p - px, 4), p_quote=round(b["p"], 4) if b else None,
                   rel=round(now / 1000.0 - self.anchor(w.start), 1) if w else None, up=round(up, 4), dn=round(dn, 4),
                   bn2=None if mv is None else round(mv, 3), lag_ms=now - int(fill["ts"]))
        if p is not None:
            for h in self.mm.markout_s:
                self.marks.append((now + h * 1000, key, side, h, px, q, p))

    def _on_mm_unfill(self, rec):
        """Live: a trade we counted FAILED on chain (the exchange takes it back): remove those shares of that order."""
        key, left = rec["key"], float(rec["size"])
        es = self.entries.get(key, [])
        for e in reversed(es):
            if left <= 1e-9:
                break
            if e.get("oid") != rec["order_id"]:
                continue
            q = min(e["shares"], left)
            e["shares"] = round(e["shares"] - q, 4)
            e["cost"] = round(e["price"] * e["shares"], 6)
            left -= q
        self.entries[key] = [e for e in es if e["shares"] > 1e-9]
        if not self.entries[key]:
            self.entries.pop(key, None)
        self._save_state()
        self.j.log("mm_unfill", key=key, side=rec.get("side"), order_id=rec["order_id"], size=round(float(rec["size"]), 4),
                   not_found=round(left, 4))

    def _markouts(self, now):
        keep = []
        for mk in self.marks:
            due, key, side, h, px, q, p0 = mk
            if now < due:
                keep.append(mk)
                continue
            p1 = self._side_fair(key, side, now)
            self._note("markout", h, None if p1 is None else p1 - p0, q)
            self.j.log("mm_markout", key=key, side=side, h=h, price=round(px, 4), size=round(q, 4), p0=round(p0, 4),
                       p1=None if p1 is None else round(p1, 4), d=None if p1 is None else round(p1 - p0, 4))
        self.marks = keep

    # ---------------------------------------------------------------- the periodic report (reports/95 section 10)
    def _note(self, what, *args):
        """Feed the report's interval. The report is only a view: a fault in it must never reach the order, fill or settle
        bookkeeping it is called from (real money), so it is logged and dropped."""
        try:
            getattr(self.iv, what)(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("report %s: %s", what, e)

    def _settle(self, key, winner):
        """LockBot's settlement, plus the closed window for the report (result and whether we held both sides)."""
        es = list(self.entries.get(key, []))
        super()._settle(key, winner)
        if es:
            up = sum(e["shares"] for e in es if e["side"] == "U")
            dn = sum(e["shares"] for e in es if e["side"] == "D")
            self._note("settle", (up if winner == "Up" else dn) - sum(e["cost"] + e["fee"] for e in es), up, dn)

    def extra_loops(self):
        return [self.report_loop()] if self.mm.report_every_s > 0 else []

    async def report_loop(self, clock=time.time, sleep=asyncio.sleep):
        """One `mm_report` every maker.report_every_s seconds, at wall-clock multiples (:00 and :30 for 1800). A report that
        would cover less than a third of the period (the bot was just restarted: every deploy restarts it) is skipped and its
        counters go on into the next one."""
        every = float(self.mm.report_every_s)
        while self.alive():
            now = clock()
            target = next_boundary(now, every)
            await sleep(max(0.5, target - now))
            if not self.alive():
                break
            t = clock()
            if t < target:                    # woken a few ms early (the sleep runs on the monotonic clock): the report is stamped :00 / :30
                await sleep(target - t + 0.01)
                if not self.alive():
                    break
                t = max(clock(), target)
            if t - self.iv.t0 < every / 3:
                continue
            try:
                self.report_once(t)
            except Exception as e:  # noqa: BLE001  (a report must never stop the maker)
                log.warning("report: %s", e)
                self.j.log("error", where="mm_report", error=str(e)[:200])

    def report_state(self):
        """'ok', or why the maker is not quoting now. No side effects (can_enter() may stop the bot)."""
        r = self.cfg.risk
        if r.kill_file and os.path.exists(r.kill_file):
            return "kill_file"
        if self.risk.get("killed_day") == self.today() or self.day_pnl() <= -r.daily_loss_limit:
            return "killed"
        health = getattr(self.broker, "health", None)
        ok, why = health() if health else (True, "")
        return "ok" if ok else why

    def report_record(self, now):
        """The `mm_report` record of the interval that ends at `now` (wall clock, s); the next interval starts here."""
        iv = self.iv
        bs = getattr(self.broker, "stats", None) or {}
        cur = {k: self.stats.get(k, 0) for k in REPORT_COUNTERS}
        cur.update({"b_" + k: bs.get(k, 0) for k in REPORT_BROKER})
        d = {k: cur[k] - self._rep_prev.get(k, 0) for k in cur}
        rec = iv.summary()
        rec.update(mode=self.broker.mode, t0=round(iv.t0, 1), t1=round(now, 1), minutes=round((now - iv.t0) / 60),
                   state=self.report_state(), quotes=d["quotes"], requotes=d["requotes"], pulls=d["pulls"], guard=d["guard"],
                   lag_over_100ms=d["lag_over_100ms"], skipped_risk=d["skipped_risk"])
        prov = self.risk["provisional"]
        open_keys = [k for k, es in self.entries.items() if es and k not in prov]
        held = [self.held(k) for k in open_keys]
        rec["open"] = {"windows": len(open_keys), "up": round(sum(h[0] for h in held), 4), "dn": round(sum(h[1] for h in held), 4),
                       "worst": round(self.open_cost(), 4), "bids": len(self.mm_bids)}
        by_day = self.risk["realized_by_day"]
        rec.update(day=round(self.day_pnl(), 4), day_limit=self.cfg.risk.daily_loss_limit, days=len(by_day),
                   total=round(sum(by_day.values()) + sum(v for k, v in prov.items() if k not in self.risk["settled"]), 4))
        if self.live:
            rec["broker"] = {k: d["b_" + k] for k in REPORT_BROKER}
            lat = getattr(self.broker, "latency_report", None)
            rec["lat"] = lat() if lat else {}
            rec["balance"] = getattr(self.broker, "balance_usd", None)
        self.iv, self._rep_prev = Interval(now), cur
        return rec

    def report_once(self, now):
        rec = self.report_record(now)
        self.j.log("mm_report", **rec)
        return rec
