"""LIVE test of the liquidity-rewards rule: REAL MONEY, deliberately tiny (reports/68, section 12).

One market, the paper bot's own quotes (minimum size on both outcomes), for a few hours. The point is not to earn but
to read from the exchange what paper cannot know: is a lone maker credited the pool, does our share next to other
makers match the formula, is a pool split by the minute or over the day.

What it does NOT do: no taker hedge, no merge, no second market. The first fill ends the test: every order of the
market is cancelled and the shares stay in the wallet until the owner decides (or the market resolves).

Gates, all required (the same as the other live bots, pmbot/bot/live.py):
  1. rewards.mode = "live" in the config AND the --live flag of scripts/run_rewards_live.py
  2. env PMBOT_LIVE_CONFIRM, PMBOT_PRIVATE_KEY
  3. the geoblock check
  4. collateral >= live.max_open_usd + live.reserve_usd (the wallet is shared with the lock bots)

Limits enforced by the broker itself, whatever the quoting code asks for: BUY post-only orders only, at most
live.max_shares a piece, live.max_open_usd resting in total, two resting orders, live.max_orders placed per run, prices
inside [0.05, 0.95], every order expires by itself (GTD) and the order heartbeat is the dead-man switch. Nothing here
ever calls cancel-all: the wallet is shared, so orders are cancelled by id and, as a sweep, by the market's two tokens.
"""
import asyncio
import logging
import os
import time
import tomllib
from dataclasses import dataclass, fields, replace

import requests

from ..bot.live import LiveGateError, check_env, check_geoblock
from . import markets as mk
from .config import RewardsCfg
from .farmer import Farmer, utc_day

log = logging.getLogger("pmbot.rewards.live")


@dataclass
class LiveCfg:
    kind: str = "alone"             # which market to pick: "alone" (nobody else in the reward zone), "shared", "any"
    slug: str = ""                  # exact market slug: skip the choice (it must still pass the limits below)
    minutes: float = 180.0          # the test ends by itself after this long
    max_open_usd: float = 20.0      # both resting bids together
    max_shares: float = 20.0        # one order
    price_lo: float = 0.30          # price of YES of the market we pick: a filled bid then costs at most
    price_hi: float = 0.70          # max_shares * ~0.70
    min_rate: float = 20.0          # $/day pool: 3 hours of a $20 pool are $2.50 by time, above the $1 payout minimum
    calm_days: float = 7.0          # skip questions about a date in the next N days ("... on November 16?" two days
                                    # ahead): the market's own end date is often weeks later, the news is not
    reserve_usd: float = 40.0       # collateral that must stay free on top of our orders (lock bots, same wallet)
    order_ttl_s: float = 1200.0     # every order expires by itself after this long (GTD); refreshed before it does
    refresh_before_s: float = 300.0  # ... when this little of its life is left (the exchange wants >= 3 min ahead)
    heartbeat_s: float = 4.0        # order heartbeat: the exchange cancels our orders ~10 s after the last one
    max_orders: int = 300           # orders placed per run, a loop gone wrong cannot churn
    poll_s: float = 60.0            # how often the exchange's own numbers are read
    stop_on_fill: bool = True


def load_rewards_live_config(path, live_flag=False):
    """(RewardsCfg, LiveCfg) of the live test. Refuses a config that is not marked live, and refuses to run it
    without the --live flag: a paper config can never reach the live broker, and the reverse."""
    raw = tomllib.load(open(path, "rb"))
    sec = dict(raw.get("rewards") or {})
    if "journal_dir" in raw and "journal_dir" not in sec:
        sec["journal_dir"] = raw["journal_dir"]
    unknown = set(sec) - {f.name for f in fields(RewardsCfg)}
    if unknown:
        raise ValueError(f"unknown config key(s) in [rewards]: {sorted(unknown)}")
    rc = RewardsCfg(**sec)
    lsec = dict(raw.get("live") or {})
    unknown = set(lsec) - {f.name for f in fields(LiveCfg)}
    if unknown:
        raise ValueError(f"unknown config key(s) in [live]: {sorted(unknown)}")
    lc = LiveCfg(**lsec)
    if rc.mode != "live":
        raise ValueError("this is the live test: rewards.mode must be \"live\" (paper runs through run_rewards.py)")
    if not live_flag:
        raise LiveGateError("the config says live but the --live flag is missing: refusing to trade real money")
    if lc.kind not in ("alone", "shared", "any"):
        raise ValueError("live.kind must be alone, shared or any")
    if not (0 < lc.max_open_usd <= 60 and 0 < lc.max_shares <= 60 and 0 < lc.minutes <= 24 * 60):
        raise ValueError("live test limits: max_open_usd <= 60, max_shares <= 60, minutes <= 1440 (a test, not a bot)")
    if lc.order_ttl_s - lc.refresh_before_s < 240 or lc.refresh_before_s < 200:
        raise ValueError("live.order_ttl_s / refresh_before_s: an order must be placed >= 3 min before it expires")
    # the test is one market with our two bids and nothing else, whatever the [rewards] section says
    rc = replace(rc, max_markets=1, quote_budget_usd=lc.max_open_usd, hedge=False, reduce_only=False, state_file="",
                 max_fills_per_market_day=1)
    return rc, lc


class LiveOrder:
    """One of our resting orders as the broker believes it (the farmer reads price / remaining / live_at)."""
    __slots__ = ("token", "key", "side", "price", "size", "remaining", "t", "exp")

    def __init__(self, token, key, side, price, size, t, exp):
        self.token, self.key, self.side, self.price, self.size = token, key, side, price, size
        self.remaining, self.t, self.exp = size, t, exp

    def live_at(self, now_ms):
        return self.remaining > 1e-9


class RewardsLiveBroker:
    """Post-only GTD bids for the live test, with its own hard limits. See the module docstring."""
    mode = "live"
    PRICE_LO, PRICE_HI = 0.05, 0.95

    def __init__(self, lc, on_fill=None, journal=None, client=None, geoblock_get=requests.get, env=os.environ,
                 clock=time.time):
        self.lc = lc
        self.on_fill = on_fill
        self.journal = journal
        self.client = client              # injected in tests; created in start() otherwise
        self._geoblock_get = geoblock_get
        self._env = env
        self.clock = clock
        self.orders = {}                  # order id -> LiveOrder, believed resting
        self.known = {}                   # every order id of this run (late fills are attributed through it)
        self.tokens = set()               # tokens we ever placed on: the stop sweep cancels by these
        self.placed = 0
        self.balance_usd = None
        self.fatal = ""                   # 401 / 403 from the API: stop the test
        self.suspect = False              # an order call ended without an answer: an order may rest unknown to us
        self.backoff_until = 0.0
        self.place_after = 0.0            # monotonic: no new order before this (after a refusal by the exchange)
        self.stream_ok = False
        self.hb_ok_at = 0.0
        self._hb_id = ""
        self._tasks = []
        self._seen_fills = set()
        self._stopping = False
        self._lock = asyncio.Lock()

    def log(self, kind, **kw):
        if self.journal:
            self.journal.log(kind, mode="live", **kw)

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        check_env(self._env)
        geo = check_geoblock(self._geoblock_get)
        self.log("geoblock_ok", country=geo.get("country"), region=geo.get("region"))
        if self.client is None:
            from polymarket import AsyncSecureClient      # lazily: tests and paper do not need the SDK
            self.client = await AsyncSecureClient.create(private_key=self._env["PMBOT_PRIVATE_KEY"],
                                                         wallet=self._env.get("PMBOT_WALLET") or None)
        bal = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        self.balance_usd = bal.balance / 1e6
        self.log("balance", pusd=round(self.balance_usd, 2))
        need = self.lc.max_open_usd + self.lc.reserve_usd
        if self.balance_usd < need:
            raise LiveGateError(f"collateral {self.balance_usd:.2f} < {need:.2f} (orders {self.lc.max_open_usd:.0f} + "
                                f"reserve {self.lc.reserve_usd:.0f} for the other live bots)")
        await self._heartbeat_once()
        self._tasks = [asyncio.create_task(self._heartbeat_loop()), asyncio.create_task(self._user_stream())]
        return True

    async def stop(self):
        """Cancel what we placed (by id, then by the market's tokens), check nothing of ours rests, close."""
        self._stopping = True
        left = None
        try:
            try:
                await self.cancel(list(self.orders), None)
            except Exception as e:  # noqa: BLE001
                self.log("rlive_error", where="stop_cancel", error=str(e)[:200])
            for attempt in range(3):                       # an order call cut off in flight may land a moment later
                await self.sweep()
                await asyncio.sleep(1.0)
                left = await self.open_orders()
                if left == 0:
                    break
            if left:
                self.log("rlive_error", where="stop", error=f"{left} orders still open after the sweep")
        finally:
            for t in self._tasks:
                t.cancel()
            close = getattr(self.client, "close", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass
        return left

    async def sweep(self):
        """Cancel every open order of ours on the tokens of the test market (never cancel-all: the wallet is shared)."""
        n = 0
        for tok in sorted(self.tokens):
            for attempt in range(3):
                try:
                    resp = await self.client.cancel_market_orders(asset_id=tok)
                    n += len(getattr(resp, "canceled", ()) or ())
                    break
                except Exception as e:  # noqa: BLE001
                    self.log("rlive_error", where="sweep", error=str(e)[:200], attempt=attempt)
                    await asyncio.sleep(1.0)
        self.orders.clear()
        self.log("rlive_sweep", canceled=n, tokens=len(self.tokens))
        return n

    async def open_orders(self):
        """How many orders of ours still rest on the test market's tokens (None = could not ask)."""
        n = 0
        try:
            for tok in sorted(self.tokens):
                async for _ in self.client.list_open_orders(asset_id=tok).iter_items():
                    n += 1
        except Exception as e:  # noqa: BLE001
            self.log("rlive_error", where="open_orders", error=str(e)[:200])
            return None
        return n

    # ------------------------------------------------------------ heartbeat (dead-man switch)
    async def _heartbeat_once(self):
        from polymarket import OrderHeartbeatMismatchError
        async with self._lock:
            try:
                hb = await self.client.send_order_heartbeat(heartbeat_id=self._hb_id)
            except OrderHeartbeatMismatchError as e:
                hb = await self.client.send_order_heartbeat(heartbeat_id=e.heartbeat_id)
            self._hb_id = hb.heartbeat_id
            self.hb_ok_at = time.monotonic()

    async def _heartbeat_loop(self):
        while not self._stopping:
            await asyncio.sleep(self.lc.heartbeat_s)
            try:
                await self._heartbeat_once()
            except Exception as e:  # noqa: BLE001
                self.note_error("heartbeat", e)
                self.log("heartbeat_error", error=str(e)[:200])

    def health(self):
        """(ok, reason). Not ok = rest nothing (the farmer pulls its quotes)."""
        if self.fatal:
            return False, "broker_fatal"
        if self.suspect:
            return False, "order_outcome_unknown"
        now = time.monotonic()
        if now < self.backoff_until:
            return False, "api_backoff"
        if not self.stream_ok:
            return False, "user_stream_down"
        if self.hb_ok_at and now - self.hb_ok_at > 2 * self.lc.heartbeat_s + 1:
            return False, "heartbeat_down"
        return True, ""

    def note_error(self, op, e):
        status = getattr(e, "status", None)
        if status in (401, 403):
            self.fatal = f"{op}: HTTP {status} {str(e)[:150]}"
            self.log("broker_fatal", op=op, status=status, error=str(e)[:300])
            return
        wait = float(getattr(e, "retry_after", None) or (5.0 if type(e).__name__ == "RateLimitError" else 2.0))
        self.backoff_until = max(self.backoff_until, time.monotonic() + wait)
        self.log("api_error", op=op, err=type(e).__name__, status=status, error=str(e)[:300], backoff_s=wait)

    # ------------------------------------------------------------ orders
    def open_usd(self):
        return sum(o.price * o.remaining for o in self.orders.values())

    def refuse(self, why, **kw):
        self.log("rlive_refused", why=why, **kw)
        return None

    async def place(self, token, key, side, price, size, now_ms, queue_ahead=None):
        """One post-only GTD BUY, or None (refused by our limits or by the exchange; nothing rests then)."""
        lc = self.lc
        if self._stopping or self.fatal or self.suspect or time.monotonic() < self.place_after:
            return None
        if side not in ("Y", "N") or not token:
            return self.refuse("bad_side", side=side)
        if not self.PRICE_LO <= price <= self.PRICE_HI:
            return self.refuse("price", price=price)
        if size <= 0 or size > lc.max_shares + 1e-9:
            return self.refuse("size", size=size)
        if self.open_usd() + price * size > lc.max_open_usd + 1e-9:
            return self.refuse("max_open_usd", open=round(self.open_usd(), 2), add=round(price * size, 2))
        if len(self.orders) >= 2 or any(o.token == token for o in self.orders.values()):
            return self.refuse("two_orders", n=len(self.orders))
        if self.placed >= lc.max_orders:
            self.fatal = self.fatal or f"max_orders {lc.max_orders} reached"
            return self.refuse("max_orders", placed=self.placed)
        now = self.clock()
        exp = int(now + lc.order_ttl_s)
        self.placed += 1
        self.tokens.add(token)
        t0 = time.perf_counter()
        try:
            resp = await self.client.place_limit_order(asset_id=token, side="BUY", price=f"{price:.2f}",
                                                       size=f"{size:.2f}", post_only=True, expiration=exp)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status", None) == 400:         # refused, nothing was created
                msg = str(e)
                cross = "crosses book" in msg
                self.log("order_rejected", token=token, key=key, side=side, price=price, size=size,
                         code="post_only_would_cross" if cross else "http_400", message=msg[:200])
                self.place_after = time.monotonic() + (5 if cross else 30)     # a refusal is not retried every 2 s
                return None
            self.note_error("place", e)
            self.suspect = True                            # no answer: the order may exist. Stop and sweep the market
            self.log("rlive_error", where="place", error=str(e)[:200])
            return None
        lat = round((time.perf_counter() - t0) * 1000, 1)
        if not getattr(resp, "ok", False):
            code = getattr(resp, "code", "?")
            self.log("order_rejected", token=token, key=key, side=side, price=price, size=size, latency_ms=lat,
                     code=code, message=str(getattr(resp, "message", ""))[:200])
            self.place_after = time.monotonic() + (60 if code == "not_enough_balance" else 30)
            return None
        o = LiveOrder(token, key, side, price, size, now, exp)
        self.orders[resp.order_id] = o
        self.known[resp.order_id] = o
        self.log("order_placed", order_id=resp.order_id, token=token, key=key, side=side, price=price, size=size,
                 expiration=exp, latency_ms=lat, status=getattr(resp, "status", None))
        return resp.order_id

    async def cancel(self, oids, now_ms):
        oids = [o for o in oids if o in self.orders]
        if not oids:
            return
        try:
            resp = await self.client.cancel_orders(order_ids=oids)
        except Exception as e:  # noqa: BLE001
            self.note_error("cancel", e)
            raise
        for oid in getattr(resp, "canceled", ()) or ():
            self.orders.pop(oid, None)
        nc = getattr(resp, "not_canceled", {}) or {}
        self.log("order_cancelled", order_ids=list(getattr(resp, "canceled", ()) or ()),
                 not_canceled={k: str(v)[:80] for k, v in nc.items()})
        for oid in nc:                                     # matched or gone already: ask the exchange what it filled
            self.orders.pop(oid, None)
            await self.account(oid)

    async def account(self, oid):
        """Compare one order of this run with the exchange's own record and count what matched without a stream event
        (the stream can miss a trade while it reconnects; an uncounted fill would let the test quote again)."""
        o = self.known.get(oid)
        if o is None:
            return 0.0
        try:
            x = await self.client.get_order(order_id=oid)
        except Exception as e:  # noqa: BLE001
            self.log("rlive_error", where="get_order", error=str(e)[:200])
            return 0.0
        matched = float(getattr(x, "size_matched", 0) or 0)
        q = matched - (o.size - o.remaining)
        status = str(getattr(x, "status", "") or "").upper()
        if oid in self.orders and any(w in status for w in ("CANCEL", "EXPIRE", "INVALID")):
            self.orders.pop(oid, None)                     # the exchange removed it (heartbeat lapse, expiry): place again
            self.log("order_gone", order_id=oid, status=status)
        if q <= 1e-9:
            return 0.0
        o.remaining -= q
        if o.remaining <= 1e-9:
            self.orders.pop(oid, None)
        fill = {"order_id": oid, "token": o.token, "key": o.key, "side": o.side, "price": o.price, "size": q,
                "ts": int(time.time() * 1000), "trade_id": None, "status": "RECONCILED"}
        self.log("fill", **fill)
        if self.on_fill:
            self.on_fill(fill)
        return q

    async def reconcile(self):
        """account() for every order we believe resting. Returns the shares found filled."""
        tot = 0.0
        for oid in list(self.orders):
            tot += await self.account(oid)
        return tot

    def gc(self, now_ms):
        """Forget orders whose own expiry passed (the exchange removed them)."""
        now = self.clock()
        for oid in [k for k, o in self.orders.items() if now >= o.exp]:
            self.orders.pop(oid, None)

    def on_public_trade(self, *a, **kw):
        pass                                               # live fills come from the authenticated user stream

    # ------------------------------------------------------------ fills
    async def _user_stream(self):
        from polymarket.streams import UserSpec
        while not self._stopping:
            try:
                async with await self.client.subscribe([UserSpec()]) as stream:
                    self.stream_ok = True
                    self.log("user_stream_up")
                    async for ev in stream:
                        if getattr(ev, "type", None) == "trade":
                            self.handle_trade(ev.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("user_stream_error", error=str(e)[:200])
            self.stream_ok = False
            if not self._stopping:
                await asyncio.sleep(2)

    def handle_trade(self, pl):
        """A trade of the account: count the part matched against orders of this run (the same rule as LiveBroker)."""
        status = str(getattr(pl, "status", "") or "")
        for mo in getattr(pl, "maker_orders", None) or ():
            o = self.known.get(mo.order_id)
            uid = (pl.id, mo.order_id)
            if o is None or uid in self._seen_fills:
                continue
            if status.upper() == "FAILED":
                self.log("fill_failed", trade_id=pl.id, order_id=mo.order_id)
                continue
            self._seen_fills.add(uid)
            q, px = min(float(mo.matched_amount), o.remaining), float(mo.price)
            if q <= 1e-9:
                continue                                   # already counted from the exchange's record (account())
            o.remaining -= q
            if o.remaining <= 1e-9:
                self.orders.pop(mo.order_id, None)
            fill = {"order_id": mo.order_id, "token": o.token, "key": o.key, "side": o.side, "price": px, "size": q,
                    "ts": int(time.time() * 1000), "trade_id": pl.id, "status": status}
            self.log("fill", **fill)
            if self.on_fill:
                self.on_fill(fill)

    # ------------------------------------------------------------ what the exchange says about rewards
    async def reward_state(self, cid, oids):
        """{"pct": our share of the market's rewards right now (%), "scoring": {order id: bool}}; a missing key means
        that call failed (journalled) -- the numbers are the reason for the test, a failure must not look like zero."""
        out = {}
        try:
            pct = await self.client.get_reward_percentages()
            out["pct"] = float(pct.get(cid, 0.0))
            out["markets_with_pct"] = sum(1 for v in pct.values() if v)
        except Exception as e:  # noqa: BLE001
            self.log("rlive_error", where="percentages", error=str(e)[:200])
        if oids:
            try:
                sc = await self.client.get_orders_scoring(order_ids=list(oids))
                out["scoring"] = {k: bool(v) for k, v in sc.items()}
            except Exception as e:  # noqa: BLE001
                self.log("rlive_error", where="scoring", error=str(e)[:200])
        return out

    async def earnings(self, date):
        """{condition id: $ credited for that UTC date} as the exchange reports it, None when the call failed."""
        try:
            out = {}
            async for e in self.client.list_user_earnings_for_day(date=date).iter_items():
                out[str(e.condition_id)] = out.get(str(e.condition_id), 0.0) + float(e.earnings)
            return out
        except Exception as e:  # noqa: BLE001
            self.log("rlive_error", where="earnings", error=str(e)[:200])
            return None


def choose_market(picks, lc, now=None):
    """The first of the ranked picks that fits the test, or None. `picks` come from mk.select (book priced)."""
    now = now if now is not None else time.time()
    for c in picks:
        if lc.calm_days and mk.date_passed(c.question, now + lc.calm_days * 86400):
            continue                                       # every date it names is within the next days: news market
        if lc.slug and c.slug != lc.slug:
            continue
        if abs(c.tick - 0.01) > 1e-9 or c.size > lc.max_shares + 1e-9 or c.capital > lc.max_open_usd + 1e-9:
            continue
        ref = (c.extra or {}).get("ref")                   # the price our quotes would be centred on (mk.price_market)
        if ref is None or not lc.price_lo <= ref <= lc.price_hi or c.rate < lc.min_rate:
            continue
        if c.neg_risk:                                     # another exchange contract: not in this test
            continue
        if (lc.kind == "alone" and not c.empty) or (lc.kind == "shared" and c.empty):
            continue
        return c
    return None


class LiveFarmer(Farmer):
    """The paper farmer's quoting on ONE market with real orders. Everything that only makes sense on paper is off:
    no simulated fills, no simulated hedge or merge. The first fill stops the test."""

    def __init__(self, rc, lc, journal, broker, clock=time.time, select=mk.select):
        super().__init__(rc, journal, clock)
        self.lc = lc
        self.broker = broker
        broker.on_fill = self.on_fill
        self._select = select
        self.market = None                # the RMarket of the test
        self.exch = []                    # samples of the exchange's numbers while we quote
        self.why_stop = ""
        self.t_start = None

    # ---- the book without ourselves: in live our own bids sit in the public book the quoting code reads
    def levels(self, token):
        bids, asks = super().levels(token)
        mine_b, mine_a = {}, {}
        for o in self.broker.orders.values():
            if o.remaining <= 1e-9:
                continue
            if o.token == token:                           # our bid on this token
                mine_b[round(o.price, 4)] = mine_b.get(round(o.price, 4), 0.0) + o.remaining
            elif self._other(o.token) == token:            # our bid on the other outcome shows here as an ask
                p = round(1 - o.price, 4)
                mine_a[p] = mine_a.get(p, 0.0) + o.remaining
        if mine_b:
            bids = [(p, s - mine_b.get(round(p, 4), 0.0)) for p, s in bids]
            bids = [(p, s) for p, s in bids if s > 1e-9]
        if mine_a:
            asks = [(p, s - mine_a.get(round(p, 4), 0.0)) for p, s in asks]
            asks = [(p, s) for p, s in asks if s > 1e-9]
        return bids, asks

    def cross_fills(self, ms, now_ms):
        return 0.0                                         # real fills come from the exchange

    def hedge_once(self):
        return 0                                           # no simulated hedge / merge: what fills stays in the wallet

    def _next_hedge_in(self):
        return None

    def targets(self, ms, now):
        ok, why = self.broker.health()
        if not ok or self.why_stop:
            return {}
        return super().targets(ms, now)

    def _keep(self, ms, side, cur, price, now):
        o = self.broker.orders.get(cur["oid"])
        if o is None:
            return False                                   # expired or cancelled by the exchange: place again
        if o.exp - now < self.lc.refresh_before_s:
            return False                                   # near its own expiry: a fresh one before it lapses
        return super()._keep(ms, side, cur, price, now)

    def on_fill(self, fill):
        super().on_fill(fill)
        self.journal.log("rlive_fill", key=fill["key"], side=fill["side"], price=fill["price"], size=fill["size"],
                         question=self.market.question if self.market else "")
        if self.lc.stop_on_fill and float(fill["size"]) >= self.rc.min_fill_shares:
            self.finish("fill")

    def finish(self, why):
        if not self.why_stop:
            self.why_stop = why
        self._stop = True
        self._wake.set()

    # ---- one market, chosen once
    async def selector(self):
        while not self._stop and self.market is None:
            try:
                wide = replace(self.rc, max_markets=40, quote_budget_usd=1e9, max_empty_frac=1.0,
                               min_rate=max(self.rc.min_rate, self.lc.min_rate))
                picks = await asyncio.to_thread(self._select, wide, None)
                c = choose_market(picks, self.lc)
                if c is None:
                    self.journal.log("rlive_no_market", test_kind=self.lc.kind, looked_at=len(picks))
                    self.finish("no_market")
                    return
                self.market = c
                self.apply_selection([c])
                self.journal.log("rlive_market", key=c.slug, question=c.question, cid=c.condition_id, rate=c.rate,
                                 comp=round(c.comp, 3), empty=c.empty, v=c.v, min_size=c.min_size, size=c.size,
                                 price=c.price, capital=round(c.capital, 2), est_reward_day=round(c.est_reward, 2),
                                 test_kind=self.lc.kind, minutes=self.lc.minutes, vol=c.vol)
            except Exception as e:  # noqa: BLE001
                log.warning("selection failed: %s", e)
                self.journal.log("rerror", where="select", error=str(e)[:300])
                await self._sleep(30)
        while not self._stop:
            await self._sleep(5)

    # ---- the exchange's own numbers, next to the paper estimate
    async def exchange_once(self):
        ms = self.markets.get(self.market.slug) if self.market else None
        if ms is None:
            return None
        await self.broker.reconcile()                      # a fill the stream missed ends the test here
        if self._stop:
            return None
        oids = {o["oid"]: side for side, o in ms.orders.items()}
        st = await self.broker.reward_state(self.market.condition_id, list(oids))
        rec = {"key": self.market.slug, "paper_share": round(ms.last_share, 4), "alone": bool(ms.alone),
               "ref": round(ms.ref, 4) if ms.ref is not None else None,
               "bids": {side: o["price"] for side, o in ms.orders.items()},
               "age_s": {side: round(self.clock() - o["t"]) for side, o in ms.orders.items()},
               "paper_reward": round(ms.reward_total, 4), "quoted_min": round(ms.quoted_s / 60, 1)}
        if "pct" in st:
            rec["exch_pct"] = st["pct"]
            rec["markets_with_pct"] = st.get("markets_with_pct")
        if "scoring" in st:
            rec["scoring"] = {oids[k]: v for k, v in st["scoring"].items() if k in oids}
        self.journal.log("rlive", **rec)
        if len(ms.orders) == 2:
            self.exch.append(rec)
        return rec

    async def exchange_loop(self):
        while not self._stop:
            await self._sleep(self.lc.poll_s)
            if self._stop:
                break
            try:
                await self.exchange_once()
            except Exception as e:  # noqa: BLE001
                self.journal.log("rlive_error", where="exchange_loop", error=str(e)[:200])

    async def watchdog(self):
        """Stop on a fatal broker error, on an order whose outcome is unknown, on the kill file or the day stop."""
        while not self._stop:
            await asyncio.sleep(1)
            if self.broker.fatal:
                self.finish("broker_fatal")
            elif self.broker.suspect:
                self.finish("order_outcome_unknown")
            elif self.rc.kill_file and os.path.exists(self.rc.kill_file):
                self.finish("kill_file")
            elif self.clock() < self.halted_until:
                self.finish("day_stop")

    def summary(self, left):
        ms = self.markets.get(self.market.slug) if self.market else None
        pcts = [r["exch_pct"] for r in self.exch if "exch_pct" in r]
        paper = [r["paper_share"] for r in self.exch]
        scoring = [all(r["scoring"].values()) for r in self.exch if r.get("scoring")]
        q_min = ms.quoted_s / 60 if ms else 0.0
        rate = self.market.rate if self.market else 0.0
        mean_pct = sum(pcts) / len(pcts) if pcts else None
        ps = self.pos.get(self.market.slug) if self.market else None
        return {"why": self.why_stop or "time", "key": self.market.slug if self.market else None,
                "question": self.market.question if self.market else None,
                "cid": self.market.condition_id if self.market else None, "rate": rate,
                "test_kind": self.lc.kind, "quoted_min": round(q_min, 1), "samples": len(self.exch),
                "exch_pct_mean": round(mean_pct, 2) if mean_pct is not None else None,
                "exch_pct_last": pcts[-1] if pcts else None,
                "paper_share_mean": round(sum(paper) / len(paper), 4) if paper else None,
                "scoring_all": (sum(scoring) / len(scoring)) if scoring else None,
                "paper_reward": round(ms.reward_total, 4) if ms else 0.0,
                "paper_alone": round(ms.alone_total, 4) if ms else 0.0,
                "by_time_exch": round(rate * mean_pct / 100 * q_min / 1440, 4) if mean_pct is not None else None,
                "whole_pool": rate, "fills": sum(self.fills_by_day.values()),
                "position": {"yes": round(ps.yes, 2), "no": round(ps.no, 2), "cost": round(ps.cost, 2)} if ps else None,
                "orders_placed": self.broker.placed, "orders_left_open": left,
                "day": utc_day(self.t_start or self.clock())}

    async def run(self, duration_s=None):
        """Gates, the read-only reward calls first, then quote until the time is up, a fill, or a stop."""
        duration_s = duration_s if duration_s is not None else self.lc.minutes * 60
        await self.broker.start()                          # raises LiveGateError: nothing was placed
        self.t_start = self.clock()
        self.journal.log("start", mode="live", bot="rewards-live", test_kind=self.lc.kind, minutes=self.lc.minutes,
                         max_open_usd=self.lc.max_open_usd, max_shares=self.lc.max_shares,
                         balance=round(self.broker.balance_usd, 2))
        yday = utc_day(self.t_start - 86400)
        check = await self.broker.reward_state("", [])     # a read-only call before any order: are these endpoints ours
        self.journal.log("rlive_check", pct_ok="pct" in check, markets_with_pct=check.get("markets_with_pct"),
                         earnings_yesterday=await self.broker.earnings(yday))
        tasks = [asyncio.create_task(x) for x in (self.feed.run(), self.selector(), self.quoter(), self.sampler(),
                                                    self.status_loop(), self.exchange_loop(), self.watchdog())]
        left = None
        try:
            t0 = time.time()
            while not self._stop and time.time() - t0 < duration_s:
                await asyncio.sleep(0.2)
            if not self.why_stop:                          # the parent's quote loop also stops on the kill file
                self.why_stop = ("time" if time.time() - t0 >= duration_s else
                                 "kill_file" if self.rc.kill_file and os.path.exists(self.rc.kill_file) else "stopped")
            if self.why_stop == "time":
                try:
                    await self.exchange_once()             # one last reading while the orders still rest
                except Exception:  # noqa: BLE001
                    pass
            self._stop = True
        finally:
            self._stop = True
            self.broker._stopping = True                   # no new order from here on, whatever is still running
            for t in tasks:                                # the quote loop first: it must not place while we cancel
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ms in self.markets.values():
                try:
                    await self.pull(ms, "stop")
                except Exception as e:  # noqa: BLE001
                    self.journal.log("rlive_error", where="pull", error=str(e)[:200])
            left = await self.broker.stop()
        st = self.status()
        self.journal.log("rstatus", **st)
        self.journal.log("rmarkets", markets=self.market_stats())
        done = self.summary(left)
        self.journal.log("rlive_done", **done)
        return done
