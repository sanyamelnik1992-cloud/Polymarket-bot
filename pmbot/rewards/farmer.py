"""Liquidity-rewards farmer (paper): selection -> two-sided quotes -> simulated fills -> estimated rewards.

Per market we rest a YES bid at ref - d and a NO bid at 1 - (ref + d) (= a YES ask at ref + d), size = the market's
rewards min size. ref = the size-cutoff-adjusted book mid when others quote near the price, else the last trade.
  * fills: PaperBroker trade-through rule on the public trade stream (a print strictly through our price fills us);
  * after a fill: all quotes in that market are pulled for `cooldown_min`, then re-centred on the last trade;
    after `max_fills_per_market_day` fills the market rests until 00:00 UTC; a side stops being bid once its
    unhedged inventory reaches `max_inventory_usd_per_market`;
  * rewards: every ~60 s our orders are scored against the live book with Polymarket's formula
    (pmbot.rewards.scoring) and pool/1440 * our share is credited -- an ESTIMATE, the real payout is decided by
    Polymarket and can only be confirmed live;
  * PnL = estimated rewards + mark-to-market of the inventory (YES+NO pairs count as $1).
"""
import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..bot.paper import PaperBroker
from . import markets as mk
from .feed import BookFeed
from .scoring import adjusted_mid, our_share, plan_quotes

log = logging.getLogger("pmbot.rewards")


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def next_midnight(ts):
    return (int(ts) // 86400 + 1) * 86400


@dataclass
class Position:
    yes: float = 0.0
    no: float = 0.0
    cost: float = 0.0

    def value(self, mid):
        pairs = min(self.yes, self.no)
        return pairs + (self.yes - pairs) * mid + (self.no - pairs) * (1 - mid)


@dataclass
class MState:
    rm: mk.RMarket
    active: bool = True
    last_trade: float | None = None      # YES-terms price of the last public trade
    last_mid: float | None = None
    cooldown_until: float = 0.0
    blocked_until: float = 0.0           # after max fills in a day
    fills_today: int = 0
    orders: dict = field(default_factory=dict)   # "Y"/"N" -> {"oid", "price", "size", "t"}
    reward_total: float = 0.0
    last_share: float = 0.0


class Farmer:
    def __init__(self, rc, journal, clock=time.time):
        self.rc = rc
        self.journal = journal
        self.clock = clock
        self.markets = {}                # slug -> MState
        self.token_slug = {}             # token -> slug
        self.pos = {}                    # slug -> Position
        self.broker = PaperBroker(150, 150, on_fill=self.on_fill, journal=journal)
        self.feed = BookFeed(on_trade=self.on_trade, alive=lambda: not self._stop)
        self._stop = False
        self.rewards_by_day = {}
        self.fills_by_day = {}
        self.day = utc_day(clock())
        self.day_base = None             # mark-to-market PnL at the start of the UTC day
        self.halted_until = 0.0
        self.meta = {}                   # slug -> {"yes", "no", "mid"} for positions of markets no longer selected
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
        self.fills_by_day = s.get("fills_by_day", {})
        for slug, d in (s.get("positions") or {}).items():
            self.pos[slug] = Position(d["yes"], d["no"], d["cost"])
            self.meta[slug] = {"yes": d.get("yes_token"), "no": d.get("no_token"), "mid": d.get("mid")}
        if s.get("day") == self.day:
            self.day_base = s.get("day_base")
            self.halted_until = s.get("halted_until", 0.0)

    def _save(self):
        p = self.rc.state_file
        if not p:
            return
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        positions = {}
        for slug, ps in self.pos.items():
            if ps.yes or ps.no:
                m = self.meta.get(slug, {})
                positions[slug] = {"yes": ps.yes, "no": ps.no, "cost": ps.cost, "yes_token": m.get("yes"),
                                   "no_token": m.get("no"), "mid": self.mid_of(slug)}
        tmp = p + ".tmp"
        json.dump({"day": self.day, "day_base": self.day_base, "halted_until": self.halted_until,
                   "rewards_by_day": self.rewards_by_day, "fills_by_day": self.fills_by_day,
                   "positions": positions}, open(tmp, "w"))
        os.replace(tmp, p)

    # ------------------------------------------------------------ prices
    def levels(self, token):
        b = self.feed.books.get(token)
        if not b:
            return [], []
        return sorted(b.bids.items(), reverse=True), sorted(b.asks.items())

    def reference(self, ms):
        bids, asks = self.levels(ms.rm.yes)
        if bids and asks and asks[0][0] - bids[0][0] <= self.rc.competitive_spread_mult * ms.rm.v / 100:
            m = adjusted_mid(bids, asks, ms.rm.min_size)
            return m if m is not None else (bids[0][0] + asks[0][0]) / 2
        return ms.last_trade if ms.last_trade is not None else ms.rm.price

    def mid_of(self, slug):
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
            if ms:
                ms.last_mid = m
            meta["mid"] = m
        return m

    def mtm(self):
        """(inventory value, cost, pnl) over all positions."""
        val = cost = 0.0
        for slug, ps in self.pos.items():
            if not (ps.yes or ps.no):
                continue
            m = self.mid_of(slug)
            val += ps.value(m if m is not None else 0.5)
            cost += ps.cost
        return val, cost, val - cost

    # ------------------------------------------------------------ events
    def on_trade(self, token, side, price, size, ts_ms):
        slug = self.token_slug.get(token)
        if slug is None:
            return
        ms = self.markets.get(slug)
        if ms is not None:
            ms.last_trade = price if token == ms.rm.yes else 1 - price
        self.broker.on_public_trade(token, self._other, side, price, size, ts_ms)

    def _other(self, token):
        slug = self.token_slug.get(token)
        ms = self.markets.get(slug) if slug else None
        if ms:
            return ms.rm.no if token == ms.rm.yes else ms.rm.yes
        m = self.meta.get(slug, {})
        return m.get("no") if token == m.get("yes") else m.get("yes")

    def on_fill(self, fill):
        slug = fill["key"]
        ps = self.pos.setdefault(slug, Position())
        if fill["side"] == "Y":
            ps.yes += fill["size"]
        else:
            ps.no += fill["size"]
        ps.cost += fill["price"] * fill["size"]
        now = self.clock()
        day = utc_day(now)
        self.fills_by_day[day] = self.fills_by_day.get(day, 0) + 1
        ms = self.markets.get(slug)
        if ms is None:
            return
        o = ms.orders.get(fill["side"])
        if o and o["oid"] == fill["order_id"]:
            o["size"] -= fill["size"]
        ms.fills_today += 1
        ms.cooldown_until = now + self.rc.cooldown_min * 60
        if ms.fills_today >= self.rc.max_fills_per_market_day:
            ms.blocked_until = next_midnight(now)
        self.journal.log("rfill", key=slug, side=fill["side"], price=fill["price"], size=fill["size"],
                         last_trade=ms.last_trade, fills_today=ms.fills_today,
                         pos={"yes": round(ps.yes, 2), "no": round(ps.no, 2), "cost": round(ps.cost, 2)})
        # the cooldown makes targets() empty, so the next quote pass (<= 2 s) pulls the other side too

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
                ms = self.markets[p.slug] = MState(rm=p)
            else:
                last, cd, blk, ft = ms.last_trade, ms.cooldown_until, ms.blocked_until, ms.fills_today
                ms.rm, ms.active = p, True
                ms.last_trade, ms.cooldown_until, ms.blocked_until, ms.fills_today = last, cd, blk, ft
            self.token_slug[p.yes] = p.slug
            self.token_slug[p.no] = p.slug
            self.meta[p.slug] = {"yes": p.yes, "no": p.no, "mid": self.meta.get(p.slug, {}).get("mid")}
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
        log.info("selected %d markets (%d tokens subscribed) at %s", len(picks), len(tokens), now)

    async def selector(self):
        while not self._stop:
            try:
                picks = await asyncio.to_thread(mk.select, self.rc, self.journal)
                self.apply_selection(picks)
            except Exception as e:  # noqa: BLE001
                log.warning("selection failed: %s", e)
                self.journal.log("rerror", where="select", error=str(e)[:300])
            await self._sleep(self.rc.reselect_min * 60)

    # ------------------------------------------------------------ quoting
    async def pull(self, ms, why):
        oids = [o["oid"] for o in ms.orders.values()]
        if oids:
            await self.broker.cancel(oids, int(self.clock() * 1000))
            self.journal.log("rcancel", key=ms.rm.slug, why=why, n=len(oids))
        ms.orders.clear()

    def budget_used(self):
        used = 0.0
        for ms in self.markets.values():
            for o in ms.orders.values():
                used += o["price"] * max(o["size"], 0)
        return used

    def targets(self, ms, now):
        """{"Y": (price, size), "N": (price, size)} we want resting now (sides may be missing)."""
        rc = self.rc
        if (not ms.active or now < ms.cooldown_until or now < ms.blocked_until or now < self.halted_until
                or ms.rm.end_ts - now < rc.min_days_left * 86400 * 0.5):
            return {}
        b = self.feed.books.get(ms.rm.yes)
        if b is None or not b.last_rx:
            return {}                                  # no live book yet
        bids, asks = self.levels(ms.rm.yes)
        ref = self.reference(ms)
        if not rc.price_lo / 2 <= ref <= 1 - (1 - rc.price_hi) / 2:
            return {}
        plan = plan_quotes(ref, rc.dist_cents, ms.rm.tick, bids[0][0] if bids else None, asks[0][0] if asks else None)
        if plan is None:
            return {}
        bid, ask = plan
        size = ms.rm.size
        share, mid, _, _ = our_share(bids, asks, (bid, size), (ask, size), ms.rm.v, ms.rm.min_size)
        if share <= 0:
            return {}
        out = {"Y": (bid, size), "N": (round(1 - ask, 6), size)}
        ps = self.pos.get(ms.rm.slug)
        if ps:
            if (ps.yes - ps.no) * bid >= rc.max_inventory_usd_per_market:
                out.pop("Y")
            if (ps.no - ps.yes) * (1 - ask) >= rc.max_inventory_usd_per_market:
                out.pop("N")
        return out

    async def quote_once(self):
        now = self.clock()
        now_ms = int(now * 1000)
        for ms in list(self.markets.values()):
            tg = self.targets(ms, now)
            for side in ("Y", "N"):
                cur = ms.orders.get(side)
                want = tg.get(side)
                if want is None:
                    if cur:
                        await self.broker.cancel([cur["oid"]], now_ms)
                        ms.orders.pop(side, None)
                    continue
                price, size = want
                if cur and cur["size"] > 1e-9 and abs(cur["price"] - price) < 1e-9:
                    continue
                if cur and now - cur["t"] < self.rc.requote_min_s and cur["size"] > 1e-9:
                    continue
                if cur:
                    await self.broker.cancel([cur["oid"]], now_ms)
                    ms.orders.pop(side, None)
                if self.budget_used() + price * size > self.rc.quote_budget_usd:
                    continue
                token = ms.rm.yes if side == "Y" else ms.rm.no
                oid = await self.broker.place(token, ms.rm.slug, side, price, size, now_ms)
                ms.orders[side] = {"oid": oid, "price": price, "size": size, "t": now}
                self.journal.log("rquote", key=ms.rm.slug, side=side, price=price, size=size)
        self.broker.gc(now_ms)

    # ------------------------------------------------------------ rewards
    def sample_once(self):
        """Score our resting orders like Polymarket's once-a-minute sample; credit pool/1440 * share."""
        now = self.clock()
        now_ms = int(now * 1000)
        day = utc_day(now)
        inc_total, n_live, n_score = 0.0, 0, 0
        for ms in self.markets.values():
            live = {}
            for side, o in ms.orders.items():
                po = self.broker.orders.get(o["oid"])
                if po and po.live_at(now_ms):
                    live[side] = (po.price, po.remaining)
            if not live:
                ms.last_share = 0.0
                continue
            n_live += 1
            bids, asks = self.levels(ms.rm.yes)
            ours_bid = live.get("Y")
            ours_ask = (round(1 - live["N"][0], 6), live["N"][1]) if "N" in live else None
            share, _, _, _ = our_share(bids, asks, ours_bid, ours_ask, ms.rm.v, ms.rm.min_size)
            ms.last_share = share
            if share > 0:
                n_score += 1
                inc = ms.rm.rate / 1440.0 * share
                ms.reward_total += inc
                inc_total += inc
        self.rewards_by_day[day] = self.rewards_by_day.get(day, 0.0) + inc_total
        self.journal.log("rsample", live=n_live, scoring=n_score, inc=round(inc_total, 4),
                         today=round(self.rewards_by_day[day], 3))
        return inc_total

    async def sampler(self):
        while not self._stop:
            await self._sleep(self.rc.sample_s * random.uniform(0.5, 1.5))
            try:
                self.sample_once()
            except Exception as e:  # noqa: BLE001
                log.warning("sample failed: %s", e)

    # ------------------------------------------------------------ status / risk
    def status(self):
        now = self.clock()
        day = utc_day(now)
        val, cost, pnl = self.mtm()
        if self.day_base is None:
            self.day_base = pnl
        st = {"day": day, "rewards_today": round(self.rewards_by_day.get(day, 0.0), 3),
              "rewards_total": round(sum(self.rewards_by_day.values()), 3),
              "inv_value": round(val, 2), "inv_cost": round(cost, 2), "mtm_pnl": round(pnl, 2),
              "mtm_pnl_today": round(pnl - self.day_base, 2),
              "fills_today": self.fills_by_day.get(day, 0), "fills_total": sum(self.fills_by_day.values()),
              "markets": sum(1 for m in self.markets.values() if m.active),
              "quoting": sum(1 for m in self.markets.values() if m.orders),
              "bids_usd": round(self.budget_used(), 2),
              "rate_now": round(sum(m.rm.rate * m.last_share for m in self.markets.values()), 2),
              "halted": now < self.halted_until}
        st["total_pnl"] = round(st["rewards_total"] + st["mtm_pnl"], 2)
        top = sorted(self.markets.values(), key=lambda m: -m.reward_total)[:12]
        st["top"] = [{"slug": m.rm.slug[:60], "reward": round(m.reward_total, 3), "share": round(m.last_share, 3),
                      "rate": m.rm.rate, "fills": m.fills_today, "empty": m.rm.empty} for m in top if m.reward_total > 0]
        return st

    def check_risk(self, st):
        now = self.clock()
        if st["mtm_pnl_today"] <= -self.rc.daily_loss_limit and now >= self.halted_until:
            self.halted_until = next_midnight(now)
            self.journal.log("kill", reason=f"награды: убыток по позициям за день {st['mtm_pnl_today']:.2f}$ "
                                            f"(лимит {self.rc.daily_loss_limit}$); котировки выключены до 00:00 UTC, "
                                            f"потом включатся сами")
            # halted_until makes targets() empty: the next quote pass cancels everything

    def roll_day(self):
        now = self.clock()
        day = utc_day(now)
        if day == self.day:
            return
        _, _, pnl = self.mtm()
        self.journal.log("rday", day=self.day, rewards=round(self.rewards_by_day.get(self.day, 0.0), 3),
                         mtm_pnl_day=round(pnl - (self.day_base or 0.0), 2), fills=self.fills_by_day.get(self.day, 0))
        self.day, self.day_base = day, pnl
        for ms in self.markets.values():
            ms.fills_today = 0

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
            await self._sleep(2.0)

    async def _sleep(self, s):
        end = time.time() + s
        while not self._stop and time.time() < end:
            await asyncio.sleep(min(1.0, end - time.time()))

    async def run(self, duration_s=None):
        self.journal.log("start", mode="paper", bot="rewards", budget=self.rc.quote_budget_usd,
                         max_markets=self.rc.max_markets, dist_cents=self.rc.dist_cents)
        tasks = [asyncio.create_task(x) for x in (self.feed.run(), self.selector(), self.quoter(), self.sampler(),
                                                    self.status_loop())]
        t0 = time.time()
        while not self._stop and (duration_s is None or time.time() - t0 < duration_s):
            await asyncio.sleep(1)
        self._stop = True
        for ms in self.markets.values():
            await self.pull(ms, "stop")
        st = self.status()
        self.journal.log("rstatus", **st)
        self.journal.log("stopped", **{k: st[k] for k in ("rewards_total", "mtm_pnl", "total_pnl")})
        self._save()
        for t in tasks:
            t.cancel()
