"""LIVE broker (real money) on the official `polymarket-client` SDK.  Disabled unless every gate passes:

  1. execution.mode = "live" in the config AND the --live CLI flag
  2. env PMBOT_LIVE_CONFIRM == "I_UNDERSTAND_REAL_MONEY_RISK"
  3. env PMBOT_PRIVATE_KEY present (use a dedicated wallet / session key with a small balance)
  4. https://polymarket.com/api/geoblock says blocked == false for this machine, or the country is one where only
     the website is restricted and the API is allowed (API_OK_COUNTRIES, see docs.polymarket.com geoblock page)
  5. collateral balance >= risk.max_cost_per_window; the engine caps risk.max_open_worst_loss at the balance

Safety mechanisms while running:
  * every order is post-only (never pays taker fees, never crosses) and GTD (expires by itself)
  * order heartbeat every few seconds: if the bot dies, the exchange cancels all orders after ~10s
  * cancel_all on start and on shutdown (also on kill switch)
  * health(): no quoting while the user (fill) stream is down, heartbeats fail or the API asked us to back off;
    a 401/403 from the API (auth / region) is fatal -> kill switch
  * fills are attributed by every order id we ever placed (not only resting ones): a fill that arrives after
    the cancel answered "not canceled" is still counted in the position
The private key is read from the environment only and never logged.
"""
import asyncio
import os
import time

import requests

CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY_RISK"
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
# "close-only on the frontend only, API not restricted" (docs.polymarket.com/developers/CLOB/geoblock, Nov 2026).
# Only the country this bot actually runs from is listed; add others only after checking that page again.
API_OK_COUNTRIES = {"IE"}


class LiveGateError(RuntimeError):
    pass


def check_geoblock(get=requests.get):
    r = get(GEOBLOCK_URL, timeout=15)
    d = r.json()
    if d.get("blocked", True) and d.get("country") not in API_OK_COUNTRIES:
        raise LiveGateError(f"Polymarket geoblock: trading is blocked from this location "
                            f"(country={d.get('country')}, region={d.get('region')}). Live mode refused.")
    return d


def check_env(env=os.environ):
    if env.get("PMBOT_LIVE_CONFIRM") != CONFIRM_PHRASE:
        raise LiveGateError(f"set PMBOT_LIVE_CONFIRM={CONFIRM_PHRASE} to enable real-money trading")
    if not env.get("PMBOT_PRIVATE_KEY"):
        raise LiveGateError("PMBOT_PRIVATE_KEY is not set")


class LiveBroker:
    mode = "live"

    def __init__(self, cfg, on_fill=None, journal=None, client=None, geoblock_get=requests.get, env=os.environ):
        self.cfg = cfg
        self.on_fill = on_fill
        self.journal = journal
        self.client = client          # injected in tests; created in start() otherwise
        self._geoblock_get = geoblock_get
        self._env = env
        self.orders = {}              # order_id -> dict(token,key,side,price,size,remaining): believed resting
        self.known = {}               # every order id placed by this process (fills are attributed through it)
        self.balance_usd = None
        self.fatal = ""               # set on 401/403: the engine kills itself
        self.backoff_until = 0.0      # monotonic seconds: API asked us to slow down / is restarting
        self.stream_ok = False
        self.hb_ok_at = 0.0           # monotonic time of the last successful heartbeat
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
            from polymarket import AsyncSecureClient  # imported lazily: paper mode does not need the SDK
            self.client = await AsyncSecureClient.create(
                private_key=self._env["PMBOT_PRIVATE_KEY"],
                wallet=self._env.get("PMBOT_WALLET") or None,
            )
        bal = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        usd = bal.balance / 1e6
        self.balance_usd = usd
        self.log("balance", pusd=usd)
        need = self.cfg.risk.max_cost_per_window
        if usd < need:
            raise LiveGateError(f"collateral balance {usd:.2f} < risk.max_cost_per_window {need:.2f}")
        await self.cancel_all()
        await self._heartbeat_once()
        self._tasks = [asyncio.create_task(self._heartbeat_loop()), asyncio.create_task(self._user_stream())]
        return True

    async def stop(self):
        self._stopping = True
        try:
            await self.cancel_all()
        finally:
            for t in self._tasks:
                t.cancel()
            # heartbeats stop now; the exchange-side deadline would also cancel anything left
            close = getattr(self.client, "close", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

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
            await asyncio.sleep(self.cfg.execution.heartbeat_s)
            try:
                await self._heartbeat_once()
            except Exception as e:  # noqa: BLE001
                self.note_error("heartbeat", e)
                self.log("heartbeat_error", error=str(e)[:200])

    # ------------------------------------------------------------ health
    def health(self):
        """(ok, reason). Not ok = do not quote (the engine also cancels what rests)."""
        if self.fatal:
            return False, "broker_fatal"
        now = time.monotonic()
        if now < self.backoff_until:
            return False, "api_backoff"
        if not self.stream_ok:
            return False, "user_stream_down"
        if self.hb_ok_at and now - self.hb_ok_at > 2 * self.cfg.execution.heartbeat_s + 1:
            return False, "heartbeat_down"      # the exchange cancels everything ~10 s after the last heartbeat
        return True, ""

    def note_error(self, op, e):
        """Classify an API exception: fatal (auth / region), back off for a while, or transient."""
        status = getattr(e, "status", None)
        name = type(e).__name__
        if status in (401, 403):
            self.fatal = f"{op}: HTTP {status} {str(e)[:150]}"
            self.log("broker_fatal", op=op, status=status, error=str(e)[:300])
            return
        if name == "RateLimitError":
            wait = getattr(e, "retry_after", None) or 5.0
        elif getattr(e, "restriction", None):            # exchange restarting / cancel-only / post-only mode
            wait = getattr(e, "retry_after", None) or 10.0
        else:
            wait = 2.0
        self.backoff_until = max(self.backoff_until, time.monotonic() + float(wait))
        self.log("api_error", op=op, err=name, status=status, restriction=getattr(e, "restriction", None),
                 error=str(e)[:300], backoff_s=float(wait))

    # ------------------------------------------------------------ orders
    async def place(self, token, key, side, price, size, now_ms, expiration_s=None, queue_ahead=None):
        if self._stopping:
            return None
        t0 = time.perf_counter()
        try:
            resp = await self.client.place_limit_order(
                asset_id=token, side="BUY", price=f"{price:.2f}", size=f"{size:.2f}",
                post_only=True, expiration=int(expiration_s) if expiration_s else None)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status", None) == 400:
                # request refused, nothing was created (e.g. "invalid post-only order: order crosses book" when
                # the book moved during our latency): a normal rejection, no reset and no backoff
                msg = str(e)
                code = "post_only_would_cross" if "crosses book" in msg else "http_400"
                self.log("order_rejected", token=token, key=key, side=side, price=price, size=size, code=code,
                         message=msg[:200], latency_ms=round((time.perf_counter() - t0) * 1000, 1))
                return None
            self.note_error("place", e)
            raise                     # the engine resets: the order may or may not exist now
        lat = round((time.perf_counter() - t0) * 1000, 1)
        if not getattr(resp, "ok", False):
            code = getattr(resp, "code", "?")
            self.log("order_rejected", token=token, key=key, side=side, price=price, size=size, latency_ms=lat,
                     code=code, message=getattr(resp, "message", "")[:200])
            if code == "not_enough_balance":
                self.backoff_until = max(self.backoff_until, time.monotonic() + 30)
            return None
        o = {"token": token, "key": key, "side": side, "price": price, "size": size, "remaining": size}
        self.orders[resp.order_id] = o
        self.known[resp.order_id] = o
        self.log("order_placed", order_id=resp.order_id, token=token, key=key, side=side, price=price, size=size,
                 expiration=expiration_s, latency_ms=lat, status=getattr(resp, "status", None))
        return resp.order_id

    async def cancel(self, oids, now_ms):
        oids = [o for o in oids if o in self.orders]
        if not oids:
            return
        t0 = time.perf_counter()
        try:
            resp = await self.client.cancel_orders(order_ids=oids)
        except Exception as e:  # noqa: BLE001
            self.note_error("cancel", e)
            raise
        lat = round((time.perf_counter() - t0) * 1000, 1)
        for oid in getattr(resp, "canceled", ()):
            self.orders.pop(oid, None)
        self.log("order_cancelled", order_ids=list(getattr(resp, "canceled", ())), latency_ms=lat)
        nc = getattr(resp, "not_canceled", {}) or {}
        gone = set()
        if nc:
            late = {k: v for k, v in nc.items() if "matched" in str(v) or "already canceled" in str(v)}
            other = {k: v for k, v in nc.items() if k not in late}
            if late:   # normal: it was (probably) filled a moment ago, the fill event follows 0.2-1 s later
                self.log("cancel_late", order_ids=list(late))
                gone = set(late)
            if other:
                self.log("cancel_failed", details={k: str(v)[:100] for k, v in other.items()})
            for oid in nc:            # a late fill is still counted (self.known)
                self.orders.pop(oid, None)
        return gone

    async def cancel_all(self, now_ms=None):
        try:
            resp = await self.client.cancel_all()
        except Exception as e:  # noqa: BLE001
            self.note_error("cancel_all", e)
            raise
        self.orders.clear()
        self.log("cancel_all", canceled=len(getattr(resp, "canceled", ()) or ()))

    def gc(self, now_ms):
        pass

    # ------------------------------------------------------------ restart safety
    def existing_positions(self, assets, get_json=None, now=None):
        """OPEN 5m Up/Down positions already held by the wallet (e.g. after a restart), only in windows that have
        not ended yet. Tokens of ended windows are either already paid out or worthless leftovers that stay in the
        wallet forever ("REDEEMABLE" with value 0): counting them again settled phantom losses (8 Nov: -10.81 on a
        fresh state file -> kill switch before the first order).

        Returns [(asset, window_start, side, shares, avg_price, slug)] so the risk manager can count them.
        """
        from ..config import DATA
        from ..http import get_json as _get
        get_json = get_json or _get
        wallet = getattr(self.client, "wallet", None) or self._env.get("PMBOT_WALLET")
        if not wallet:
            raise LiveGateError("cannot determine wallet address to load existing positions")
        out, cursor = [], None
        while True:
            params = {"user": str(wallet), "status": "OPEN", "title": "Up or Down", "limit": 500}
            if cursor:
                params["cursor"] = cursor
            d = get_json(f"{DATA}/positions", params)
            now_s = now if now is not None else time.time()
            for r in d.get("data", []):
                slug = r.get("slug") or ""
                parts = slug.split("-")
                if len(parts) != 4 or parts[1] != "updown" or parts[2] != "5m" or parts[0] not in assets:
                    continue
                if not parts[3].isdigit() or int(parts[3]) + 300 <= now_s or r.get("redeemable"):
                    continue          # window over: resolved, nothing left to risk
                side = "U" if r.get("outcome") == "Up" else "D"
                out.append((parts[0], int(parts[3]), side, float(r.get("current_size") or 0),
                            float(r.get("avg_price") or 0), slug))
            pg = d.get("pagination") or {}
            if not pg.get("has_more") or not pg.get("next_cursor"):
                return out
            cursor = pg["next_cursor"]

    def on_public_trade(self, *a, **kw):
        pass  # live fills come from the authenticated user stream

    # ------------------------------------------------------------ fills
    async def _user_stream(self):
        from polymarket.streams import UserSpec
        from ..fees import fee_per_share
        while not self._stopping:
            try:
                async with await self.client.subscribe([UserSpec()]) as stream:
                    self.stream_ok = True
                    self.log("user_stream_up")
                    async for ev in stream:
                        if getattr(ev, "type", None) != "trade":
                            continue
                        pl = ev.payload
                        status = str(getattr(pl, "status", "") or "")
                        for mo in pl.maker_orders or ():
                            o = self.known.get(mo.order_id)
                            uid = (pl.id, mo.order_id)
                            if o is None or uid in self._seen_fills:
                                continue
                            if status.upper() == "FAILED":
                                self.log("fill_failed", trade_id=pl.id, order_id=mo.order_id)
                                continue
                            self._seen_fills.add(uid)
                            q = float(mo.matched_amount)
                            px = float(mo.price)
                            o["remaining"] -= q
                            if o["remaining"] <= 1e-9:
                                self.orders.pop(mo.order_id, None)
                            m_at = getattr(pl, "matched_at", None)
                            fill = {"order_id": mo.order_id, "token": o["token"], "key": o["key"], "side": o["side"],
                                    "price": px, "size": q, "fee_eq": fee_per_share(px) * q,
                                    "ts": int(time.time() * 1000), "trade_id": pl.id, "status": status,
                                    "match_ts": int(m_at.timestamp() * 1000) if m_at else None}
                            self.log("fill", **fill)
                            if self.on_fill:
                                self.on_fill(fill)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("user_stream_error", error=str(e)[:200])
            self.stream_ok = False
            if not self._stopping:
                await asyncio.sleep(2)


def price_str(p):
    """Price for the client: no trailing zero digits. The client rejects '0.980' on a 0.01-tick market ("max_price must
    conform to tick size 0.01 with at most 2 decimal places", dry run 8 Nov 22:55 UTC); '0.98' / '0.975' are fine."""
    s = f"{p:.3f}"
    return s[:-1] if s.endswith("0") else s


# The CLOB answers HTTP 400 with one of these texts when a FAK / FOK order matched nothing: the ask was pulled or taken
# during our latency plus the taker delay (150 ms on crypto markets). Nothing was created, so a new order is safe.
NO_MATCH_TEXTS = ("no orders found to match", "couldn't be fully filled")
NO_MATCH_CODES = frozenset({"fak_not_filled", "fok_not_filled"})


class TakerLiveBroker:
    """Minimal live taker (FAK) broker for the lock, hourly and arb bots; same start gates as LiveBroker."""
    mode = "live"

    def __init__(self, max_exposure_usd, journal=None, client=None, geoblock_get=requests.get, env=os.environ):
        self.max_exposure = max_exposure_usd
        self.journal = journal
        self.client = client
        self._geoblock_get = geoblock_get
        self._env = env
        self.last_reject = ""         # why the last place_fak returned None ("" after a fill)

    def log(self, kind, **kw):
        if self.journal:
            self.journal.log(kind, mode="live", **kw)

    async def start(self):
        check_env(self._env)
        geo = check_geoblock(self._geoblock_get)
        self.log("geoblock_ok", country=geo.get("country"))
        if self.client is None:
            from polymarket import AsyncSecureClient
            self.client = await AsyncSecureClient.create(private_key=self._env["PMBOT_PRIVATE_KEY"],
                                                         wallet=self._env.get("PMBOT_WALLET") or None)
        bal = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        if bal.balance / 1e6 < self.max_exposure:
            raise LiveGateError(f"collateral {bal.balance / 1e6:.2f} < max exposure {self.max_exposure:.2f}")
        return True

    async def prewarm(self, token):
        """Let the SDK resolve and cache the market metadata of `token` before the first order needs it.

        place_market_order() looks up tick size and fees first: GET /markets-by-token/<token>, then
        GET /clob-markets/<condition>, one after the other, cached for 10 minutes for both tokens of the market. Every
        5m window has new tokens, so without this each first order of a window waits for two extra round trips while
        the ask it aims at can be pulled. create_market_order() does the same lookup and signs an order that is never
        posted (and never logged). Returns the time it took in ms, None on an error (the order path then looks it up)."""
        t0 = time.perf_counter()
        try:
            await self.client.create_market_order(asset_id=token, side="BUY", amount="4.55", max_spend="4.55",
                                                  max_price="0.91", order_type="FAK")
        except Exception as e:  # noqa: BLE001
            self.log("prewarm_error", err=type(e).__name__, error=str(e)[:200])
            return None
        return round((time.perf_counter() - t0) * 1000, 1)

    async def place_fak(self, token, key, side, limit, usd, max_spend=None):
        """One FAK market BUY of `usd` capped at `limit`. Returns {"price", "size"} for a fill, else None with the
        reason in self.last_reject. "fak_not_filled" = the exchange matched nothing and created nothing (a new order
        is safe); any other reason means the order was refused or is still pending: do not repeat it blindly.
        Raises when the outcome is unknown (timeout, 5xx, an unexpected 400).

        `max_spend` (default `usd`) is the all-in budget the SDK plans for: when it does not cover `usd` plus the taker
        fee, the SDK lowers the notional and the order asks for a fractional share count (see LiveTaker.buy)."""
        self.last_reject = ""
        t0 = time.perf_counter()
        try:
            resp = await self.client.place_market_order(asset_id=token, side="BUY", amount=f"{usd:.2f}",
                                                        max_spend=f"{usd if max_spend is None else max_spend:.2f}",
                                                        max_price=price_str(limit), order_type="FAK")
        except Exception as e:  # noqa: BLE001
            lat = round((time.perf_counter() - t0) * 1000, 1)
            msg = str(e)
            if getattr(e, "status", None) == 400 and any(t in msg for t in NO_MATCH_TEXTS):
                self.last_reject = "fak_not_filled"
                self.log("order_rejected", key=key, side=side, limit=limit, code="fak_not_filled", message=msg[:200],
                         latency_ms=lat)
                return None
            self.log("order_failed", key=key, side=side, limit=limit, err=type(e).__name__,
                     status=getattr(e, "status", None), error=msg[:300], latency_ms=lat)
            raise
        lat = round((time.perf_counter() - t0) * 1000, 1)
        if not getattr(resp, "ok", False):
            self.last_reject = str(getattr(resp, "code", "") or "rejected")
            self.log("order_rejected", key=key, side=side, limit=limit, code=self.last_reject,
                     message=getattr(resp, "message", "")[:200], latency_ms=lat)
            return None
        spent, shares = float(resp.making_amount), float(resp.taking_amount)
        status = getattr(resp, "status", None)
        if shares <= 0:
            # accepted but not matched yet ("delayed": the market holds takers back). The order is still alive on the
            # exchange and may fill after this answer: it is NOT a miss
            self.last_reject = f"pending:{status or 'no_fill'}"
            self.log("order_pending", key=key, side=side, order_id=resp.order_id, status=status, latency_ms=lat)
            return None
        self.log("order_filled", key=key, side=side, order_id=resp.order_id, spent=spent, shares=shares, status=status,
                 latency_ms=lat)
        return {"price": spent / shares, "size": shares}
