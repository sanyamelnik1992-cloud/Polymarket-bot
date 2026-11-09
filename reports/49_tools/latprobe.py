"""How late does a process on this server wake up and see market data? 60 s probe.
1) asyncio timer overshoot (sleep 10 ms, measure the extra delay) -> scheduler / steal latency
2) CLOB market socket for the current BTC 5m window: rx - exchange timestamp of each message
3) Binance aggTrade: rx - trade time (T)"""
import asyncio, json, sys, time, statistics
sys.path.insert(0, "/home/pmbot/Polymarket-bot")
import websockets
from pmbot.gamma import fetch_event, parse_event, slug_for, window_start

def q(xs, f):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(f * len(xs)))] if xs else float("nan")

async def timer(dur, out):
    t_end = time.time() + dur
    while time.time() < t_end:
        t = time.perf_counter(); await asyncio.sleep(0.01)
        out.append((time.perf_counter() - t - 0.01) * 1000)

async def clob(dur, out):
    now = time.time(); T = window_start(now)
    m = parse_event(fetch_event(slug_for("btc", T)))
    async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market", open_timeout=15) as ws:
        await ws.send(json.dumps({"assets_ids": [m["token_up"], m["token_down"]], "type": "market"}))
        t_end = time.time() + dur
        while time.time() < t_end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            rx = time.time() * 1000
            try:
                d = json.loads(msg)
            except ValueError:
                continue
            for ev in (d if isinstance(d, list) else [d]):
                ts = ev.get("timestamp")
                if ts:
                    out.append(rx - int(ts))

async def binance(dur, out):
    async with websockets.connect("wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade", open_timeout=15) as ws:
        t_end = time.time() + dur
        while time.time() < t_end:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            rx = time.time() * 1000
            d = json.loads(msg)["data"]
            out.append(rx - d["T"])

async def main(dur=60):
    a, b, c = [], [], []
    await asyncio.gather(timer(dur, a), clob(dur, b), binance(dur, c))
    for name, xs in (("timer overshoot ms", a), ("CLOB rx-ts ms", b), ("Binance rx-T ms", c)):
        print(f"{name:20s} n={len(xs):6d} p50={q(xs,.5):8.1f} p90={q(xs,.9):8.1f} p99={q(xs,.99):8.1f} max={max(xs) if xs else float('nan'):8.1f}")

asyncio.run(main())
