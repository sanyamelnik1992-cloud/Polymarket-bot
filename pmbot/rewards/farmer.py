"""Liquidity-rewards farmer (paper): selection -> two-sided quotes -> simulated fills -> hedge -> estimated rewards.

Per market we rest a YES bid at ref - d and a NO bid at 1 - (ref + d) (= a YES ask at ref + d), size = the market's
rewards min size. ref = the size-cutoff-adjusted book mid when others quote near the price, else the last trade.
The rule follows what the largest reward earners do (reports/63: public wallet data of 498 "pure farmers"):

  * distance d: `dist_cents` when other makers score in the reward zone, `alone_frac` of the max spread when nobody
    does (a lone maker takes the whole pool at any score, so it stays as far out as still counts);
  * an order scores only after it has rested `min_age` seconds (market field r.moas, 30 s on most markets), so a
    resting order is kept while the target moved by at most `requote_ticks` and it still sits inside the zone;
  * fills: PaperBroker on the public trade stream with the queue model (a print through our price fills us, a print
    AT our price first uses up the size that rested there before us);
  * hedge: `hedge_latency_ms` after a fill we buy the other outcome from the live book if the pair then costs at most
    1 + `max_hedge_cost` a share (taker fee included) and merge the pair back to $1: a small certain loss instead of
    an open position. An exit that is too expensive is retried on every pass; until then the position is inventory;
  * a material fill (>= min_fill_shares) pulls all quotes in that market for `cooldown_min`, then re-centres; after
    `max_fills_per_market_day` material fills the market rests until 00:00 UTC; a side stops being bid once its
    unhedged inventory reaches `max_inventory_usd_per_market`. Counters survive restarts;
  * quotes are only live while the market feed is alive (no websocket message for `feed_stale_s` -> pull everything);
    a quote that would cross the book is replaced at once;
  * rewards: every sample we score our resting orders (old enough to count) against the live book with Polymarket's
    formula (pmbot.rewards.scoring) and credit rate * share * elapsed / 1 day. Tracked next to it: the same with
    the share from Polymarket's own competitiveness number ("official"), the part earned while nobody else scored
    ("alone": the least certain part), and the "stress" figure, i.e. the share if one more identical maker joined.
    All are ESTIMATES -- the real payout is decided by Polymarket and can only be confirmed live;
  * settlement: when Polymarket closes a market (Gamma `closed`, outcomePrices 1/0) the position is paid out and its
    PnL is realised; until then it is marked at the last mid;
  * trading PnL = realised (hedges, settlements) + mark-to-market of open positions. The daily loss stop uses it.
"""
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone

from ..bot.paper import PaperBroker
from ..config import GAMMA
from ..http import get_json
from . import markets as mk
from .feed import BookFeed
from .scoring import adjusted_mid, official_share, our_share, plan_quotes, walk_asks, zone_scores

log = logging.getLogger("pmbot.rewards")
SETTLE_GRACE_S = 3600          # look for a resolution at most this long after the market's end (then keep polling)
ZONE_MARGIN = 0.25             # cents: a resting order closer than this to the edge of the zone is moved in


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def next_midnight(ts):
    return (int(ts) // 86400 + 1) * 86400


class Position:
    """Shares of both outcomes of one market and what each side cost (taker fees of hedges included)."""

    def __init__(self, yes=0.0, no=0.0, cost=0.0, cy=None, cn=None):
        self.yes, self.no = yes, no
        if cy is None and cn is None:              # one total (older state files, tests): split by shares
            tot = yes + no
            cy = cost * yes / tot if tot else 0.0
            cn = cost - cy
        self.cy, self.cn = cy or 0.0, cn or 0.0

    @property
    def cost(self):
        return self.cy + self.cn

    def value(self, mid):
        pairs = min(self.yes, self.no)
        return pairs + (self.yes - pairs) * mid + (self.no - pairs) * (1 - mid)

    def add(self, side, size, cost):
        if side == "Y":
            self.yes += size
            self.cy += cost
        else:
            self.no += size
            self.cn += cost

    def merge(self):
        """Turn YES+NO pairs back into $1 each. Returns (pairs, realised PnL of those pairs)."""
        pairs = min(self.yes, self.no)
        if pairs <= 1e-9:
            return 0.0, 0.0
        c = self.cy * pairs / self.yes + self.cn * pairs / self.no
        self.cy -= self.cy * pairs / self.yes
        self.cn -= self.cn * pairs / self.no
        self.yes -= pairs
        self.no -= pairs
        if self.yes < 1e-9:
            self.yes, self.cy = 0.0, 0.0
        if self.no < 1e-9:
            self.no, self.cn = 0.0, 0.0
        return pairs, pairs - c


class MState:
    """One selected market: its selection, quotes and per-market counters."""

    def __init__(self, rm):
        self.rm = rm
        self.active = True
        self.last_trade = None           # YES-terms price of the last public trade
        self.last_trade_t = None         # when it printed (clock seconds)
        self.cooldown_until = 0.0
        self.hedge_after = 0.0           # a fill may be hedged from this moment on (order latency)
        self.orders = {}                 # "Y"/"N" -> {"oid", "price", "size", "t"}
        self.ref = None                  # reference price of the last quote pass
        self.alone = False               # nobody else scored in the zone at the last quote pass
        self.reward_total = 0.0
        self.stress_total = 0.0
        self.last_share = 0.0


class Farmer:
    def __init__(self, rc, journal, clock=time.time):
        self.rc = rc
        self.journal = journal
        self.clock = clock
        self.markets = {}                # slug -> MState
        self.token_slug = {}             # token -> slug
        self.pos = {}                    # slug -> Position
        self.meta = {}                   # slug -> {"yes", "no", "mid", "end", "fee"} (positions outlive their selection)
        self.broker = PaperBroker(150, 150, on_fill=self.on_fill, journal=journal, queue=True)
        self.feed = BookFeed(on_trade=self.on_trade, alive=lambda: not self._stop)
        self._stop = False
        self._wake = asyncio.Event()     # set on a fill: re-quote now, do not wait for the 2 s pass
        self.rewards_by_day = {}
        self.official_by_day = {}        # the same accrual with the share from Polymarket's competitiveness number
        self.alone_by_day = {}           # the part of rewards_by_day earned while nobody else scored in the zone
        self.stress_by_day = {}
        self.fills_by_day = {}
        self.hedges_by_day = {}          # day -> [hedged fills, shares, loss $]
        self.realized = 0.0
        self.realized_by_day = {}
        self.mfills = {}                 # slug -> material fills today
        self.day = utc_day(clock())
        self.day_base = None             # trading PnL at the start of the UTC day
        self.halted_until = 0.0
        self._last_sample = None
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        p = self.rc.state_file
        if not p or not os.path.exists(p):
            return
        try:
            s = json.load(open(p))
        except (OSError, ValueError):
            return
        self.rewards_by_day = s.get("rewards_by_day", {})
        self.official_by_day = s.get("official_by_day", {})
        self.alone_by_day = s.get("alone_by_day", {})
        self.stress_by_day = s.get("stress_by_day", {})
        self.fills_by_day = s.get("fills_by_day", {})
        self.hedges_by_day = s.get("hedges_by_day", {})
        self.realized = float(s.get("realized", 0.0))
        self.realized_by_day = s.get("realized_by_day", {})
        for slug, d in (s.get("positions") or {}).items():
            self.pos[slug] = (Position(d["yes"], d["no"], cy=d["cy"], cn=d["cn"]) if "cy" in d
                              else Position(d["yes"], d["no"], d["cost"]))
            self.meta[slug] = {"yes": d.get("yes_token"), "no": d.get("no_token"), "mid": d.get("mid"),
                               "end": d.get("end"), "fee": d.get("fee")}
        if s.get("day") == self.day:
            self.day_base = s.get("day_base")
            self.halted_until = s.get("halted_until", 0.0)
            self.mfills = s.get("mfills", {})

    def _save(self):
        p = self.rc.state_file
        if not p:
            return
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        positions = {}
        for slug, ps in self.pos.items():
            if ps.yes or ps.no:
                m = self.meta.get(slug, {})
                positions[slug] = {"yes": ps.yes, "no": ps.no, "cost": ps.cost, "cy": ps.cy, "cn": ps.cn,
                                   "yes_token": m.get("yes"), "no_token": m.get("no"), "mid": self.mid_of(slug),
                                   "end": m.get("end"), "fee": m.get("fee")}
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"day": self.day, "day_base": self.day_base, "halted_until": self.halted_until,
                       "mfills": self.mfills, "rewards_by_day": self.rewards_by_day,
                       "official_by_day": self.official_by_day, "alone_by_day": self.alone_by_day,
                       "stress_by_day": self.stress_by_day, "fills_by_day": self.fills_by_day,
                       "hedges_by_day": self.hedges_by_day, "realized": self.realized,
                       "realized_by_day": self.realized_by_day, "positions": positions}, f)
        os.replace(tmp, p)

    # ------------------------------------------------------------ feed and prices
    def feed_ok(self, now=None):
        """The market websocket delivered something recently (PONG answers count)."""
        now = self.clock() if now is None else now
        return now - self.feed.last_msg < self.rc.feed_stale_s

    def levels(self, token):
        b = self.feed.books.get(token)
        if not b:
            return [], []
        return sorted(b.bids.items(), reverse=True), sorted(b.asks.items())

    def reference(self, ms):
        """Price our quotes are centred on: the size-adjusted book mid when others quote near the price; in a wide
        book the last trade if it lies between the best bid and ask, else the middle of the book."""
        bids, asks = self.levels(ms.rm.yes)
        last = ms.last_trade if ms.last_trade is not None else ms.rm.price
        if not (bids and asks):
            return last
        lo, hi = bids[0][0], asks[0][0]
        if hi - lo <= self.rc.competitive_spread_mult * ms.rm.v / 100:
            m = adjusted_mid(bids, asks, ms.rm.min_size)
            return m if m is not None else (lo + hi) / 2
        return last if lo < last < hi else (lo + hi) / 2

    def mid_of(self, slug):
        """Mark price of a market's YES token: book mid if the book is tight, else last trade, else the last known."""
        ms = self.markets.get(slug)
        meta = self.meta.get(slug, {})
        token = ms.rm.yes if ms else meta.get("yes")
        bids, asks = self.levels(token) if token else ([], [])
        if bids and asks and asks[0][0] - bids[0][0] <= 0.10:
            m = (bids[0][0] + asks[0][0]) / 2
        elif ms and ms.last_trade is not None:
            m = ms.last_trade
        elif ms:
            m = ms.rm.price
        else:
            m = meta.get("mid")
        if m is not None:
            meta["mid"] = m
        return m

    def open_marks(self):
        """(inventory value, cost, mark-to-market PnL) over the open positions."""
        val = cost = 0.0
        for slug, ps in self.pos.items():
            if not (ps.yes or ps.no):
                continue
            m = self.mid_of(slug)
            val += ps.value(m if m is not None else 0.5)
            cost += ps.cost
        return val, cost, val - cost

    def trading_pnl(self):
        return self.realized + self.open_marks()[2]

    def min_age(self, ms):
        return ms.rm.min_age if ms.rm.min_age is not None else self.rc.min_order_age_s

    # ------------------------------------------------------------ events
    def on_trade(self, token, side, price, size, ts_ms):
        slug = self.token_slug.get(token)
        if slug is None:
            return
        ms = self.markets.get(slug)
        if ms is not None:
            ms.last_trade = price if token == ms.rm.yes else 1 - price
            ms.last_trade_t = self.clock()
        self.broker.on_public_trade(token, self._other, side, price, size, ts_ms)

    def _other(self, token):
        slug = self.token_slug.get(token)
        ms = self.markets.get(slug) if slug else None
        if ms:
            return ms.rm.no if token == ms.rm.yes else ms.rm.yes
        m = self.meta.get(slug, {})
        return m.get("no") if token == m.get("yes") else m.get("yes")

    def on_fill(self, fill):
        size = float(fill["size"])
        if size <= 1e-9:                    # zero-size prints exist; they are not fills
            return
        slug = fill["key"]
        ps = self.pos.setdefault(slug, Position())
        ps.add(fill["side"], size, fill["price"] * size)
        now = self.clock()
        day = utc_day(now)
        self.fills_by_day[day] = self.fills_by_day.get(day, 0) + 1
        ms = self.markets.get(slug)
        o = ms.orders.get(fill["side"]) if ms else None
        if o and o["oid"] == fill["order_id"]:
            o["size"] -= size
        material = size >= self.rc.min_fill_shares
        if ms is not None:
            ms.hedge_after = now + self.rc.hedge_latency_ms / 1000.0
        if material and ms is not None:
            ms.cooldown_until = now + self.rc.cooldown_min * 60
            self.mfills[slug] = self.mfills.get(slug, 0) + 1
        self.journal.log("rfill", key=slug, side=fill["side"], price=fill["price"], size=round(size, 4),
                         material=material, last_trade=ms.last_trade if ms else None,
                         alone=ms.alone if ms else None, fills_today=self.mfills.get(slug, 0),
                         pos={"yes": round(ps.yes, 2), "no": round(ps.no, 2), "cost": round(ps.cost, 2)})
        self._save()
        self._wake.set()

    # ------------------------------------------------------------ selection
    def apply_selection(self, picks):
        now = self.clock()
        keep = {p.slug for p in picks}
        for slug, ms in self.markets.items():
            if slug not in keep and ms.active:
                ms.active = False                  # the next quote pass cancels its orders
        for p in picks:
            ms = self.markets.get(p.slug)
            if ms is None:
                ms = self.markets[p.slug] = MState(p)
            else:
                ms.rm, ms.active = p, True
            self.token_slug[p.yes] = p.slug
            self.token_slug[p.no] = p.slug
            end = p.end_ts if p.end_ts == p.end_ts else None      # NaN -> None (JSON-safe)
            self.meta[p.slug] = {"yes": p.yes, "no": p.no, "mid": self.meta.get(p.slug, {}).get("mid"),
                                 "end": end, "fee": p.fee_rate}
        tokens = set()
        for slug, ms in self.markets.items():
            ps = self.pos.get(slug)
            if ms.active or (ps and (ps.yes or ps.no)):
                tokens |= {ms.rm.yes, ms.rm.no}
        for slug, m in self.meta.items():            # positions restored from the state file
            ps = self.pos.get(slug)
            if ps and (ps.yes or ps.no) and m.get("yes"):
                tokens |= {m["yes"], m["no"]}
                self.token_slug.setdefault(m["yes"], slug)
                self.token_slug.setdefault(m["no"], slug)
        self.feed.set_tokens(tokens)
        self._prune()
        log.info("selected %d markets (%d tokens subscribed) at %s", len(picks), len(tokens), now)

    def _prune(self):
        """Forget markets that are no longer selected, have no resting orders and no position (memory stays flat)."""
        for slug in list(self.markets):
            ms, ps = self.markets[slug], self.pos.get(slug)
            if not ms.active and not ms.orders and not (ps and (ps.yes or ps.no)):
                del self.markets[slug]
        for slug in list(self.meta):
            ps = self.pos.get(slug)
            if slug not in self.markets and not (ps and (ps.yes or ps.no)):
                del self.meta[slug]

    async def selector(self):
        while not self._stop:
            try:
                picks = await asyncio.to_thread(mk.select, self.rc, self.journal)
                self.apply_selection(picks)
            except Exception as e:  # noqa: BLE001   (an empty or failed fetch keeps the previous selection)
                log.warning("selection failed: %s", e)
                self.journal.log("rerror", where="select", error=str(e)[:300])
            await self._sleep(self.rc.reselect_min * 60)

    # ------------------------------------------------------------ hedge
    def hedge_once(self):
        """Flatten what a fill left: buy the other outcome from the live book when the pair then costs at most
        1 + max_hedge_cost a share (taker fee included) and merge the pair. Returns the number of hedges done."""
        rc = self.rc
        if not rc.hedge or not self.feed_ok():
            return 0
        now = self.clock()
        done = 0
        for slug, ps in list(self.pos.items()):
            imb = ps.yes - ps.no
            ms = self.markets.get(slug)
            meta = self.meta.get(slug, {})
            min_order = ms.rm.min_order if ms else 5.0
            if abs(imb) < min_order - 1e-9:                    # under the exchange's minimum order: stays as dust
                continue
            if ms is not None and now < ms.hedge_after:
                continue
            buy = "N" if imb > 0 else "Y"                      # the outcome we are short of
            token = (ms.rm.no if buy == "N" else ms.rm.yes) if ms else meta.get("no" if buy == "N" else "yes")
            if not token:
                continue
            q = walk_asks(self.levels(token)[1], abs(imb))
            if q is None:
                continue                                       # the book cannot take the size now: try again later
            fee_rate = (ms.rm.fee_rate if ms and ms.rm.fee_rate is not None
                        else meta.get("fee") if meta.get("fee") is not None else rc.fee_rate)
            fee = fee_rate * q * (1 - q)
            held = (ps.cy / ps.yes) if imb > 0 else (ps.cn / ps.no)
            cost = held + q + fee - 1.0                        # loss per share of the merged pair
            if cost > rc.max_hedge_cost + 1e-9:
                continue
            n = abs(imb)
            ps.add(buy, n, n * (q + fee))
            pairs, pnl = ps.merge()
            day = utc_day(now)
            self.realized += pnl
            self.realized_by_day[day] = self.realized_by_day.get(day, 0.0) + pnl
            h = self.hedges_by_day.setdefault(day, [0, 0.0, 0.0])
            h[0] += 1
            h[1] += n
            h[2] += n * cost
            self.journal.log("rhedge", key=slug, buy=buy, size=round(n, 4), price=round(q, 4), fee=round(fee, 5),
                             held=round(held, 4), cost_per_share=round(cost, 4), pnl=round(pnl, 4),
                             pairs=round(pairs, 4))
            if not (ps.yes or ps.no):
                del self.pos[slug]
            done += 1
        if done:
            self._save()
        return done

    def unhedged_usd(self):
        """Cost of the inventory that is not paired (what a hedge could not flatten yet)."""
        tot = 0.0
        for ps in self.pos.values():
            if ps.yes > ps.no:
                tot += ps.cy * (ps.yes - ps.no) / ps.yes
            elif ps.no > ps.yes:
                tot += ps.cn * (ps.no - ps.yes) / ps.no
        return tot

    # ------------------------------------------------------------ quoting
    async def pull(self, ms, why):
        oids = [o["oid"] for o in ms.orders.values()]
        if oids:
            await self.broker.cancel(oids, int(self.clock() * 1000))
            self.journal.log("rcancel", key=ms.rm.slug, why=why, n=len(oids))
        ms.orders.clear()

    def budget_used(self):
        """pUSD tied up: resting bids plus what the open positions cost (a real wallet pays for both)."""
        used = sum(ps.cost for ps in self.pos.values())
        for ms in self.markets.values():
            for o in ms.orders.values():
                used += o["price"] * max(o["size"], 0)
        return used

    def bids_usd(self):
        return sum(o["price"] * max(o["size"], 0) for ms in self.markets.values() for o in ms.orders.values())

    def targets(self, ms, now):
        """{"Y": (price, size), "N": (price, size)} we want resting now (sides may be missing)."""
        rc = self.rc
        if not ms.active or now < ms.cooldown_until or now < self.halted_until:
            return {}
        if self.mfills.get(ms.rm.slug, 0) >= rc.max_fills_per_market_day:
            return {}
        if ms.rm.end_ts - now < rc.min_days_left * 86400 * 0.5:
            return {}
        if not self.feed_ok(now):
            return {}                                  # silent websocket: the book may be stale, quote nothing
        b = self.feed.books.get(ms.rm.yes)
        if b is None or not b.last_rx:
            return {}                                  # no book yet
        bids, asks = self.levels(ms.rm.yes)
        ref = self.reference(ms)
        if not rc.price_lo <= ref <= rc.price_hi:
            return {}
        if (ms.last_trade is not None and ms.last_trade_t is not None and now - ms.last_trade_t < rc.trade_fresh_s
                and abs(ref - ms.last_trade) > rc.max_ref_disagree):
            # the book says one thing, the latest trade another: quoting here means being picked off
            return {}
        q1, q2 = zone_scores(bids, asks, ref, ms.rm.v)
        alone = mk.is_alone(ms.rm, q1, q2)
        plan = plan_quotes(ref, mk.quote_dist(rc, ms.rm.v, alone), ms.rm.tick, bids[0][0] if bids else None,
                           asks[0][0] if asks else None, v_cents=ms.rm.v, zone_margin=ZONE_MARGIN)
        if plan is None:
            return {}
        bid, ask = plan
        size = ms.rm.size
        share, _, _, _ = our_share(bids, asks, (bid, size), (ask, size), ms.rm.v, ms.rm.min_size)
        if share <= 0:
            return {}
        ms.ref, ms.alone = ref, alone
        out = {"Y": (bid, size), "N": (round(1 - ask, 6), size)}
        ps = self.pos.get(ms.rm.slug)
        if ps:
            if (ps.yes - ps.no) * bid >= rc.max_inventory_usd_per_market:
                out.pop("Y")
            if (ps.no - ps.yes) * (1 - ask) >= rc.max_inventory_usd_per_market:
                out.pop("N")
        return out

    def _keep(self, ms, side, cur, price, now):
        """A resting order stays (and keeps its age) while the target is the same or close and it still counts."""
        if abs(cur["price"] - price) < 1e-9:
            return True
        ref = ms.ref
        if ref is None:
            return False
        dist = ((ref - cur["price"]) if side == "Y" else ((1 - cur["price"]) - ref)) * 100      # cents from ref
        want = ((ref - price) if side == "Y" else ((1 - price) - ref)) * 100
        if dist >= ms.rm.v - ZONE_MARGIN + 1e-9 or dist < 0.5 * want:
            return False                          # out of the zone (scores nothing) or much too close to the price
        if abs(cur["price"] - price) <= self.rc.requote_ticks * ms.rm.tick + 1e-9:
            return True
        return now - cur["t"] < self.rc.requote_min_s

    async def quote_once(self):
        now = self.clock()
        now_ms = int(now * 1000)
        self.hedge_once()
        for ms in list(self.markets.values()):
            tg = self.targets(ms, now)
            bids, asks = self.levels(ms.rm.yes)
            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None
            for side in ("Y", "N"):
                cur = ms.orders.get(side)
                if cur and cur["size"] <= 1e-9:            # fully filled: nothing rests any more
                    ms.orders.pop(side, None)
                    cur = None
                want = tg.get(side)
                if want is None:
                    if cur:
                        await self.broker.cancel([cur["oid"]], now_ms)
                        ms.orders.pop(side, None)
                    continue
                price, size = want
                if side == "Y":
                    crossing = cur is not None and best_ask is not None and cur["price"] >= best_ask - 1e-9
                else:
                    crossing = cur is not None and best_bid is not None and 1 - cur["price"] <= best_bid + 1e-9
                if cur and not crossing and self._keep(ms, side, cur, price, now):
                    continue
                if cur:
                    await self.broker.cancel([cur["oid"]], now_ms)
                    ms.orders.pop(side, None)
                if self.budget_used() + price * size > self.rc.quote_budget_usd:
                    continue
                token = ms.rm.yes if side == "Y" else ms.rm.no
                ahead = dict(self.levels(token)[0]).get(price, 0.0)       # size already resting at our price
                oid = await self.broker.place(token, ms.rm.slug, side, price, size, now_ms, queue_ahead=ahead)
                ms.orders[side] = {"oid": oid, "price": price, "size": size, "t": now}
                self.journal.log("rquote", key=ms.rm.slug, side=side, price=price, size=size, crossing=crossing,
                                 alone=ms.alone, ref=round(ms.ref, 4) if ms.ref is not None else None)
        self.broker.gc(now_ms)

    # ------------------------------------------------------------ rewards
    def sample_once(self):
        """Score our resting orders like Polymarket's sample; credit rate * share * elapsed time."""
        now = self.clock()
        now_ms = int(now * 1000)
        day = utc_day(now)
        dt = (now - self._last_sample) if self._last_sample is not None else self.rc.sample_s
        self._last_sample = now
        dt = min(max(dt, 0.0), 3 * self.rc.sample_s)         # a long pause (restart, suspend) is not one big sample
        feed_up = self.feed_ok(now)
        inc_total = stress_total = off_total = alone_total = 0.0
        n_live = n_score = n_young = 0
        for ms in self.markets.values():
            live = {}
            age = self.min_age(ms)
            for side, o in ms.orders.items():
                po = self.broker.orders.get(o["oid"])
                if po and po.live_at(now_ms):
                    if now - o["t"] < age - 1e-9:
                        n_young += 1                         # rests, but not long enough to count yet
                        continue
                    live[side] = (po.price, po.remaining)
            if not live:
                ms.last_share = 0.0
                continue
            n_live += 1
            if not feed_up:
                continue                                     # cannot score against a silent book: no credit
            bids, asks = self.levels(ms.rm.yes)
            ours_bid = live.get("Y")
            ours_ask = (round(1 - live["N"][0], 6), live["N"][1]) if "N" in live else None
            share, _, q_ours, q_comp = our_share(bids, asks, ours_bid, ours_ask, ms.rm.v, ms.rm.min_size)
            ms.last_share = share
            if share <= 0:
                continue
            n_score += 1
            stress = q_ours / (2 * q_ours + q_comp) if q_ours > 0 else 0.0   # as if an identical maker joined
            k = ms.rm.rate * dt / 86400.0
            inc = k * share
            ms.reward_total += inc
            ms.stress_total += k * stress
            inc_total += inc
            stress_total += k * stress
            off_total += k * official_share(ms.rm.rate, ms.rm.comp, q_ours)
            if q_comp <= 1e-9:
                alone_total += inc
        for book, x in ((self.rewards_by_day, inc_total), (self.stress_by_day, stress_total),
                        (self.official_by_day, off_total), (self.alone_by_day, alone_total)):
            book[day] = book.get(day, 0.0) + x
        self.journal.log("rsample", live=n_live, scoring=n_score, young=n_young, dt=round(dt, 1), feed=feed_up,
                         inc=round(inc_total, 5), official=round(off_total, 5), alone=round(alone_total, 5),
                         today=round(self.rewards_by_day[day], 3))
        return inc_total

    async def sampler(self):
        while not self._stop:
            await self._sleep(self.rc.sample_s * random.uniform(0.5, 1.5))
            try:
                self.sample_once()
            except Exception as e:  # noqa: BLE001
                log.warning("sample failed: %s", e)

    # ------------------------------------------------------------ settlement
    @staticmethod
    def _gamma_resolution(slug, yes_token, no_token):
        """(payoff of one YES share, payoff of one NO share) once Polymarket has closed the market, else None."""
        # Gamma returns a market by slug only with closed=true once it has closed (without it the list is empty)
        d = get_json(f"{GAMMA}/markets", {"slug": slug, "closed": "true"}, timeout=30)
        m = next((x for x in d if x.get("slug") == slug), None) if isinstance(d, list) else None
        if not m or not m.get("closed"):
            return None
        toks, prices = m.get("clobTokenIds"), m.get("outcomePrices")
        if isinstance(toks, str):
            toks = json.loads(toks)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not toks or not prices or yes_token not in toks or no_token not in toks:
            return None
        py = float(prices[toks.index(yes_token)])
        pn = float(prices[toks.index(no_token)])
        if abs(py + pn - 1.0) > 1e-6:                 # not a clean resolution yet
            return None
        return py, pn

    async def settle_once(self):
        now = self.clock()
        for slug, ps in list(self.pos.items()):
            if not (ps.yes or ps.no):
                continue
            m = self.meta.get(slug, {})
            ms = self.markets.get(slug)
            if ms is not None and ms.active:
                continue                                    # still selected: not over yet
            end = m.get("end")
            if end is not None and now < end + SETTLE_GRACE_S:   # unknown end (None): look every pass
                continue
            try:
                res = await asyncio.to_thread(self._gamma_resolution, slug, m.get("yes"), m.get("no"))
            except Exception as e:  # noqa: BLE001
                log.warning("resolution lookup failed for %s: %s", slug, e)
                continue
            if res is None:
                continue
            py, pn = res
            payoff = ps.yes * py + ps.no * pn
            pnl = payoff - ps.cost
            day = utc_day(now)
            self.realized += pnl
            self.realized_by_day[day] = self.realized_by_day.get(day, 0.0) + pnl
            self.journal.log("rsettle", key=slug, yes=round(ps.yes, 2), no=round(ps.no, 2), cost=round(ps.cost, 2),
                             payoff=round(payoff, 2), pnl=round(pnl, 2), py=py, pn=pn)
            del self.pos[slug]
            self.meta.pop(slug, None)
            self._save()

    async def settler(self):
        while not self._stop:
            await self._sleep(self.rc.settle_s)
            try:
                await self.settle_once()
            except Exception as e:  # noqa: BLE001
                log.warning("settlement pass failed: %s", e)

    # ------------------------------------------------------------ status / risk
    def status(self):
        now = self.clock()
        day = utc_day(now)
        val, cost, open_pnl = self.open_marks()
        trading = self.realized + open_pnl
        if self.day_base is None:
            self.day_base = trading
        h = self.hedges_by_day.get(day, [0, 0.0, 0.0])
        ht = [sum(x[i] for x in self.hedges_by_day.values()) for i in range(3)]
        st = {"day": day,
              "rewards_today": round(self.rewards_by_day.get(day, 0.0), 3),
              "rewards_total": round(sum(self.rewards_by_day.values()), 3),
              "official_today": round(self.official_by_day.get(day, 0.0), 3),
              "official_total": round(sum(self.official_by_day.values()), 3),
              "alone_today": round(self.alone_by_day.get(day, 0.0), 3),
              "alone_total": round(sum(self.alone_by_day.values()), 3),
              "stress_today": round(self.stress_by_day.get(day, 0.0), 3),
              "stress_total": round(sum(self.stress_by_day.values()), 3),
              "inv_value": round(val, 2), "inv_cost": round(cost, 2), "unhedged_usd": round(self.unhedged_usd(), 2),
              "open_pnl": round(open_pnl, 2), "realized": round(self.realized, 2),
              "trading_pnl": round(trading, 2), "trading_pnl_today": round(trading - self.day_base, 2),
              "fills_today": self.fills_by_day.get(day, 0), "fills_total": sum(self.fills_by_day.values()),
              "hedges_today": h[0], "hedge_loss_today": round(h[2], 2),
              "hedges_total": ht[0], "hedge_shares_total": round(ht[1], 1), "hedge_loss_total": round(ht[2], 2),
              "markets": sum(1 for m in self.markets.values() if m.active),
              "quoting": sum(1 for m in self.markets.values() if m.orders),
              "alone_now": sum(1 for m in self.markets.values() if m.orders and m.alone),
              "bids_usd": round(self.bids_usd(), 2),
              "rate_now": round(sum(m.rm.rate * m.last_share for m in self.markets.values()), 2),
              "feed": self.feed_ok(now), "halted": now < self.halted_until}
        st["total_pnl"] = round(st["rewards_total"] + trading, 2)
        # the cautious figure: the lower reward estimate, without the part earned alone in a zone
        low = min(st["rewards_total"], st["official_total"]) - st["alone_total"]
        st["cautious_pnl"] = round(max(low, 0.0) + trading, 2)
        top = sorted(self.markets.values(), key=lambda m: -m.reward_total)[:12]
        st["top"] = [{"slug": m.rm.slug[:60], "reward": round(m.reward_total, 3), "share": round(m.last_share, 3),
                      "rate": m.rm.rate, "empty": m.alone} for m in top if m.reward_total > 0]
        return st

    def check_risk(self, st):
        now = self.clock()
        if st["trading_pnl_today"] <= -self.rc.daily_loss_limit and now >= self.halted_until:
            self.halted_until = next_midnight(now)
            self.journal.log("kill", reason=f"награды: дневной убыток по позициям {st['trading_pnl_today']:.2f}$ "
                                            f"(лимит {self.rc.daily_loss_limit}$); котировки выключены до 00:00 UTC, "
                                            f"потом включатся сами")
            # halted_until makes targets() empty: the next quote pass cancels everything

    def roll_day(self):
        now = self.clock()
        day = utc_day(now)
        if day == self.day:
            return
        trading = self.trading_pnl()
        h = self.hedges_by_day.get(self.day, [0, 0.0, 0.0])
        self.journal.log("rday", day=self.day, rewards=round(self.rewards_by_day.get(self.day, 0.0), 3),
                         official=round(self.official_by_day.get(self.day, 0.0), 3),
                         alone=round(self.alone_by_day.get(self.day, 0.0), 3),
                         trading_pnl_day=round(trading - (self.day_base or 0.0), 2),
                         fills=self.fills_by_day.get(self.day, 0), hedges=h[0], hedge_loss=round(h[2], 2))
        self.day, self.day_base = day, trading
        self.mfills = {}

    async def status_loop(self):
        while not self._stop:
            try:
                self.roll_day()
                st = self.status()
                self.check_risk(st)
                self.journal.log("rstatus", **st)
                self._save()
            except Exception as e:  # noqa: BLE001
                log.warning("status failed: %s", e)
            await self._sleep(self.rc.status_s)

    def _next_hedge_in(self):
        """Seconds until the earliest pending hedge may be tried (None = nothing pending)."""
        now = self.clock()
        due = [ms.hedge_after - now for slug, ms in self.markets.items()
               if ms.hedge_after > now and slug in self.pos]
        return min(due) if due else None

    async def quoter(self):
        while not self._stop:
            if self.rc.kill_file and os.path.exists(self.rc.kill_file):
                self.journal.log("kill", reason=f"kill file {self.rc.kill_file} present")
                self._stop = True
                break
            try:
                await self.quote_once()
            except Exception as e:  # noqa: BLE001
                log.warning("quote failed: %s", e)
            wait = 2.0
            nh = self._next_hedge_in()
            if nh is not None:
                wait = max(0.02, min(wait, nh + 0.01))       # come back when the hedge order would reach the book
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _sleep(self, s):
        end = time.time() + s
        while not self._stop and time.time() < end:
            await asyncio.sleep(min(1.0, end - time.time()))

    async def run(self, duration_s=None):
        self.journal.log("start", mode="paper", bot="rewards", budget=self.rc.quote_budget_usd,
                         max_markets=self.rc.max_markets, dist_cents=self.rc.dist_cents,
                         alone_frac=self.rc.alone_frac, hedge=self.rc.hedge, max_hedge_cost=self.rc.max_hedge_cost)
        tasks = [asyncio.create_task(x) for x in (self.feed.run(), self.selector(), self.quoter(), self.sampler(),
                                                    self.settler(), self.status_loop())]
        t0 = time.time()
        while not self._stop and (duration_s is None or time.time() - t0 < duration_s):
            await asyncio.sleep(1)
        self._stop = True
        for ms in self.markets.values():
            await self.pull(ms, "stop")
        st = self.status()
        self.journal.log("rstatus", **st)
        self.journal.log("stopped", **{k: st[k] for k in ("rewards_total", "trading_pnl", "total_pnl")})
        self._save()
        for t in tasks:
            t.cancel()
