"""Async market-data feeds that keep State current: CLOB books (one socket per asset), Chainlink (RTDS), Binance."""
import asyncio
import json
import logging

import websockets

from ..config import BINANCE_SYMBOL, CHAINLINK_SYMBOL, WS_BINANCE, WS_CLOB_MARKET, WS_RTDS
from .state import State, now_ms

log = logging.getLogger("pmbot.feeds")


class ClobFeed:
    """Order books for the tokens of one asset's current/next windows. Calls on_trade for public trades."""

    def __init__(self, st: State, asset: str, on_trade=None, alive=lambda: True):
        self.st, self.asset, self.on_trade, self.alive = st, asset, on_trade, alive
        self.ws = None
        self.subscribed = set()

    def wanted(self):
        out = set()
        for w in self.st.windows.values():
            if w.asset == self.asset:
                out |= {w.token_up, w.token_down}
        return out

    async def sync(self):
        if self.ws is None:
            return
        want = self.wanted()
        add, rem = sorted(want - self.subscribed), sorted(self.subscribed - want)
        try:
            if add:
                await self.ws.send(json.dumps({"assets_ids": add, "operation": "subscribe"}))
            if rem:
                await self.ws.send(json.dumps({"assets_ids": rem, "operation": "unsubscribe"}))
            self.subscribed = (self.subscribed | set(add)) - set(rem)
        except Exception as e:  # noqa: BLE001
            log.warning("clob %s sync error %s", self.asset, e)

    def _invalidate(self):
        for t in self.subscribed:
            b = self.st.books.get(t)
            if b:
                b.clear()
        self.st.feed_alive["clob"][self.asset] = 0

    def handle(self, ev, rx):
        t = ev.get("event_type")
        if t == "book":
            b = self.st.books.get(ev["asset_id"])
            if b is not None:
                b.bids = {float(x["price"]): float(x["size"]) for x in ev["bids"] if float(x["size"]) > 0}
                b.asks = {float(x["price"]): float(x["size"]) for x in ev["asks"] if float(x["size"]) > 0}
                b.last_rx = rx
        elif t == "price_change":
            for c in ev["price_changes"]:
                b = self.st.books.get(c["asset_id"])
                if b is None:
                    continue
                side = b.bids if c["side"] == "BUY" else b.asks
                p, s = float(c["price"]), float(c["size"])
                if s <= 0:
                    side.pop(p, None)
                else:
                    side[p] = s
                b.last_rx = rx
        elif t == "last_trade_price" and self.on_trade:
            self.on_trade(ev["asset_id"], ev.get("side"), float(ev["price"]), float(ev["size"]),
                          int(ev.get("timestamp") or rx), rx)
        elif t == "tick_size_change":
            w = self.st.token_window.get(ev.get("asset_id"))
            if w:
                w.tick = min(w.tick, float(ev["new_tick_size"]))

    async def run(self):
        while self.alive():
            try:
                async with websockets.connect(WS_CLOB_MARKET, open_timeout=15, max_size=2**24,
                                              ping_interval=None, max_queue=None) as ws:
                    self.ws = ws
                    want = sorted(self.wanted())
                    await ws.send(json.dumps({"assets_ids": want, "type": "market"}))
                    self.subscribed = set(want)
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        while self.alive():
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            rx = now_ms()
                            self.st.feed_alive["clob"][self.asset] = rx
                            if msg == "PONG":
                                continue
                            data = json.loads(msg)
                            for ev in data if isinstance(data, list) else [data]:
                                self.handle(ev, rx)
                    finally:
                        pinger.cancel()
                        self.ws = None
                        self._invalidate()
            except Exception as e:  # noqa: BLE001
                log.warning("clob %s ws error %s %s", self.asset, type(e).__name__, str(e)[:120])
                self._invalidate()
                await asyncio.sleep(1)

    @staticmethod
    async def _ping(ws):
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")


async def rtds_feed(st: State, alive=lambda: True):
    syms = {CHAINLINK_SYMBOL[a]: a for a in st.assets}
    while alive():
        try:
            async with websockets.connect(WS_RTDS, open_timeout=15, ping_interval=20) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                    {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
                while alive():
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    rx = now_ms()
                    if not msg:
                        continue
                    pl = json.loads(msg).get("payload") or {}
                    a = syms.get(pl.get("symbol"))
                    if a:
                        st.feed_alive["rtds"] = rx
                        st.prices[a].on_chainlink(float(pl["value"]), int(pl["timestamp"]), rx)
        except Exception as e:  # noqa: BLE001
            log.warning("rtds error %s %s", type(e).__name__, str(e)[:120])
            await asyncio.sleep(1)


async def binance_feed(st: State, alive=lambda: True):
    streams = "/".join(f"{BINANCE_SYMBOL[a].lower()}@bookTicker" for a in st.assets)
    rev = {BINANCE_SYMBOL[a]: a for a in st.assets}
    while alive():
        try:
            async with websockets.connect(f"{WS_BINANCE}?streams={streams}", open_timeout=15) as ws:
                while alive():
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    rx = now_ms()
                    d = json.loads(msg)["data"]
                    a = rev.get(d["s"])
                    if a:
                        st.feed_alive["binance"] = rx
                        st.prices[a].on_binance((float(d["b"]) + float(d["a"])) / 2, rx)
                        if st.on_tick is not None:
                            st.on_tick(a)
        except Exception as e:  # noqa: BLE001
            log.warning("binance error %s %s", type(e).__name__, str(e)[:120])
            await asyncio.sleep(1)
