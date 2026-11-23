"""Price-source race recorder (reports/87): which venue shows a BTC / ETH / SOL / XRP move first, as seen from this server.

Public sockets only (no keys, no orders): Binance spot (two hosts) and USD-M futures, Coinbase, OKX spot and swaps, Bybit
spot and linear perps, Kraken, Deribit, Hyperliquid, the Polymarket 5m Up books and the Chainlink prints. Rows go to
<dir>/YYYYmmdd-HH.csv (pmbot/feedrec.py has the format); scripts/feed_race.py analyses them. Stops by itself at the config's
`until` (a restart after that exits at once).

    python scripts/record_feeds.py config/feedrec.toml
"""
import asyncio
import json
import logging
import os
import signal
import sys
import time
import tomllib
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import websockets  # noqa: E402

from pmbot.bot.feeds import ClobFeed  # noqa: E402
from pmbot.bot.state import State, Window  # noqa: E402
from pmbot.config import WS_RTDS  # noqa: E402
from pmbot.feedrec import ASSETS, Writer, _f, parse_chainlink, sources  # noqa: E402
from pmbot.gamma import fetch_event, parse_event, slug_for, window_start  # noqa: E402

log = logging.getLogger("feedrec")
STOP = {"flag": False}


def load(path):
    c = tomllib.load(open(path, "rb"))
    until = datetime.fromisoformat(c["until"].replace("Z", "+00:00")).timestamp()
    return {"until": until, "dir": c.get("dir", "data/feeds"), "assets": tuple(c.get("assets", ASSETS)),
            "sources": list(c.get("sources", [])), "min_bp": float(c.get("min_bp", 0.2)),
            "heartbeat_ms": float(c.get("heartbeat_ms", 1000.0))}


def rx_ms():
    return time.time() * 1000.0


async def venue(name, spec, w, alive):
    url, subs, make_parser, app_ping, proto_ping = spec
    fails = 0
    while alive():
        parse = make_parser()
        try:
            # compression=None: Binance negotiates permessage-deflate by default and inflating every frame cost ~40 % more CPU.
            # Protocol pings on every socket (a dead one is closed within ~40 s), so recv() needs no per-message timeout:
            # asyncio.wait_for around each of ~5000 messages a second was the largest cost after the frame parsing.
            async with websockets.connect(url, open_timeout=15, ping_interval=proto_ping or 20, ping_timeout=20,
                                          max_size=2 ** 22, max_queue=None, compression=None) as ws:
                for m in subs:
                    await ws.send(json.dumps(m))
                w.mark(rx_ms(), "sys", name, "", "connect")
                fails = 0
                last = {"rx": rx_ms()}

                async def keep(text=app_ping[0] if app_ping else None, every=app_ping[1] if app_ping else 20):
                    while True:                      # the venue's own ping, and a silent socket (data stopped) is dropped
                        await asyncio.sleep(every)
                        if text:
                            await ws.send(text)
                        if rx_ms() - last["rx"] > 90_000:
                            await ws.close()
                            return
                pinger = asyncio.create_task(keep())
                try:
                    while alive():
                        msg = await ws.recv()
                        rx = last["rx"] = rx_ms()
                        try:
                            rows = parse(msg)
                        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                            continue
                        for a, bid, ask, ts in rows:
                            w.add(rx, name, a, bid, ask, ts)
                finally:
                    pinger.cancel()
        except Exception as e:  # noqa: BLE001
            w.mark(rx_ms(), "sys", name, "", f"error {type(e).__name__} {str(e)[:80]}")
            if fails < 3:
                log.warning("%s: %s %s", name, type(e).__name__, str(e)[:120])
            fails += 1
            await asyncio.sleep(min(300.0, 5.0 * 2 ** min(fails - 1, 6)))   # a blocked host is retried less and less often


async def chainlink(w, alive):
    while alive():
        try:
            async with websockets.connect(WS_RTDS, open_timeout=15, ping_interval=20, compression=None) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                    {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
                w.mark(rx_ms(), "sys", "cl", "", "connect")
                while alive():
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    rx = rx_ms()
                    try:
                        rows = parse_chainlink(msg)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    for a, v, _, ts in rows:
                        w.add(rx, "cl", a, v, v, ts)
        except Exception as e:  # noqa: BLE001
            w.mark(rx_ms(), "sys", "cl", "", f"error {type(e).__name__} {str(e)[:80]}")
            await asyncio.sleep(5)


class RecClobFeed(ClobFeed):
    """The lock bots' book feed; after every book event the top of the Up book of a 5m window is written (src "pm")."""

    def __init__(self, st, asset, w, alive):
        super().__init__(st, asset, alive=alive)
        self.w = w

    def wanted(self):
        """Only the Up book of the running window (the Up / Down books mirror each other; the next window is quoted but idle)."""
        now = time.time()
        return {w.token_up for w in self.st.windows.values()
                if w.asset == self.asset and w.start - 3 <= now < w.start + 300}

    def handle(self, ev, rx):
        super().handle(ev, rx)
        t = ev.get("event_type")
        if t == "book":
            toks = {ev.get("asset_id")}
        elif t == "price_change":
            toks = {c.get("asset_id") for c in ev.get("price_changes") or []}
        else:
            return
        ts = _f(ev.get("timestamp"))
        now_s = time.time()
        for tok in toks:
            win = self.st.token_window.get(tok)
            b = self.st.books.get(tok)
            if win is None or b is None or tok != win.token_up or not b.bids or not b.asks:
                continue
            if not win.start <= now_s < win.start + 300:
                continue                      # the next window's book is quoted too: only the running one matters
            self.w.add(rx_ms(), "pm", win.asset, max(b.bids), min(b.asks), ts, win.start)


async def polymarket(assets, w, alive):
    st = State(list(assets))
    feeds = {a: RecClobFeed(st, a, w, alive) for a in assets}
    tasks = [asyncio.create_task(f.run()) for f in feeds.values()]
    try:
        while alive():
            now = time.time()
            ws0 = window_start(now, 300)
            for start in (ws0, ws0 + 300):
                for a in assets:
                    if f"{a}-{start}" in st.windows:
                        continue
                    try:
                        e = await asyncio.to_thread(fetch_event, slug_for(a, start, "5m"))
                    except Exception as ex:  # noqa: BLE001
                        log.warning("gamma %s %s: %s", a, start, ex)
                        continue
                    if e:
                        m = parse_event(e)
                        st.add_window(Window(asset=a, start=start, slug=m["slug"], condition_id=m["condition_id"],
                                             token_up=m["token_up"], token_down=m["token_down"], end=start + 300))
            for key, win in list(st.windows.items()):
                if now > win.start + 305:
                    st.drop_window(key)
            for f in feeds.values():
                await f.sync()
            await asyncio.sleep(3)
    finally:
        for t in tasks:
            t.cancel()


async def lag_loop(w, alive, period_ms=10.0, report_ms=15.0):
    """A stall of the event loop delays every receive time in it: mark it so the analysis can drop those moves."""
    while alive():
        t = rx_ms()
        await asyncio.sleep(period_ms / 1000)
        over = rx_ms() - t - period_ms
        if over > report_ms:
            w.mark(t, "lag", "", f"{over:.1f}")


async def flush_loop(w, alive):
    while alive():
        await asyncio.sleep(1.0)
        w.flush()
        if w.need_compress:
            w.need_compress = False
            await asyncio.to_thread(w.compress_old, w.hour)
    w.flush()


async def main(cfg):
    if time.time() >= cfg["until"]:
        print("feedrec: past `until`, nothing to record")
        return
    w = Writer(cfg["dir"], cfg["min_bp"], cfg["heartbeat_ms"])

    def alive():
        return not STOP["flag"] and time.time() < cfg["until"]

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, lambda: STOP.__setitem__("flag", True))
    specs = sources(cfg["assets"])
    tasks = [venue(n, specs[n], w, alive) for n in cfg["sources"] if n in specs]
    if "cl" in cfg["sources"]:
        tasks.append(chainlink(w, alive))
    if "pm" in cfg["sources"]:
        tasks.append(polymarket(cfg["assets"], w, alive))
    w.mark(rx_ms(), "sys", "start", "", f"sources {' '.join(cfg['sources'])}")
    runners = [asyncio.create_task(t) for t in tasks]
    await asyncio.gather(lag_loop(w, alive), flush_loop(w, alive))
    for t in runners:
        t.cancel()
    w.mark(rx_ms(), "sys", "stop", "", "deadline" if time.time() >= cfg["until"] else "signal")
    w.close()
    await asyncio.to_thread(w.compress_old, None, True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main(load(sys.argv[1] if len(sys.argv) > 1 else "config/feedrec.toml")))
