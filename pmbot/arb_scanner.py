"""Live PAPER scanner for the nested 5m/15m monotonicity arbitrage (no orders are sent).

During the last 5 minutes of every 15m window [T, T+900] the 15m market and the 5m market [T+600, T+900]
settle on the same closing Chainlink TWAP C, with strikes
    O15 = finalPrice of the 5m window ending at T        (published long before)
    O5  = finalPrice of the 5m window ending at T+600    (published ~80s after T+600)
If O15 <= O5:  buy 15m Up + buy 5m Down  pays >= 1 in every state   (2 if O15 <= C < O5)
If O15 >  O5:  buy 15m Down + buy 5m Up  pays >= 1 in every state
So whenever  ask_leg1 + ask_leg2 + taker fees < 1  the set is a locked profit.  We walk both books to
find the executable size and log every opportunity; at resolution the paper PnL is computed.

usage: python -m pmbot.arb_scanner OUT_DIR [minutes] [min_edge=0.005]
"""
import asyncio
import json
import os
import sys
import time

import websockets

from .config import ASSETS, CHAINLINK_SYMBOL, WS_CLOB_MARKET, WS_RTDS
from .fees import fee_per_share
from .gamma import fetch_event, parse_event, slug_for, window_start


def walk(asks_a, asks_b, min_edge, max_shares=5000):
    """Greedy match of two ask ladders [(price,size)...] ascending; returns (shares, cost, avg_edge)."""
    ia = ib = 0
    la = [list(x) for x in asks_a]
    lb = [list(x) for x in asks_b]
    shares = cost = 0.0
    while ia < len(la) and ib < len(lb) and shares < max_shares:
        pa, sa = la[ia]
        pb, sb = lb[ib]
        unit = pa + pb + fee_per_share(pa) + fee_per_share(pb)
        if 1 - unit < min_edge:
            break
        q = min(sa, sb, max_shares - shares)
        shares += q
        cost += q * unit
        la[ia][1] -= q
        lb[ib][1] -= q
        if la[ia][1] <= 1e-9:
            ia += 1
        if lb[ib][1] <= 1e-9:
            ib += 1
    return shares, cost, (shares - cost) / shares if shares else 0.0


class ArbScanner:
    def __init__(self, out_dir, minutes=60, min_edge=0.005, assets=ASSETS):
        self.out = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.f = open(os.path.join(out_dir, "arb_opps.jsonl"), "a", buffering=1)
        self.deadline = time.time() + minutes * 60
        self.min_edge = min_edge
        self.assets = list(assets)
        self.books = {}      # token -> {"b": {p: s}, "a": {p: s}}
        self.pairs = {}      # (asset, T15) -> dict
        self.ws = None
        self.subscribed = set()
        self.best = {}       # (asset, T15) -> best opportunity seen
        self.cl = {a: {} for a in self.assets}  # asset -> {oracle_second: chainlink spot}
        self.min_gap_est_bp = 1.5  # with estimated strikes require a clear gap (estimate error ~0.1-0.5bp)

    def cl_twap(self, asset, T):
        """60s TWAP of Chainlink spot over [T-60, T-1] from RTDS (None if too many seconds missing)."""
        xs = [self.cl[asset][s] for s in range(T - 60, T) if s in self.cl[asset]]
        return sum(xs) / len(xs) if len(xs) >= 40 else None

    async def chainlink_loop(self):
        syms = {CHAINLINK_SYMBOL[a]: a for a in self.assets}
        while time.time() < self.deadline:
            try:
                async with websockets.connect(WS_RTDS, open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
                    while time.time() < self.deadline:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        if not msg:
                            continue
                        pl = json.loads(msg).get("payload") or {}
                        a = syms.get(pl.get("symbol"))
                        if a:
                            self.cl[a][int(pl["timestamp"]) // 1000] = float(pl["value"])
            except Exception as ex:  # noqa: BLE001
                print("rtds error", type(ex).__name__, ex, flush=True)
                await asyncio.sleep(2)

    def log(self, obj):
        self.f.write(json.dumps(obj) + "\n")

    # ------------------------------------------------------------ pair management
    async def refresh_pairs(self):
        while time.time() < self.deadline:
            now = time.time()
            T15 = window_start(now, 900)
            for a in self.assets:
                key = (a, T15)
                p = self.pairs.get(key)
                if p is None:
                    e15 = await asyncio.to_thread(fetch_event, slug_for(a, T15, "15m"))
                    e5 = await asyncio.to_thread(fetch_event, slug_for(a, T15 + 600, "5m"))
                    if not e15 or not e5:
                        continue
                    m15, m5 = parse_event(e15), parse_event(e5)
                    p = self.pairs[key] = {"asset": a, "T15": T15, "m15": m15, "m5": m5, "o15": None, "o5": None,
                                           "est": False}
                if p["o15"] is None:
                    prev = await asyncio.to_thread(fetch_event, slug_for(a, T15 - 300, "5m"))
                    if prev and (prev.get("eventMetadata") or {}).get("finalPrice"):
                        p["o15"] = prev["eventMetadata"]["finalPrice"]
                if p["o5"] is None and now > T15 + 600:
                    # strike of the 5m leg = its own priceToBeat, or the previous 5m window's finalPrice
                    own = await asyncio.to_thread(fetch_event, slug_for(a, T15 + 600, "5m"))
                    prev = await asyncio.to_thread(fetch_event, slug_for(a, T15 + 300, "5m"))
                    for ev, k in ((own, "priceToBeat"), (prev, "finalPrice")):
                        v = (ev or {}).get("eventMetadata") or {}
                        if v.get(k):
                            p["o5"] = v[k]
                            break
                # fallback: estimate missing strikes from the Chainlink stream
                if p["o5"] is None and now > T15 + 601:
                    e15 = p["o15"] if p["o15"] is not None else self.cl_twap(a, T15)
                    e5 = self.cl_twap(a, T15 + 600)
                    if e15 and e5 and abs(e5 / e15 - 1) * 1e4 >= self.min_gap_est_bp:
                        p["o15"], p["o5"], p["est"] = e15, e5, True
                elif p.get("est") and p["o5"] is not None:
                    # replace estimates by published values once they appear
                    own = await asyncio.to_thread(fetch_event, slug_for(a, T15 + 600, "5m"))
                    v = (own or {}).get("eventMetadata") or {}
                    if v.get("priceToBeat"):
                        p["o5"], p["est"] = v["priceToBeat"], False
            await self.sync_subs()
            await self.settle_old()
            await asyncio.sleep(5)

    def active_pairs(self):
        now = time.time()
        return [p for p in self.pairs.values() if p["T15"] + 600 <= now < p["T15"] + 900]

    async def sync_subs(self):
        if self.ws is None:
            return
        want = set()
        now = time.time()
        for p in self.pairs.values():
            if p["T15"] + 590 <= now < p["T15"] + 905:
                want |= {p["m15"]["token_up"], p["m15"]["token_down"], p["m5"]["token_up"], p["m5"]["token_down"]}
        add, rem = sorted(want - self.subscribed), sorted(self.subscribed - want)
        if add:
            await self.ws.send(json.dumps({"assets_ids": add, "operation": "subscribe"}))
        if rem:
            await self.ws.send(json.dumps({"assets_ids": rem, "operation": "unsubscribe"}))
            for t in rem:
                self.books.pop(t, None)
        self.subscribed = (self.subscribed | set(add)) - set(rem)

    # ------------------------------------------------------------ books
    def on_event(self, ev):
        t = ev.get("event_type")
        if t == "book":
            self.books[ev["asset_id"]] = {
                "b": {float(x["price"]): float(x["size"]) for x in ev["bids"]},
                "a": {float(x["price"]): float(x["size"]) for x in ev["asks"]}}
        elif t == "price_change":
            for c in ev["price_changes"]:
                bk = self.books.setdefault(c["asset_id"], {"b": {}, "a": {}})
                side = bk["b"] if c["side"] == "BUY" else bk["a"]
                p, s = float(c["price"]), float(c["size"])
                if s <= 0:
                    side.pop(p, None)
                else:
                    side[p] = s
        else:
            return
        self.check()

    def asks(self, tok):
        bk = self.books.get(tok)
        return sorted(bk["a"].items()) if bk else []

    def check(self):
        now = time.time()
        for p in self.active_pairs():
            if p["o15"] is None or p["o5"] is None:
                continue
            if p["o15"] <= p["o5"]:
                leg1, leg2, kind = p["m15"]["token_up"], p["m5"]["token_down"], "15U+5D"
            else:
                leg1, leg2, kind = p["m15"]["token_down"], p["m5"]["token_up"], "15D+5U"
            a1, a2 = self.asks(leg1), self.asks(leg2)
            if not a1 or not a2:
                continue
            shares, cost, edge = walk(a1, a2, self.min_edge)
            if shares <= 0:
                continue
            key = (p["asset"], p["T15"])
            opp = {"t": now, "asset": p["asset"], "T15": p["T15"], "rel15": round(now - p["T15"], 2), "kind": kind,
                   "strikes_estimated": p.get("est", False),
                   "o15": p["o15"], "o5": p["o5"], "gap_bp": (p["o5"] / p["o15"] - 1) * 1e4,
                   "best1": a1[0], "best2": a2[0], "shares": round(shares, 2), "cost": round(cost, 4),
                   "profit": round(shares - cost, 4), "edge": round(edge, 4)}
            b = self.best.get(key)
            if b is None or opp["profit"] > b["profit"]:
                self.best[key] = opp
            self.log({"type": "opp", **opp})

    async def settle_old(self):
        now = time.time()
        for key, p in list(self.pairs.items()):
            if now < p["T15"] + 900 + 120:
                continue
            b = self.best.pop(key, None)
            if b:
                e5 = await asyncio.to_thread(fetch_event, slug_for(p["asset"], p["T15"] + 600, "5m"))
                e15 = await asyncio.to_thread(fetch_event, slug_for(p["asset"], p["T15"], "15m"))
                w5, w15 = parse_event(e5)["winner"], parse_event(e15)["winner"]
                if w5 and w15:
                    pay = (int(w15 == "Up") + int(w5 == "Down")) if b["kind"] == "15U+5D" else (int(w15 == "Down") + int(w5 == "Up"))
                    self.log({"type": "settle", **b, "w5": w5, "w15": w15, "payoff_per_set": pay,
                              "paper_pnl": round(b["shares"] * pay - b["cost"], 4)})
                    print(time.strftime("%H:%M:%S"), "SETTLE", b["asset"], b["kind"], "shares", b["shares"],
                          "pnl", round(b["shares"] * pay - b["cost"], 2), flush=True)
            else:
                self.log({"type": "no_opp", "asset": p["asset"], "T15": p["T15"]})
            self.pairs.pop(key, None)

    async def ws_loop(self):
        while time.time() < self.deadline:
            try:
                async with websockets.connect(WS_CLOB_MARKET, open_timeout=15, max_size=2**24, ping_interval=None) as ws:
                    self.ws, self.subscribed, self.books = ws, set(), {}
                    await ws.send(json.dumps({"assets_ids": [], "type": "market"}))
                    await self.sync_subs()

                    async def ping():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("PING")
                    pt = asyncio.create_task(ping())
                    try:
                        while time.time() < self.deadline:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            if msg == "PONG":
                                continue
                            data = json.loads(msg)
                            for ev in data if isinstance(data, list) else [data]:
                                self.on_event(ev)
                    finally:
                        pt.cancel()
                        self.ws = None
            except Exception as ex:  # noqa: BLE001
                print("ws error", type(ex).__name__, ex, flush=True)
                self.books = {}
                await asyncio.sleep(2)

    async def status_loop(self):
        while time.time() < self.deadline:
            await asyncio.sleep(30)
            for p in self.active_pairs():
                if p["o15"] is None or p["o5"] is None:
                    print(time.strftime("%H:%M:%S"), p["asset"], "waiting for strikes", p["o15"], p["o5"], flush=True)
                    continue
                if p["o15"] <= p["o5"]:
                    l1, l2 = p["m15"]["token_up"], p["m5"]["token_down"]
                else:
                    l1, l2 = p["m15"]["token_down"], p["m5"]["token_up"]
                a1, a2 = self.asks(l1), self.asks(l2)
                if a1 and a2:
                    unit = a1[0][0] + a2[0][0] + fee_per_share(a1[0][0]) + fee_per_share(a2[0][0])
                    self.log({"type": "status", "t": time.time(), "asset": p["asset"], "T15": p["T15"],
                              "gap_bp": (p["o5"] / p["o15"] - 1) * 1e4, "ask1": a1[0], "ask2": a2[0], "unit": unit})
                    print(time.strftime("%H:%M:%S"), p["asset"], f"gap={(p['o5']/p['o15']-1)*1e4:+.2f}bp",
                          "ask1", a1[0], "ask2", a2[0], f"unit_cost_with_fees={unit:.4f}", flush=True)

    async def run(self):
        try:
            await asyncio.wait_for(asyncio.gather(self.refresh_pairs(), self.ws_loop(), self.status_loop(),
                                                  self.chainlink_loop()),
                                   timeout=self.deadline - time.time() + 200)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    out = sys.argv[1]
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 60
    min_edge = float(sys.argv[3]) if len(sys.argv) > 3 else 0.005
    asyncio.run(ArbScanner(out, minutes, min_edge).run())
