"""Liquidity-rewards farmer (paper): selection -> two-sided quotes -> simulated fills -> estimated rewards.

Per market we rest a YES bid at ref - d and a NO bid at 1 - (ref + d) (= a YES ask at ref + d), size = the market's
rewards min size. ref = the size-cutoff-adjusted book mid when others quote near the price, else the last trade.

  * fills: PaperBroker trade-through rule on the public trade stream (a print strictly through our price fills us);
  * a material fill (>= min_fill_shares) pulls all quotes in that market for `cooldown_min`, then re-centres on the
    last trade; after `max_fills_per_market_day` material fills the market rests until 00:00 UTC; a side stops being
    bid once its unhedged inventory reaches `max_inventory_usd_per_market`. Counters survive restarts;
  * quotes are only live while the market feed is alive (no websocket message for `feed_stale_s` -> pull everything);
    a quote that would cross the book is replaced at once;
  * rewards: every sample we score our resting orders against the live book with Polymarket's formula
    (pmbot.rewards.scoring) and credit rate * share * elapsed / 1 day. Also tracked: the "stress" figure, i.e. the
    share if one more identical maker joined. Both are ESTIMATES -- the real payout is decided by Polymarket and can
    only be confirmed live;
  * settlement: when Polymarket closes a market (Gamma `closed`, outcomePrices 1/0) the position is paid out and its
    PnL is realised; until then it is marked at the last mid;
  * trading PnL = realised + mark-to-market of open positions (YES+NO pairs count as $1). The daily loss stop uses it.
"""
import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..bot.paper import PaperBroker
from ..config import GAMMA
from ..http import get_json
from . import markets as mk
from .feed import BookFeed
from .scoring import adjusted_mid, our_share, plan_quotes

log = logging.getLogger("pmbot.rewards")
SETTLE_GRACE_S = 3600          # look for a resolution at most this long after the market's end (then keep polling)


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


class MState:
    """One selected market: its selection, quotes and per-market counters."""

    def __init__(self, rm):
        self.rm = rm
        self.active = True
        self.last_trade = None           # YES-terms price of the last public trade
        self.last_trade_t = None         # when it printed (clock seconds)
        self.cooldown_until = 0.0
        self.orders = {}                 # "Y"/"N" -> {"oid", "price", "size", "t"}
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
        self.meta = {}                   # slug -> {"yes", "no", "mid", "end"} (positions outlive their selection)
        self.broker = PaperBroker(150, 150, on_fill=self.on_fill, journal=journal)
        self.feed = BookFeed(on_trade=self.on_trade, alive=lambda: not self._stop)
        self._stop = False
        self._wake = asyncio.Event()     # set on a fill: re-quote now, do not wait for the 2 s pass
        self.rewards_by_day = {}
        self.stress_by_day = {}
        self.fills_by_day = {}
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
        self.stress_by_day = s.get("stress_by_day", {})
        self.fills_by_day = s.get("fills_by_day", {})
        self.realized = float(s.get("realized", 0.0))
        self.realized_by_day = s.get("realized_by_day", {})
        for slug, d in (s.get("positions") or {}).items():
            self.pos[slug] = Position(d["yes"], d["no"], d["cost"])
            self.meta[slug] = {"yes": d.get("yes_token"), "no": d.get("no_token"), "mid": d.get("mid"),
                               "end": d.get("end")}
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
                positions[slug] = {"yes": ps.yes, "no": ps.no, "cost": ps.cost, "yes_token": m.get("yes"),
                                   "no_token": m.get("no"), "mid": self.mid_of(slug), "end": m.get("end")}
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"day": self.day, "day_base": self.day_base, "halted_until": self.halted_until,
                       "mfills": self.mfills, "rewards_by_day": self.rewards_by_day,
                       "stress_by_day": self.stress_by_day, "fills_by_day": self.fills_by_day,
                       "realized": self.realized, "realized_by_day": self.realized_by_day,
                       "positions": positions}, f)
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
        bids, asks = self.levels(ms.rm.yes)
        if bids and asks and asks[0][0] - bids[0][0] <= self.rc.competitive_spread_mult * ms.rm.v / 100:
            m = adjusted_mid(bids, asks, ms.rm.min_size)
            return m if m is not None else (bids[0][0] + asks[0][0]) / 2
        return ms.last_trade if ms.last_trade is not None else ms.rm.price

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
        if fill["side"] == "Y":
            ps.yes += size
        else:
            ps.no += size
        ps.cost += fill["price"] * size
        now = self.clock()
        day = utc_day(now)
        self.fills_by_day[day] = self.fills_by_day.get(day, 0) + 1
        ms = self.markets.get(slug)
        o = ms.orders.get(fill["side"]) if ms else None
        if o and o["oid"] == fill["order_id"]:
            o["size"] -= size
        material = size >= self.rc.min_fill_shares
        if material and ms is not None:
            ms.cooldown_until = now + self.rc.cooldown_min * 60
            self.mfills[slug] = self.mfills.get(slug, 0) + 1
        self.journal.log("rfill", key=slug, side=fill["side"], price=fill["price"], size=round(size, 4),
                         material=material, last_trade=ms.last_trade if ms else None,
                         fills_today=self.mfills.get(slug, 0),
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
                                 "end": end}
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
        plan = plan_quotes(ref, rc.dist_cents, ms.rm.tick, bids[0][0] if bids else None, asks[0][0] if asks else None)
        if plan is None:
            return {}
        bid, ask = plan
        size = ms.rm.size
        share, _, _, _ = our_share(bids, asks, (bid, size), (ask, size), ms.rm.v, ms.rm.min_size)
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
                if cur and abs(cur["price"] - price) < 1e-9 and not crossing:
                    continue
                if cur and not crossing and now - cur["t"] < self.rc.requote_min_s:
                    continue
                if cur:
                    await self.broker.cancel([cur["oid"]], now_ms)
                    ms.orders.pop(side, None)
                if self.budget_used() + price * size > self.rc.quote_budget_usd:
                    continue
                token = ms.rm.yes if side == "Y" else ms.rm.no
                oid = await self.broker.place(token, ms.rm.slug, side, price, size, now_ms)
                ms.orders[side] = {"oid": oid, "price": price, "size": size, "t": now}
                self.journal.log("rquote", key=ms.rm.slug, side=side, price=price, size=size,
                                 crossing=crossing)
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
        inc_total = stress_total = 0.0
        n_live = n_score = 0
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
            inc = ms.rm.rate * share * dt / 86400.0
            inc_s = ms.rm.rate * stress * dt / 86400.0
            ms.reward_total += inc
            ms.stress_total += inc_s
            inc_total += inc
            stress_total += inc_s
        self.rewards_by_day[day] = self.rewards_by_day.get(day, 0.0) + inc_total
        self.stress_by_day[day] = self.stress_by_day.get(day, 0.0) + stress_total
        self.journal.log("rsample", live=n_live, scoring=n_score, dt=round(dt, 1), feed=feed_up,
                         inc=round(inc_total, 5), today=round(self.rewards_by_day[day], 3))
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
        st = {"day": day,
              "rewards_today": round(self.rewards_by_day.get(day, 0.0), 3),
              "rewards_total": round(sum(self.rewards_by_day.values()), 3),
              "stress_today": round(self.stress_by_day.get(day, 0.0), 3),
              "stress_total": round(sum(self.stress_by_day.values()), 3),
              "inv_value": round(val, 2), "inv_cost": round(cost, 2),
              "open_pnl": round(open_pnl, 2), "realized": round(self.realized, 2),
              "trading_pnl": round(trading, 2), "trading_pnl_today": round(trading - self.day_base, 2),
              "fills_today": self.fills_by_day.get(day, 0), "fills_total": sum(self.fills_by_day.values()),
              "markets": sum(1 for m in self.markets.values() if m.active),
              "quoting": sum(1 for m in self.markets.values() if m.orders),
              "bids_usd": round(self.budget_used(), 2),
              "rate_now": round(sum(m.rm.rate * m.last_share for m in self.markets.values()), 2),
              "feed": self.feed_ok(now), "halted": now < self.halted_until}
        st["total_pnl"] = round(st["rewards_total"] + trading, 2)
        top = sorted(self.markets.values(), key=lambda m: -m.reward_total)[:12]
        st["top"] = [{"slug": m.rm.slug[:60], "reward": round(m.reward_total, 3), "share": round(m.last_share, 3),
                      "rate": m.rm.rate, "empty": m.rm.empty} for m in top if m.reward_total > 0]
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
        self.journal.log("rday", day=self.day, rewards=round(self.rewards_by_day.get(self.day, 0.0), 3),
                         trading_pnl_day=round(trading - (self.day_base or 0.0), 2),
                         fills=self.fills_by_day.get(self.day, 0))
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
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _sleep(self, s):
        end = time.time() + s
        while not self._stop and time.time() < end:
            await asyncio.sleep(min(1.0, end - time.time()))

    async def run(self, duration_s=None):
        self.journal.log("start", mode="paper", bot="rewards", budget=self.rc.quote_budget_usd,
                         max_markets=self.rc.max_markets, dist_cents=self.rc.dist_cents)
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
