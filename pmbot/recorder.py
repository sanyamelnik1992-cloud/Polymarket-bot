"""Live recorder: Polymarket CLOB books for the current/next 5m Up/Down markets,
Chainlink oracle prices (RTDS) and Binance top-of-book, all stamped with local receive time.

Output: JSONL files in `out_dir`:
  clob.jsonl      raw CLOB market-channel events + "rx" (local ms)
  chainlink.jsonl {"sym","ts","v","rx"}
  binance.jsonl   {"sym","bid","ask","rx"}  (throttled to <=10/s per symbol)
  markets.jsonl   parsed market metadata for every subscribed window
"""
import asyncio
import json
import os
import time

import websockets

from .config import ASSETS, BINANCE_SYMBOL, CHAINLINK_SYMBOL, WINDOW, WS_BINANCE, WS_CLOB_MARKET, WS_RTDS
from .gamma import fetch_event, parse_event, slug_for, window_start


def now_ms() -> int:
    return int(time.time() * 1000)


class Recorder:
    def __init__(self, out_dir: str, assets=ASSETS, duration_s: float = 3600):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.assets = list(assets)
        self.deadline = time.time() + duration_s
        self.files = {k: open(os.path.join(out_dir, f"{k}.jsonl"), "a", buffering=1 << 16)
                      for k in ("clob", "chainlink", "binance", "markets")}
        self.known = {}          # slug -> parsed market
        self.tokmap = {}         # token id -> short key
        self.subscribed = {a: set() for a in self.assets}  # asset -> token ids
        self.clob_ws = {}
        self.stats = {"clob": 0, "chainlink": 0, "binance": 0}

    def write(self, kind, obj):
        self.files[kind].write(json.dumps(obj, separators=(",", ":")) + "\n")
        if kind in self.stats:
            self.stats[kind] += 1

    @property
    def alive(self):
        return time.time() < self.deadline

    # ---------------- market discovery ----------------
    async def discover_loop(self):
        while self.alive:
            ws0 = window_start(time.time())
            for start in (ws0, ws0 + WINDOW):
                for a in self.assets:
                    slug = slug_for(a, start)
                    if slug in self.known:
                        continue
                    try:
                        e = await asyncio.to_thread(fetch_event, slug)
                    except Exception as ex:  # noqa: BLE001
                        print("discover error", slug, ex)
                        continue
                    if not e:
                        continue
                    m = parse_event(e)
                    m["asset"] = a
                    m["window_start"] = start
                    self.known[slug] = m
                    self.write("markets", m)
            await self.sync_subscriptions()
            await asyncio.sleep(5)

    def wanted_tokens(self, asset):
        t = time.time()
        out = set()
        for m in self.known.values():
            if m["asset"] != asset:
                continue
            # subscribed from 20s before the window opens until 15s after it closes (keeps the feed light)
            if m["window_start"] - 20 <= t <= m["window_start"] + WINDOW + 15:
                out.update([m["token_up"], m["token_down"]])
        return out

    async def sync_subscriptions(self):
        for a in self.assets:
            ws = self.clob_ws.get(a)
            if ws is None:
                continue
            want = self.wanted_tokens(a)
            add = sorted(want - self.subscribed[a])
            rem = sorted(self.subscribed[a] - want)
            try:
                if add:
                    await ws.send(json.dumps({"assets_ids": add, "operation": "subscribe", "custom_feature_enabled": True}))
                if rem:
                    await ws.send(json.dumps({"assets_ids": rem, "operation": "unsubscribe"}))
                self.subscribed[a] = (self.subscribed[a] | set(add)) - set(rem)
            except Exception as ex:  # noqa: BLE001
                print("sub error", a, ex)

    # ---------------- feeds ----------------
    async def clob_loop(self, asset):
        await asyncio.sleep(3)  # let discovery find the markets first
        while self.alive:
            try:
                async with websockets.connect(WS_CLOB_MARKET, open_timeout=15, max_size=2**24, ping_interval=None,
                                              max_queue=None) as ws:
                    want = sorted(self.wanted_tokens(asset))
                    await ws.send(json.dumps({"assets_ids": want, "type": "market", "custom_feature_enabled": True}))
                    self.subscribed[asset] = set(want)
                    self.clob_ws[asset] = ws
                    self.write("clob", {"e": "conn", "asset": asset, "state": "up", "rx": now_ms()})
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        while self.alive:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            rx = now_ms()
                            if msg == "PONG":
                                continue
                            data = json.loads(msg)
                            for ev in (data if isinstance(data, list) else [data]):
                                self.write("clob", self.compact(ev, rx))
                    finally:
                        pinger.cancel()
                        self.clob_ws.pop(asset, None)
                        self.write("clob", {"e": "conn", "asset": asset, "state": "down", "rx": now_ms()})
            except Exception as ex:  # noqa: BLE001
                print(time.strftime("%H:%M:%S"), "clob ws error", asset, type(ex).__name__, ex, flush=True)
                await asyncio.sleep(2)

    def tok_key(self, token_id):
        """Short key like 'btc-1793786100-U' for a token id (falls back to the raw id)."""
        k = self.tokmap.get(token_id)
        if k is None:
            for m in self.known.values():
                if token_id in (m["token_up"], m["token_down"]):
                    k = f'{m["asset"]}-{m["window_start"]}-{"U" if token_id == m["token_up"] else "D"}'
                    self.tokmap[token_id] = k
                    break
        return k or token_id

    def compact(self, ev, rx):
        t = ev.get("event_type")
        ts = int(ev.get("timestamp") or 0)
        if t == "price_change":
            return {"e": "pc", "rx": rx, "ts": ts, "c": [
                [self.tok_key(c["asset_id"]), float(c["price"]), float(c["size"]), c["side"][0],
                 float(c["best_bid"] or 0), float(c["best_ask"] or 0)] for c in ev["price_changes"]]}
        if t == "best_bid_ask":
            return {"e": "bba", "rx": rx, "ts": ts, "k": self.tok_key(ev["asset_id"]),
                    "b": float(ev["best_bid"] or 0), "a": float(ev["best_ask"] or 0)}
        if t == "book":
            return {"e": "book", "rx": rx, "ts": ts, "k": self.tok_key(ev["asset_id"]),
                    "b": [[float(x["price"]), float(x["size"])] for x in ev["bids"]],
                    "a": [[float(x["price"]), float(x["size"])] for x in ev["asks"]]}
        if t == "last_trade_price":
            return {"e": "trade", "rx": rx, "ts": ts, "k": self.tok_key(ev["asset_id"]),
                    "p": float(ev["price"]), "s": float(ev["size"]), "side": ev.get("side")}
        ev["rx"] = rx
        return ev

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    async def chainlink_loop(self):
        syms = {CHAINLINK_SYMBOL[a] for a in self.assets}
        while self.alive:
            try:
                async with websockets.connect(WS_RTDS, open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
                    while self.alive:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        rx = now_ms()
                        if not msg:
                            continue
                        d = json.loads(msg)
                        p = d.get("payload") or {}
                        if p.get("symbol") in syms:
                            self.write("chainlink", {"sym": p["symbol"], "ts": p["timestamp"], "v": p["value"], "rx": rx})
            except Exception as ex:  # noqa: BLE001
                print("chainlink ws error", type(ex).__name__, ex)
                await asyncio.sleep(2)

    async def binance_loop(self):
        streams = "/".join(f"{BINANCE_SYMBOL[a].lower()}@bookTicker" for a in self.assets)
        last = {}
        while self.alive:
            try:
                async with websockets.connect(f"{WS_BINANCE}?streams={streams}", open_timeout=15) as ws:
                    while self.alive:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        rx = now_ms()
                        d = json.loads(msg)["data"]
                        s = d["s"]
                        key = (d["b"], d["a"])
                        if last.get(s) == key:
                            continue
                        last[s] = key
                        self.write("binance", {"sym": s, "bid": float(d["b"]), "ask": float(d["a"]), "rx": rx})
            except Exception as ex:  # noqa: BLE001
                print("binance ws error", type(ex).__name__, ex)
                await asyncio.sleep(2)

    async def stats_loop(self):
        while self.alive:
            await asyncio.sleep(60)
            for f in self.files.values():
                f.flush()
            print(time.strftime("%H:%M:%S"), "stats", self.stats, "subs", {a: len(v) for a, v in self.subscribed.items()}, flush=True)

    async def run(self):
        tasks = [self.discover_loop(), self.chainlink_loop(), self.binance_loop(), self.stats_loop()]
        tasks += [self.clob_loop(a) for a in self.assets]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=self.deadline - time.time() + 5)
        except asyncio.TimeoutError:
            pass
        finally:
            for f in self.files.values():
                f.close()
