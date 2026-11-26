"""LIVE broker of the maker (Мейкер, reports/89 and 94): REAL MONEY, the exchange instead of PaperBroker for
pmbot/bot/fairmaker.py. A test at the exchange minimum, not a scaled bot.

What the paper maker cannot know and this measures: how often a resting 5-share bid is really filled (other makers see our
bid and step in front; the paper bid is invisible), how far the price moves against our fills with our real place / cancel
latency (paper assumes 60 / 50 ms), and the exchange's maker rebate (not counted on paper).

Gates, all required (the same as the other live bots, pmbot/bot/live.py):
  1. execution.mode = "live" in the config AND the --live flag of scripts/run_maker.py (fairmaker.load_maker_config)
  2. env PMBOT_LIVE_CONFIRM, PMBOT_PRIVATE_KEY
  3. the geoblock check
  4. collateral >= live.max_resting_usd + live.reserve_usd (the wallet is shared with the lock bots)

Limits enforced by the broker itself, whatever the quoting code asks for:
  * BUY, post-only (never takes, never pays a taker fee), GTD (expires by itself), price in [0.05, 0.95], at most
    live.max_shares a piece, at most one resting order per token and two in all, live.max_resting_usd resting in all;
  * at most live.max_orders_hour orders placed in any rolling hour (a loop gone wrong cannot churn);
  * the order heartbeat is the dead-man switch: if the process dies or hangs, the exchange cancels our orders ~10 s later;
  * NOTHING here calls cancel-all: the wallet is shared with the lock bots. Orders are cancelled by id; a sweep cancels by the
    tokens we quoted (the lock bots send only FAK orders, which never rest, so a sweep of a token cancels only ours);
  * an order call that ended without an answer means an order may rest unknown to us: no new order until the tokens of that
    window were swept, and a fill of an order of ours we did not know (maker_address = our wallet, on a token we quoted) is
    still counted ("fill_adopted");
  * fills come from the account's trade stream, counted once per (trade, order); the exchange's record (get_order) is read for
    every resting order every live.reconcile_every_s and for every order a cancel could not cancel, so a fill the stream missed
    is counted too; a trade that FAILED after it was counted is taken back (on_unfill).
The private key is read from the environment only and never logged.
"""
import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass

import requests

from .live import LiveGateError, check_env, check_geoblock

log = logging.getLogger("pmbot.maker.live")


@dataclass
class MakerLiveCfg:
    """[live] of a live maker config (config/maker_live.toml). load_maker_config caps every value (a test, not a bot)."""
    max_shares: float = 5.0              # one order (the exchange minimum is 5)
    max_resting_usd: float = 10.0        # all resting bids together (two 5-share bids at <= 0.90 = $9)
    reserve_usd: float = 60.0            # collateral that must stay free for the lock bots (the same wallet)
    max_orders_hour: int = 4000          # orders placed in any rolling hour: a runaway guard, not a quota (the paper twin
                                         # maker_safe placed 1330 an hour on average over 40.6 h, ~2x that in busy hours)
    ttl_s: int = 185                     # GTD: expiration = max(now + ttl_s, window end + 60); the SDK wants >= now + 180
    heartbeat_s: float = 4.0             # the exchange cancels our orders ~10 s after the last heartbeat
    cross_pause_ms: int = 300            # after a post-only refusal ("crosses book") no new order on that token this long
    reject_pause_s: float = 30.0         # ... after any other refusal of the exchange
    balance_every_s: float = 60.0        # how often the collateral is read
    reconcile_every_s: float = 20.0      # get_order for each resting order (a fill the stream missed)
    rebate_every_s: float = 3600.0       # the exchange's maker rebate of the day (get_current_rebates)


class LiveOrder:
    """One of our orders as the broker believes it."""
    __slots__ = ("token", "key", "side", "price", "size", "remaining", "t", "exp")

    def __init__(self, token, key, side, price, size, t, exp):
        self.token, self.key, self.side, self.price, self.size = token, key, side, price, size
        self.remaining, self.t, self.exp = size, t, exp


class MakerLiveBroker:
    """Post-only GTD bids of the live maker, with its own hard limits. See the module docstring."""
    mode = "live"
    PRICE_LO, PRICE_HI = 0.05, 0.95

    def __init__(self, lc: MakerLiveCfg, on_fill=None, on_unfill=None, journal=None, client=None,
                 geoblock_get=requests.get, env=os.environ, clock=time.time, mono=time.monotonic):
        self.lc = lc
        self.on_fill = on_fill
        self.on_unfill = on_unfill
        self.journal = journal
        self.client = client              # injected in tests; created in start() otherwise
        self._geoblock_get = geoblock_get
        self._env = env
        self.clock = clock                # wall clock (s): GTD expirations
        self.mono = mono                  # monotonic (s): pauses, rates
        self.orders = {}                  # order id -> LiveOrder, believed resting
        self.known = {}                   # every order id of this run (late fills are attributed through it)
        self.tokens = {}                  # token -> (window key, side, window end s) of every token we quoted
        self.placed_t = collections.deque()   # monotonic times of the placements of the last hour
        self.pause = {}                   # token -> monotonic: no new order on it before then (a refusal)
        self.balance_usd = None
        self.wallet = ""                  # our maker address (lower case): adopts a fill of an order we lost track of
        self.fatal = ""                   # 401 / 403 from the API, or a limit that must stop the run
        self.suspect = set()              # tokens where an order call ended without an answer (an order may rest unknown)
        self.backoff_until = 0.0
        self.stream_ok = False
        self.hb_ok_at = 0.0
        self._hb_id = ""
        self._tasks = []
        self._counted = {}                # (trade id, order id) -> shares counted for it
        self._stopping = False
        self._lock = asyncio.Lock()
        self._refused = {}                # (token, why) -> monotonic of the last "mm_refused" record (no flood)
        self._reconcile_now = False       # the account stream just (re)connected: read the exchange's record of our orders at once
        self.stats = {"placed": 0, "rejected": 0, "crossed": 0, "cancels": 0, "cancel_late": 0, "fills": 0,
                      "adopted": 0, "reconciled": 0, "failed": 0, "place_ms": None, "cancel_ms": None}
        self._lat = {"place": [], "cancel": []}

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
        self.wallet = str(getattr(self.client, "wallet", None) or self._env.get("PMBOT_WALLET") or "").lower()
        await self.read_balance()
        need = self.lc.max_resting_usd + self.lc.reserve_usd
        if self.balance_usd is None or self.balance_usd < need:
            raise LiveGateError(f"collateral {self.balance_usd} < {need:.2f} (bids {self.lc.max_resting_usd:.0f} + reserve "
                                f"{self.lc.reserve_usd:.0f} for the other live bots)")
        await self._heartbeat_once()
        self._tasks = [asyncio.create_task(self._heartbeat_loop()), asyncio.create_task(self._user_stream()),
                       asyncio.create_task(self._upkeep_loop())]
        return True

    async def quiesce(self, why="stop"):
        """Cancel every order of ours (by id, then a sweep of the tokens we quoted); the heartbeat goes on. Returns how many of
        ours still rest according to the exchange (None = could not ask)."""
        try:
            await self.cancel(list(self.orders), None)
        except Exception as e:  # noqa: BLE001
            self.log("mm_live_error", where="quiesce_cancel", error=str(e)[:200])
        left = None
        for _ in range(3):                                 # an order call cut off in flight may land a moment later
            await self.sweep(why=why)
            await asyncio.sleep(1.0)
            left = await self.open_orders()
            if left == 0:
                break
        if left:
            self.log("mm_live_error", where="quiesce", error=f"{left} orders of ours still open after the sweep "
                     "(the exchange cancels them ~10 s after our last heartbeat)")
        return left

    async def stop(self):
        self._stopping = True
        try:
            await self.quiesce("stop")
        finally:
            for t in self._tasks:
                t.cancel()
            close = getattr(self.client, "close", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

    def _live_tokens(self, now_s=None):
        """Tokens of windows that have not ended yet (or ended < 2 min ago): the ones a sweep looks at."""
        now_s = self.clock() if now_s is None else now_s
        return sorted(t for t, (_, _, end) in self.tokens.items() if end + 120 > now_s)

    async def sweep(self, tokens=None, why=""):
        """Cancel every open order of ours on the given tokens (default: the windows we quote). Never cancel-all."""
        n = 0
        toks = sorted(tokens) if tokens is not None else self._live_tokens()
        for tok in toks:
            for attempt in range(3):
                try:
                    resp = await self.client.cancel_market_orders(asset_id=tok)
                    n += len(getattr(resp, "canceled", ()) or ())
                    break
                except Exception as e:  # noqa: BLE001
                    self.log("mm_live_error", where="sweep", error=str(e)[:200], attempt=attempt)
                    await asyncio.sleep(1.0)
        for oid in [k for k, o in self.orders.items() if o.token in toks]:
            self.orders.pop(oid, None)
        self.log("mm_sweep", canceled=n, tokens=len(toks), why=why)
        return n

    async def open_orders(self, tokens=None):
        """How many orders of ours rest on the tokens (default: the windows we quote); None = could not ask."""
        n = 0
        try:
            for tok in (sorted(tokens) if tokens is not None else self._live_tokens()):
                async for _ in self.client.list_open_orders(asset_id=tok).iter_items():
                    n += 1
        except Exception as e:  # noqa: BLE001
            self.log("mm_live_error", where="open_orders", error=str(e)[:200])
            return None
        return n

    # ------------------------------------------------------------ heartbeat (dead-man switch), health, upkeep
    async def _heartbeat_once(self):
        from polymarket import OrderHeartbeatMismatchError
        async with self._lock:
            try:
                hb = await self.client.send_order_heartbeat(heartbeat_id=self._hb_id)
            except OrderHeartbeatMismatchError as e:
                hb = await self.client.send_order_heartbeat(heartbeat_id=e.heartbeat_id)
            self._hb_id = hb.heartbeat_id
            self.hb_ok_at = self.mono()

    async def _heartbeat_loop(self):
        while not self._stopping:
            await asyncio.sleep(self.lc.heartbeat_s)
            try:
                await self._heartbeat_once()
            except Exception as e:  # noqa: BLE001
                self.note_error("heartbeat", e)
                self.log("heartbeat_error", error=str(e)[:200])

    def health(self):
        """(ok, reason). Not ok = no new order (the maker pulls what rests)."""
        if self.fatal:
            return False, "broker_fatal"
        if self.suspect:
            return False, "order_outcome_unknown"
        now = self.mono()
        if now < self.backoff_until:
            return False, "api_backoff"
        if not self.stream_ok:
            return False, "user_stream_down"
        if not self.hb_ok_at or now - self.hb_ok_at > 2 * self.lc.heartbeat_s + 1:
            return False, "heartbeat_down"     # the exchange cancels everything ~10 s after the last heartbeat
        if self.balance_usd is not None and self.balance_usd < self.lc.reserve_usd:
            return False, "low_balance"
        return True, ""

    def note_error(self, op, e):
        status = getattr(e, "status", None)
        if status in (401, 403):
            self.fatal = f"{op}: HTTP {status} {str(e)[:150]}"
            self.log("broker_fatal", op=op, status=status, error=str(e)[:300])
            return
        wait = float(getattr(e, "retry_after", None) or (5.0 if type(e).__name__ == "RateLimitError" else 2.0))
        self.backoff_until = max(self.backoff_until, self.mono() + wait)
        self.log("api_error", op=op, err=type(e).__name__, status=status, error=str(e)[:300], backoff_s=wait)

    async def read_balance(self):
        try:
            bal = await self.client.get_balance_allowance(asset_type="COLLATERAL")
            self.balance_usd = round(bal.balance / 1e6, 4)
            self.log("mm_balance", pusd=self.balance_usd)
        except Exception as e:  # noqa: BLE001
            self.note_error("balance", e)
        return self.balance_usd

    async def _upkeep_loop(self):
        """Balance, the exchange's record of resting orders, unknown-order sweeps and the day's maker rebate."""
        last = {"bal": self.mono(), "rec": self.mono(), "reb": self.mono() - self.lc.rebate_every_s + 300}
        while not self._stopping:
            await asyncio.sleep(1.0)
            now = self.mono()
            try:
                if self.suspect:
                    await self.resolve_suspect()
                if self._reconcile_now or now - last["rec"] >= self.lc.reconcile_every_s:
                    self._reconcile_now = False
                    last["rec"] = now
                    await self.reconcile()
                if now - last["bal"] >= self.lc.balance_every_s:
                    last["bal"] = now
                    await self.read_balance()
                if now - last["reb"] >= self.lc.rebate_every_s:
                    last["reb"] = now
                    await self.rebates()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("mm_live_error", where="upkeep", error=str(e)[:200])

    async def resolve_suspect(self):
        """Sweep the tokens where an order may rest unknown to us; quoting resumes once the exchange shows none of ours there.
        A fill of such an order still reaches us through the stream (adopted by our maker address)."""
        toks = set(self.suspect)
        await self.sweep(toks, why="suspect")
        left = await self.open_orders(toks)
        if left == 0:
            self.suspect -= toks
            self.log("mm_suspect_cleared", tokens=len(toks))

    async def rebates(self):
        """The exchange's maker rebate of the UTC day for our address (journal mm_rebate); a failure is journalled, not zero."""
        if not self.wallet:
            return None
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        try:
            rows = await self.client.get_current_rebates(date=day, maker_address=self.wallet)
        except Exception as e:  # noqa: BLE001
            self.log("mm_rebate", day=day, error=str(e)[:200])
            return None
        usd = sum(float(getattr(r, "rebated_fees_usdc", 0) or 0) for r in rows or ())     # polymarket RebatedFee, per market
        self.log("mm_rebate", day=day, usd=round(usd, 6), rows=len(rows or ()))
        return usd

    # ------------------------------------------------------------ orders
    def open_usd(self):
        return sum(o.price * o.remaining for o in self.orders.values())

    async def prewarm(self, token):
        """Let the SDK resolve and cache the market metadata of a new window (tick size, fee rate, neg-risk: two lookups, cached
        10 min for both tokens) before its first bid: create_limit_order() signs an order that is never posted. LockBot calls it
        when the window appears (~20 s before the first quote). Returns ms, None on an error (the order path then looks it up)."""
        t0 = time.perf_counter()
        try:
            await self.client.create_limit_order(asset_id=token, side="BUY", price="0.01", size="5", post_only=True,
                                                 expiration=int(self.clock()) + 600)
        except Exception as e:  # noqa: BLE001
            self.log("prewarm_error", err=type(e).__name__, error=str(e)[:200])
            return None
        return round((time.perf_counter() - t0) * 1000, 1)

    def is_resting(self, oid):
        """False once the exchange removed the order (filled, cancelled by a heartbeat lapse, expired): the maker bids again."""
        return oid in self.orders

    def refuse(self, why, token="", **kw):
        now = self.mono()
        k = (token, why)
        if now - self._refused.get(k, -1e18) >= 10.0:     # the maker asks every pass: one record per 10 s and reason
            self._refused[k] = now
            self.log("mm_refused", why=why, **kw)
        return None

    def _note_lat(self, op, ms):
        xs = self._lat[op]
        xs.append(ms)
        if len(xs) > 200:
            del xs[:100]
        s = sorted(xs)
        self.stats[op + "_ms"] = s[len(s) // 2]

    async def place(self, token, key, side, price, size, now_ms, expiration_s=None, queue_ahead=None, end_s=None):
        """One post-only GTD BUY, or None (refused by our limits or by the exchange: nothing rests then)."""
        lc = self.lc
        if self._stopping or self.fatal:
            return None
        ok, why = self.health()
        if not ok:
            return self.refuse(why, token)
        now = self.mono()
        if now < self.pause.get(token, 0.0):
            return self.refuse("paused", token)
        if side not in ("U", "D") or not token:
            return self.refuse("bad_side", token, side=side)
        if not self.PRICE_LO <= price <= self.PRICE_HI:
            return self.refuse("price", token, price=price)
        if size < 5 - 1e-9 or size > lc.max_shares + 1e-9 or abs(size - round(size)) > 1e-9:
            return self.refuse("size", token, size=size)
        if any(o.token == token for o in self.orders.values()) or len(self.orders) >= 2:
            return self.refuse("one_per_token", token, n=len(self.orders))
        if self.open_usd() + price * size > lc.max_resting_usd + 1e-9:
            return self.refuse("max_resting_usd", token, open=round(self.open_usd(), 2), add=round(price * size, 2))
        while self.placed_t and now - self.placed_t[0] > 3600:
            self.placed_t.popleft()
        if len(self.placed_t) >= lc.max_orders_hour:
            self.fatal = self.fatal or f"max_orders_hour {lc.max_orders_hour} reached"
            self.log("mm_live_error", where="max_orders_hour", error=self.fatal)
            return None
        wall = self.clock()
        end = float(end_s) if end_s else wall
        exp = int(max(wall + lc.ttl_s, end + 60))
        self.placed_t.append(now)
        self.tokens[token] = (key, side, end)
        t0 = time.perf_counter()
        try:
            resp = await self.client.place_limit_order(asset_id=token, side="BUY", price=f"{price:.2f}",
                                                       size=f"{size:.2f}", post_only=True, expiration=exp)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status", None) == 400:         # refused, nothing was created
                msg = str(e)
                cross = "crosses book" in msg
                self.stats["crossed" if cross else "rejected"] += 1
                self.log("order_rejected", token=token, key=key, side=side, price=price, size=size,
                         code="post_only_would_cross" if cross else "http_400", message=msg[:200])
                self.pause[token] = now + (lc.cross_pause_ms / 1000.0 if cross else lc.reject_pause_s)
                return None
            self.note_error("place", e)
            if type(e).__name__ == "RequestRejectedError" or getattr(e, "restriction", None):
                # the exchange answered: restarting / cancel-only / post-only mode (docs: "Matching Engine Restarts"). Nothing was
                # created; note_error backed off for retry_after and the maker pulls and waits (health: api_backoff)
                self.stats["rejected"] += 1
                self.log("order_rejected", token=token, key=key, side=side, price=price, size=size, code="restricted",
                         message=str(e)[:200])
                return None
            self.suspect.add(token)                        # no answer: the order may exist. Sweep before the next one
            self.log("mm_live_error", where="place", error=str(e)[:200], token=token, key=key)
            return None
        lat = round((time.perf_counter() - t0) * 1000, 1)
        self._note_lat("place", lat)
        if not getattr(resp, "ok", False):
            code = getattr(resp, "code", "?")
            self.stats["rejected"] += 1
            self.log("order_rejected", token=token, key=key, side=side, price=price, size=size, latency_ms=lat,
                     code=code, message=str(getattr(resp, "message", ""))[:200])
            self.pause[token] = now + (lc.cross_pause_ms / 1000.0 if "cross" in str(code) else lc.reject_pause_s)
            return None
        oid = resp.order_id
        o = self.known.get(oid)
        if o is None:
            o = LiveOrder(token, key, side, price, size, wall, exp)
            self.known[oid] = o
        else:
            # the order was filled (at least partly) and the stream event came BEFORE this answer: it was adopted by our address
            # with remaining = size - filled. Keep what it counted: a fresh order here would count the same shares again at the
            # next reconcile (a phantom position of up to 5 shares in the window's books)
            filled = max(0.0, o.size - o.remaining)
            o.price, o.size, o.exp, o.t = price, size, exp, wall
            o.remaining = max(0.0, size - filled)
            self.stats["fill_before_answer"] = self.stats.get("fill_before_answer", 0) + 1
        if o.remaining > 1e-9:
            self.orders[oid] = o
        self.stats["placed"] += 1
        self.log("order_placed", order_id=oid, token=token, key=key, side=side, price=price, size=size,
                 expiration=exp, latency_ms=lat, status=getattr(resp, "status", None))
        return oid

    async def cancel(self, oids, now_ms):
        """Cancel our orders by id. Returns the ids the exchange could not cancel (matched or gone already): their fills
        are counted from the exchange's record at once, so the caller does not bid again on a stale position."""
        oids = [o for o in oids if o in self.orders]
        if not oids:
            return set()
        t0 = time.perf_counter()
        try:
            resp = await self.client.cancel_orders(order_ids=oids)
        except Exception as e:  # noqa: BLE001
            self.note_error("cancel", e)
            raise                                          # still believed resting: the maker tries again
        lat = round((time.perf_counter() - t0) * 1000, 1)
        self._note_lat("cancel", lat)
        done = list(getattr(resp, "canceled", ()) or ())
        for oid in done:
            self.orders.pop(oid, None)
        nc = getattr(resp, "not_canceled", {}) or {}
        self.stats["cancels"] += len(done)
        self.log("order_cancelled", order_ids=done, latency_ms=lat,
                 not_canceled={k: str(v)[:80] for k, v in nc.items()})
        late = set()
        for oid in oids:
            if oid in done:
                continue
            late.add(oid)                                  # matched or gone: ask the exchange what it filled
            self.stats["cancel_late"] += 1
            self.orders.pop(oid, None)
            await self.account(oid)
        return late

    async def account(self, oid):
        """Compare one order of this run with the exchange's own record and count what matched without a stream event."""
        o = self.known.get(oid)
        if o is None:
            return 0.0
        try:
            x = await self.client.get_order(order_id=oid)
        except Exception as e:  # noqa: BLE001
            self.log("mm_live_error", where="get_order", error=str(e)[:200])
            return 0.0
        matched = float(getattr(x, "size_matched", 0) or 0)
        q = matched - (o.size - o.remaining)
        status = str(getattr(x, "status", "") or "").upper()
        if oid in self.orders and any(w in status for w in ("CANCEL", "EXPIRE", "INVALID", "MATCHED")) and q <= 1e-9:
            self.orders.pop(oid, None)                     # the exchange removed it (heartbeat lapse, expiry)
            self.log("order_gone", order_id=oid, status=status)
        if q <= 1e-9:
            return 0.0
        self._fill(oid, o, q, o.price, trade_id=None, status="RECONCILED")
        self.stats["reconciled"] += 1
        return q

    async def reconcile(self):
        """account() for every order we believe resting. Returns the shares found filled."""
        tot = 0.0
        for oid in list(self.orders):
            tot += await self.account(oid)
        return tot

    def gc(self, now_ms):
        """Forget orders whose own expiry passed (the exchange removed them) and tokens of long-gone windows."""
        now = self.clock()
        for oid in [k for k, o in self.orders.items() if now >= o.exp - 60]:
            self.orders.pop(oid, None)
        old = {t for t, (_, _, end) in self.tokens.items() if end + 3600 < now}
        if old:                                            # an hour after a window: no late fill can come any more
            for tok in old:
                self.tokens.pop(tok, None)
            gone = {k for k, o in self.known.items() if o.token in old and k not in self.orders}
            for k in gone:
                self.known.pop(k, None)
            for uid in [u for u in self._counted if u[1] in gone]:
                self._counted.pop(uid, None)

    def on_public_trade(self, *a, **kw):
        pass                                               # live fills come from the authenticated user stream

    # ------------------------------------------------------------ fills
    async def _user_stream(self):
        from polymarket.streams import UserSpec
        while not self._stopping:
            try:
                async with await self.client.subscribe([UserSpec()]) as stream:
                    self.stream_ok = True
                    self._reconcile_now = True                     # fills of the gap are in the exchange's record, not in the stream
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

    def _fill(self, oid, o, q, px, trade_id, status, match_ts=None, adopted=False):
        o.remaining -= q
        if o.remaining <= 1e-9:
            self.orders.pop(oid, None)
        fill = {"order_id": oid, "token": o.token, "key": o.key, "side": o.side, "price": px, "size": q,
                "ts": int(self.clock() * 1000), "trade_id": trade_id, "status": status, "match_ts": match_ts}
        self.stats["fills"] += 1
        self.log("mm_exec", adopted=adopted, **fill)       # the maker journals "fill" + "mm_fill" itself (one record per fill)
        if self.on_fill:
            self.on_fill(fill)

    def handle_trade(self, pl):
        """A trade of the account: count the part matched against our maker orders (by id; an unknown order of ours on a
        token we quote is adopted). A trade that FAILED after we counted it is taken back."""
        status = str(getattr(pl, "status", "") or "").upper()
        m_at = getattr(pl, "matched_at", None)
        match_ts = int(m_at.timestamp() * 1000) if m_at else None
        for mo in getattr(pl, "maker_orders", None) or ():
            oid = mo.order_id
            uid = (pl.id, oid)
            o = self.known.get(oid)
            if o is None:
                mine = self.wallet and str(getattr(mo, "maker_address", "") or "").lower() == self.wallet
                tok = str(getattr(mo, "asset_id", "") or "")
                if not mine or tok not in self.tokens or status == "FAILED":
                    continue                               # another maker's order (we were the taker), or not a quoted token
                key, side, _ = self.tokens[tok]
                size = max(float(mo.matched_amount), self.lc.max_shares)       # its later fills count too (up to one order)
                o = LiveOrder(tok, key, side, float(mo.price), size, self.clock(), 0)
                self.known[oid] = o
                self.stats["adopted"] += 1
            if status == "FAILED":
                q = self._counted.pop(uid, 0.0)
                if q > 1e-9:
                    self.stats["failed"] += 1
                    o.remaining += q
                    self.suspect.add(o.token)              # the exchange may have put the order back: sweep that token
                    rec = {"order_id": oid, "key": o.key, "side": o.side, "price": float(mo.price), "size": q,
                           "trade_id": pl.id}
                    self.log("fill_failed", **rec)
                    if self.on_unfill:
                        self.on_unfill(rec)
                continue
            if uid in self._counted:
                continue
            q = min(float(mo.matched_amount), o.remaining)
            self._counted[uid] = max(q, 0.0)
            if q <= 1e-9:
                continue                                   # already counted from the exchange's record (account())
            self._fill(oid, o, q, float(mo.price), trade_id=pl.id, status=status, match_ts=match_ts,
                       adopted=oid not in self.orders and o.exp == 0)
