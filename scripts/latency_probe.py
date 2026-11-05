"""Measure what matters for the 5m bot from THIS machine (run it on every candidate server).

  * CLOB REST round trip (TCP+TLS reuse, GET /time)                 -> order/cancel path proxy
  * CLOB market websocket: local receive - engine event timestamp   -> how late we see the book
  * Binance aggTrade: local receive - exchange event time E         -> how late we see the lead price
  * Chainlink via RTDS: local receive - oracle timestamp            -> resolution-source delay
Needs an accurate clock (chrony/NTP); the script reports the CLOB clock offset it can infer.

usage: python scripts/latency_probe.py [seconds=60]
"""
import asyncio
import json
import os
import statistics
import sys
import time

import requests
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.gamma import fetch_event, parse_event, slug_for, window_start  # noqa: E402


def q(xs):
    if len(xs) < 5:
        return "n<5"
    s = statistics.quantiles(xs, n=20)
    return f"p5={s[0]:.0f} p50={s[9]:.0f} p95={s[18]:.0f} ms (n={len(xs)})"


def rest_rtt(n=20):
    sess = requests.Session()
    sess.get("https://clob.polymarket.com/time", timeout=10)   # warm the connection
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        sess.get("https://clob.polymarket.com/time", timeout=10)
        out.append((time.perf_counter() - t0) * 1000)
    return out


async def clob_ws(secs):
    m = parse_event(fetch_event(slug_for("btc", window_start(time.time()))))
    lat = []
    async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market", max_size=2**24) as ws:
        await ws.send(json.dumps({"assets_ids": [m["token_up"], m["token_down"]], "type": "market"}))
        t0 = time.time()
        while time.time() - t0 < secs:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            rx = time.time() * 1000
            data = json.loads(msg)
            for ev in data if isinstance(data, list) else [data]:
                if ev.get("event_type") in ("price_change", "last_trade_price") and ev.get("timestamp"):
                    lat.append(rx - int(ev["timestamp"]))
    return lat


async def binance_ws(secs):
    lat = []
    async with websockets.connect("wss://data-stream.binance.vision/stream?streams=btcusdt@aggTrade") as ws:
        t0 = time.time()
        while time.time() - t0 < secs:
            d = json.loads(await ws.recv())["data"]
            lat.append(time.time() * 1000 - d["E"])
    return lat


async def rtds_ws(secs):
    lat = []
    async with websockets.connect("wss://ws-live-data.polymarket.com") as ws:
        await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
        t0 = time.time()
        while time.time() - t0 < secs:
            msg = await ws.recv()
            if not msg:
                continue
            p = json.loads(msg).get("payload") or {}
            if p.get("symbol") == "btc/usd":
                lat.append(time.time() * 1000 - p["timestamp"])
    return lat


async def main(secs):
    print(f"probing for {secs}s ...")
    rtt = await asyncio.to_thread(rest_rtt)
    c, b, r = await asyncio.gather(clob_ws(secs), binance_ws(secs), rtds_ws(secs))
    print(f"CLOB REST round trip        : {q(rtt)}")
    print(f"CLOB websocket (rx - engine): {q(c)}")
    print(f"Binance aggTrade (rx - E)   : {q(b)}")
    print(f"Chainlink RTDS (rx - oracle): {q(r)}")
    pm = statistics.median(c) if len(c) > 4 else float("nan")
    bn = statistics.median(b) if len(b) > 4 else float("nan")
    print("\nreaction budget for a maker after a Binance move (one-way see Binance + one-way cancel to CLOB):")
    print(f"  ~{bn + statistics.median(rtt) / 2:.0f} ms  vs. a taker exploiting the same move: >= {bn:.0f} + "
          f"(their order path) + 150 ms taker delay")
    print("  -> the maker must stay well below the taker's total; London/Dublin servers typically give ~110-125 ms.")
    if pm == pm and pm < 0:
        print("  note: negative websocket latency = local clock ahead of Polymarket; fix NTP/chrony first")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 60))
