"""LIVE broker (real money) on the official `polymarket-client` SDK.  Disabled unless every gate passes:

  1. execution.mode = "live" in the config AND the --live CLI flag
  2. env PMBOT_LIVE_CONFIRM == "I_UNDERSTAND_REAL_MONEY_RISK"
  3. env PMBOT_PRIVATE_KEY present (use a dedicated wallet / session key with a small balance)
  4. https://polymarket.com/api/geoblock says blocked == false for this machine
  5. collateral balance >= risk.max_open_worst_loss (the bot never needs more than that)

Safety mechanisms while running:
  * every order is post-only (never pays taker fees, never crosses) and GTD (expires by itself)
  * order heartbeat every few seconds: if the bot dies, the exchange cancels all orders after ~10s
  * cancel_all on start and on shutdown (also on kill switch)
The private key is read from the environment only and never logged.
"""
import asyncio
import os
import time

import requests

CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY_RISK"
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


class LiveGateError(RuntimeError):
    pass


def check_geoblock(get=requests.get):
    r = get(GEOBLOCK_URL, timeout=15)
    d = r.json()
    if d.get("blocked", True):
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
        self.orders = {}              # order_id -> dict(token,key,side,price,size,remaining)
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
        self.log("balance", pusd=usd)
        need = self.cfg.risk.max_open_worst_loss
        if usd < need:
            raise LiveGateError(f"collateral balance {usd:.2f} < risk.max_open_worst_loss {need:.2f}")
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

    async def _heartbeat_loop(self):
        while not self._stopping:
            await asyncio.sleep(self.cfg.execution.heartbeat_s)
            try:
                await self._heartbeat_once()
            except Exception as e:  # noqa: BLE001
                self.log("heartbeat_error", error=str(e)[:200])

    # ------------------------------------------------------------ orders
    async def place(self, token, key, side, price, size, now_ms, expiration_s=None):
        if self._stopping:
            return None
        resp = await self.client.place_limit_order(
            asset_id=token, side="BUY", price=f"{price:.2f}", size=f"{size:.2f}",
            post_only=True, expiration=int(expiration_s) if expiration_s else None)
        if not getattr(resp, "ok", False):
            self.log("order_rejected", token=token, key=key, side=side, price=price, size=size,
                     code=getattr(resp, "code", "?"), message=getattr(resp, "message", "")[:200])
            return None
        self.orders[resp.order_id] = {"token": token, "key": key, "side": side, "price": price,
                                      "size": size, "remaining": size}
        self.log("order_placed", order_id=resp.order_id, token=token, key=key, side=side, price=price, size=size,
                 expiration=expiration_s)
        return resp.order_id

    async def cancel(self, oids, now_ms):
        oids = [o for o in oids if o in self.orders]
        if not oids:
            return
        resp = await self.client.cancel_orders(order_ids=oids)
        for oid in getattr(resp, "canceled", ()):
            self.orders.pop(oid, None)
        nc = getattr(resp, "not_canceled", {}) or {}
        if nc:
            self.log("cancel_failed", details={k: str(v)[:100] for k, v in nc.items()})
            for oid in nc:            # usually already filled or expired
                self.orders.pop(oid, None)

    async def cancel_all(self, now_ms=None):
        resp = await self.client.cancel_all()
        self.orders.clear()
        self.log("cancel_all", canceled=len(getattr(resp, "canceled", ()) or ()))

    def gc(self, now_ms):
        pass

    def on_public_trade(self, *a, **kw):
        pass  # live fills come from the authenticated user stream

    # ------------------------------------------------------------ fills
    async def _user_stream(self):
        from polymarket.streams import UserSpec
        from ..fees import fee_per_share
        while not self._stopping:
            try:
                async with await self.client.subscribe([UserSpec()]) as stream:
                    async for ev in stream:
                        if getattr(ev, "type", None) != "trade":
                            continue
                        pl = ev.payload
                        for mo in pl.maker_orders or ():
                            o = self.orders.get(mo.order_id)
                            uid = (pl.id, mo.order_id)
                            if o is None or uid in self._seen_fills:
                                continue
                            self._seen_fills.add(uid)
                            q = float(mo.matched_amount)
                            px = float(mo.price)
                            o["remaining"] -= q
                            if o["remaining"] <= 1e-9:
                                self.orders.pop(mo.order_id, None)
                            fill = {"order_id": mo.order_id, "token": o["token"], "key": o["key"], "side": o["side"],
                                    "price": px, "size": q, "fee_eq": fee_per_share(px) * q,
                                    "ts": int(time.time() * 1000), "trade_id": pl.id}
                            self.log("fill", **fill)
                            if self.on_fill:
                                self.on_fill(fill)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("user_stream_error", error=str(e)[:200])
                await asyncio.sleep(2)
