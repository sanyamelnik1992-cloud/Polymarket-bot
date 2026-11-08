"""CLOB market websocket for an arbitrary, changing set of tokens: keeps order books, reports public trades."""
import asyncio
import json
import logging
import time

import websockets

from ..config import WS_CLOB_MARKET
from ..bot.state import Book

log = logging.getLogger("pmbot.rewards.feed")


class BookFeed:
    def __init__(self, on_trade=None, alive=lambda: True):
        self.on_trade = on_trade          # (token, side, price, size, ts_ms)
        self.alive = alive
        self.books = {}                   # token -> Book
        self.want = set()
        self.subscribed = set()
        self.ws = None
        self.last_msg = 0.0

    def set_tokens(self, tokens):
        self.want = set(tokens)
        for t in self.want:
            self.books.setdefault(t, Book())

    async def sync(self):
        if self.ws is None:
            return
        add, rem = sorted(self.want - self.subscribed), sorted(self.subscribed - self.want)
        try:
            if add:
                await self.ws.send(json.dumps({"assets_ids": add, "operation": "subscribe"}))
            if rem:
                await self.ws.send(json.dumps({"assets_ids": rem, "operation": "unsubscribe"}))
                for t in rem:
                    self.books.pop(t, None)
            self.subscribed = (self.subscribed | set(add)) - set(rem)
        except Exception as e:  # noqa: BLE001
            log.warning("sync error %s", e)

    def handle(self, ev, rx_ms):
        t = ev.get("event_type")
        if t == "book":
            b = self.books.get(ev.get("asset_id"))
            if b is not None:
                b.bids = {float(x["price"]): float(x["size"]) for x in ev.get("bids") or [] if float(x["size"]) > 0}
                b.asks = {float(x["price"]): float(x["size"]) for x in ev.get("asks") or [] if float(x["size"]) > 0}
                b.last_rx = rx_ms
        elif t == "price_change":
            for c in ev.get("price_changes") or []:
                b = self.books.get(c.get("asset_id"))
                if b is None:
                    continue
                side = b.bids if c["side"] == "BUY" else b.asks
                p, s = float(c["price"]), float(c["size"])
                if s <= 0:
                    side.pop(p, None)
                else:
                    side[p] = s
                b.last_rx = rx_ms
        elif t == "last_trade_price" and self.on_trade:
            try:
                self.on_trade(ev["asset_id"], ev.get("side"), float(ev["price"]), float(ev["size"]),
                              int(ev.get("timestamp") or rx_ms))
            except Exception as e:  # noqa: BLE001  (a bad event must not kill the feed)
                log.warning("trade handler error %s", e)

    def invalidate(self):
        for b in self.books.values():
            b.clear()

    async def run(self):
        while self.alive():
            try:
                async with websockets.connect(WS_CLOB_MARKET, open_timeout=15, max_size=2**24, ping_interval=None,
                                              max_queue=None) as ws:
                    self.ws = ws
                    want = sorted(self.want)
                    await ws.send(json.dumps({"assets_ids": want, "type": "market"}))
                    self.subscribed = set(want)
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        while self.alive():
                            await self.sync()
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            except asyncio.TimeoutError:
                                continue
                            rx = int(time.time() * 1000)
                            self.last_msg = time.time()
                            if msg == "PONG":
                                continue
                            data = json.loads(msg)
                            for ev in data if isinstance(data, list) else [data]:
                                self.handle(ev, rx)
                    finally:
                        pinger.cancel()
                        self.ws = None
                        self.invalidate()
            except Exception as e:  # noqa: BLE001
                log.warning("ws error %s %s", type(e).__name__, str(e)[:120])
                self.invalidate()
                await asyncio.sleep(2)

    @staticmethod
    async def _ping(ws):
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")
