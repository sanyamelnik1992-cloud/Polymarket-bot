"""Мейкер ("fair-value market maker") on 5m Up/Down windows — PAPER ONLY (reports/89).

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
import logging
import math
import time
import tomllib
from dataclasses import dataclass, field

from ..lockmodel import TW_LO, early_z
from .feeds import ClobFeed
from .lock import LockBot, _apply, load_lock_config, tick_floor
from .paper import PaperBroker
from .state import now_ms

log = logging.getLogger("pmbot.maker")
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
    markout_s: list = field(default_factory=lambda: [5, 30])    # mm_markout: the side's fair value this many s after a fill


def load_maker_config(path=None, overrides=None):
    """[maker] -> MakerCfg; everything else is a LockConfig (zone "fair": the Binance fair value and its sigma)."""
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
    cfg = load_lock_config(None, overrides=data)
    c, e = cfg.lock, cfg.execution
    if e.mode != "paper":
        raise ValueError("the maker is paper only: there is no live maker broker for it (reports/89)")
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
    if not mm.markout_s or any(not (isinstance(h, int) and 1 <= h <= 120) for h in mm.markout_s):
        raise ValueError("maker.markout_s: whole seconds in [1, 120]")
    if cfg.risk.max_open_usd < 2 * mm.shares * mm.p_hi:
        raise ValueError("risk.max_open_usd must fit the two bids of a window")
    return cfg, mm


def discount(mm: MakerCfg, long_by):
    """How far under its reference a side is bid: half_spread, plus skew_c per `shares` this side is long, minus the same
    for each `shares` it is short (down to 0: a pair is completed at the reference at most). The classic inventory skew of a
    maker: long Down = Down bid lower and (an Up bid is a Down ask) Down offered cheaper."""
    k = mm.skew_c * abs(long_by) / mm.shares
    return mm.half_spread + k if long_by > 0 else mm.half_spread - min(mm.half_spread, k)


def bid_target(mm: MakerCfg, p, have, long_by, best_ask):
    """Price of our bid for a side whose reference price is p, or (None, why). have = shares of that side bought in the
    window, long_by = (this side - the other side) shares, best_ask = that side's best ask or None."""
    if not mm.p_lo <= p <= mm.p_hi:
        return None, "price"
    if have >= mm.max_side_shares - 1e-9:
        return None, "side_full"
    if long_by >= mm.max_unpaired - 1e-9:
        return None, "unpaired"
    b = tick_floor(p - discount(mm, long_by), 0.01)
    if best_ask is not None and b >= best_ask - 1e-9:
        b = tick_floor(best_ask - 0.01, 0.01)          # post-only: a bid at or over the ask would take it
    b = round(b, 2)
    if b < 0.01:
        return None, "too_low"
    return b, ""


def keep_bid(mm: MakerCfg, cur, tgt, p, long_by):
    """True when a bid resting at `cur` may stay although the target is `tgt`: lower targets move it at once (>= requote_c),
    higher ones only when the unrounded target is UP_HYST past cur + requote_c."""
    if tgt <= cur - mm.requote_c + 1e-9:
        return False
    if tgt < cur + mm.requote_c - 1e-9:
        return True
    return p - discount(mm, long_by) < cur + mm.requote_c + UP_HYST


class FairMaker(LockBot):
    """LockBot's windows, feeds, Binance fair value, journal, risk and settlement; its decision pass is the maker's."""

    def __init__(self, cfg, mm: MakerCfg, journal, broker=None):
        super().__init__(cfg, journal, broker=broker or type("_Unset", (), {"mode": "paper"})())
        self.mm = mm
        self.broker = broker or PaperBroker(cfg.execution.paper_place_ms, cfg.execution.paper_cancel_ms,
                                            on_fill=self._on_mm_fill, queue=True)
        # the public trades go to the paper broker (LockBot gave the fair rule's race log the feed)
        self.feeds = {a: ClobFeed(self.st, a, alive=self.alive, on_trade=self._on_public_trade) for a in cfg.lock.assets}
        self.mm_bids = {}          # (window key, side) -> {"oid", "price", "size", "token", "p", "t"}
        self.pause_until = {}      # asset -> ms: no bids until then (a Binance jump)
        self.marks = []            # pending markouts: (due ms, key, side, h, price, size, fair value at the fill)
        self._fv_t = {}            # window key -> ms of the last "fv" record (the hourly report's markout-60 reads the book mid)
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
    async def _mm_cancel(self, key, side, now, why):
        b = self.mm_bids.pop((key, side), None)
        if b:
            await self.broker.cancel([b["oid"]], now)
            self.stats["cancels"] += 1
            if why != "requote":
                self.j.log("mm_cancel", key=key, side=side, why=why, price=b["price"])

    async def _mm_pull(self, key, now, why):
        for side in ("U", "D"):
            await self._mm_cancel(key, side, now, why)
        self.last_why[key] = why

    async def decide_once(self, now):
        c, m = self.cfg.lock, self.mm
        ok, why_risk = self.can_enter()
        self._markouts(now)
        for key, w in list(self.st.windows.items()):
            rel = now / 1000.0 - self.anchor(w.start)
            if not c.rel_lo <= rel <= c.rel_hi:
                await self._mm_pull(key, now, "time")
                continue
            if not ok:
                await self._mm_pull(key, now, "risk:" + why_risk)
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
            for side, p, token, have, long_by in (("U", pu, w.token_up, up, up - dn), ("D", 1.0 - pu, w.token_down, dn, dn - up)):
                ask = self._book_ask(token)
                book = self.st.books.get(token)
                ref = p
                if m.ref == "min" and ask and book is not None and book.bids:
                    ref = min(p, (max(book.bids) + ask[0]) / 2)          # never above the side's book mid
                tgt, why = bid_target(m, ref, have, long_by, ask[0] if ask else None)
                cur = self.mm_bids.get((key, side))
                if tgt is None:
                    if cur:
                        await self._mm_cancel(key, side, now, why)
                    continue
                if cur and keep_bid(m, cur["price"], tgt, ref, long_by):
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
                    await self._mm_cancel(key, side, now, "requote")
                    self.stats["requotes"] += 1
                q_ahead = book.bids.get(tgt, 0.0) if book else 0.0
                oid = await self.broker.place(token, key, side, tgt, size, now, queue_ahead=q_ahead)
                self.mm_bids[(key, side)] = {"oid": oid, "price": tgt, "size": size, "token": token, "p": p, "t": now}
                self.stats["quotes"] += 1
                bb = book.best_bid() if book is not None and hasattr(book, "best_bid") else None
                self.j.log("mm_quote", key=key, side=side, price=tgt, size=size, p=round(p, 4), ref=round(ref, 4), q=round(q_ahead, 1),
                           bb=bb, ba=ask[0] if ask else None, rel=round(rel, 1), up=up, dn=dn, re=bool(cur))
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
                                                 "p": round(b["p"], 4) if b else None})
        now = now_ms()
        p = self._side_fair(key, side, now)
        up, dn = self.held(key)
        w = self.st.windows.get(key)
        mv = self.move_toward_bp(w.asset, side) if w else None
        self.stats["fills"] += 1
        self.stats["shares_up" if side == "U" else "shares_dn"] += q
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

    def _markouts(self, now):
        keep = []
        for mk in self.marks:
            due, key, side, h, px, q, p0 = mk
            if now < due:
                keep.append(mk)
                continue
            p1 = self._side_fair(key, side, now)
            self.j.log("mm_markout", key=key, side=side, h=h, price=round(px, 4), size=round(q, 4), p0=round(p0, 4),
                       p1=None if p1 is None else round(p1, 4), d=None if p1 is None else round(p1 - p0, 4))
        self.marks = keep
