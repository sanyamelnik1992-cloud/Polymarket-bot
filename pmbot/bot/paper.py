"""Paper broker: simulates resting post-only bids against the live public trade stream.

Fill rule (conservative "trade-through", identical to pmbot.maker_sim):
  our BUY at b on token X is hit by
    - a taker SELL of X executed at a price  < b        (we would have been the better bid)
    - a taker BUY of the complement at q with 1-q < b   (mint match against our bid)
  Equal price = we would be in the queue behind earlier orders -> no fill.
Latency: an order becomes live `place_latency_ms` after the decision; a cancel takes effect
`cancel_latency_ms` after the decision. Trades are compared on the exchange match timestamp.
"""
import itertools
from dataclasses import dataclass

from ..fees import fee_per_share


@dataclass
class PaperOrder:
    id: str
    token: str
    key: str            # window key
    side: str           # "U" / "D" (which outcome we bid for)
    price: float
    size: float
    remaining: float
    live_from: int
    cancel_at: int | None = None
    expires_at: int | None = None

    def live_at(self, ts):
        if ts < self.live_from:
            return False
        if self.cancel_at is not None and ts >= self.cancel_at:
            return False
        if self.expires_at is not None and ts >= self.expires_at:
            return False
        return self.remaining > 1e-9


class PaperBroker:
    mode = "paper"

    def __init__(self, place_latency_ms=150, cancel_latency_ms=150, on_fill=None, journal=None):
        self.place_latency = place_latency_ms
        self.cancel_latency = cancel_latency_ms
        self.orders = {}
        self._ids = itertools.count(1)
        self.on_fill = on_fill
        self.journal = journal

    async def start(self):
        return True

    async def stop(self):
        await self.cancel_all()

    async def place(self, token, key, side, price, size, now_ms, expiration_s=None):
        oid = f"p{next(self._ids)}"
        exp = (expiration_s - 60) * 1000 if expiration_s else None   # GTD: effective expiry is 60s earlier
        self.orders[oid] = PaperOrder(oid, token, key, side, price, size, size, now_ms + self.place_latency,
                                      expires_at=exp)
        return oid

    async def cancel(self, oids, now_ms):
        for oid in oids:
            o = self.orders.get(oid)
            if o and o.cancel_at is None:
                o.cancel_at = now_ms + self.cancel_latency

    async def cancel_all(self, now_ms=None):
        import time
        now_ms = now_ms or int(time.time() * 1000)
        await self.cancel(list(self.orders), now_ms)

    def gc(self, now_ms):
        """Forget orders that can no longer fill (cancelled long ago / filled)."""
        for oid in [k for k, o in self.orders.items()
                    if o.remaining <= 1e-9 or (o.cancel_at is not None and o.cancel_at < now_ms - 10_000)]:
            self.orders.pop(oid, None)

    def on_public_trade(self, token, complement_of, side, price, size, ts_ms):
        """Feed one public trade (taker side). `complement_of(token)` returns the other outcome token."""
        other = complement_of(token)
        # candidate orders: bids on `token` hit by a taker SELL, bids on the complement hit by a taker BUY
        cands = []
        for o in self.orders.values():
            if not o.live_at(ts_ms):
                continue
            if o.token == token and side == "SELL" and price < o.price - 1e-9:
                cands.append(o)
            elif o.token == other and side == "BUY" and (1 - price) < o.price - 1e-9:
                cands.append(o)
        left = size
        for o in sorted(cands, key=lambda x: -x.price):   # best bid first
            if left <= 1e-9:
                break
            q = min(o.remaining, left)
            o.remaining -= q
            left -= q
            fill = {"order_id": o.id, "token": o.token, "key": o.key, "side": o.side, "price": o.price,
                    "size": q, "fee_eq": fee_per_share(o.price) * q, "ts": ts_ms}
            if self.journal:
                self.journal.log("fill", mode="paper", **fill)
            if self.on_fill:
                self.on_fill(fill)
