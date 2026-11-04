"""Pre-flight checks for a trading server. Run before every first start on a new machine.

usage: python scripts/preflight.py [--live]
Checks: Python & packages, configs, clock skew vs Polymarket, reachability + latency of every endpoint the bots
use, websocket handshakes, geoblock status, disk space; with --live also env secrets (presence only).
Exit code 0 = all required checks passed.
"""
import asyncio
import importlib
import json
import os
import shutil
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
results = []


def report(status, name, detail=""):
    results.append(status)
    print(f"[{status}] {name}: {detail}")


def http(name, url, params=None, required=True, n=3):
    lat = []
    for _ in range(n):
        t0 = time.time()
        try:
            r = requests.get(url, params=params, timeout=15)
            lat.append((time.time() - t0) * 1000)
            if r.status_code >= 400:
                report(FAIL if required else WARN, name, f"HTTP {r.status_code}")
                return None
        except requests.RequestException as e:
            report(FAIL if required else WARN, name, str(e)[:120])
            return None
    report(OK, name, f"median latency {sorted(lat)[len(lat) // 2]:.0f} ms")
    return r


async def ws(name, url, sub=None):
    import websockets
    t0 = time.time()
    try:
        async with websockets.connect(url, open_timeout=10) as c:
            if sub:
                await c.send(json.dumps(sub))
            await asyncio.wait_for(c.recv(), timeout=15)
        report(OK, name, f"first message after {(time.time() - t0) * 1000:.0f} ms")
    except Exception as e:  # noqa: BLE001
        report(FAIL, name, f"{type(e).__name__} {str(e)[:100]}")


async def clob_ws():
    """Subscribe to the current BTC 5m market and wait for its book snapshot."""
    import websockets
    from pmbot.gamma import fetch_event, parse_event, slug_for, window_start
    try:
        m = parse_event(fetch_event(slug_for("btc", window_start(time.time()))))
        t0 = time.time()
        async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market", open_timeout=10) as c:
            await c.send(json.dumps({"assets_ids": [m["token_up"]], "type": "market"}))
            await asyncio.wait_for(c.recv(), timeout=15)
        report(OK, "CLOB market websocket", f"book after {(time.time() - t0) * 1000:.0f} ms")
    except Exception as e:  # noqa: BLE001
        report(FAIL, "CLOB market websocket", f"{type(e).__name__} {str(e)[:100]}")


def main(live):
    v = sys.version_info
    report(OK if v >= (3, 11) else FAIL, "python", f"{v.major}.{v.minor}.{v.micro} (need >= 3.11)")
    for mod in ("requests", "pandas", "numpy", "scipy", "websockets", "pyarrow"):
        try:
            importlib.import_module(mod)
            report(OK, f"package {mod}")
        except ImportError:
            report(FAIL, f"package {mod}", "missing: pip install -r requirements.txt")
    try:
        importlib.import_module("polymarket")
        report(OK, "package polymarket-client")
    except ImportError:
        report(FAIL if live else WARN, "package polymarket-client", "needed for live: pip install polymarket-client==0.16.0")
    from pmbot.bot.config import load_config
    from pmbot.bot.hourly import load_hourly_config
    for p in ("config/paper_v2.toml", "config/hourly.toml"):
        try:
            (load_hourly_config if "hourly" in p else load_config)(p)
            report(OK, f"config {p}")
        except Exception as e:  # noqa: BLE001
            report(FAIL, f"config {p}", str(e))
    free = shutil.disk_usage(".").free / 1e9
    report(OK if free > 5 else WARN, "disk", f"{free:.1f} GB free")
    r = http("CLOB /time", "https://clob.polymarket.com/time")
    if r is not None:
        try:
            server = float(r.text.strip())
            skew = time.time() - server
            # /time has 1-second resolution, so only gross skew is detectable here
            report(OK if abs(skew) < 2.0 else FAIL, "clock skew vs Polymarket",
                   f"{skew * 1000:+.0f} ms (1s resolution; run `timedatectl` / chrony if > 2s)")
        except ValueError:
            report(WARN, "clock skew", f"unexpected /time response {r.text[:50]}")
    http("Gamma API", "https://gamma-api.polymarket.com/markets", {"limit": 1})
    http("Data API v2", "https://data-api.polymarket.com/v2/status")
    http("Binance REST (data-api.binance.vision)", "https://data-api.binance.vision/api/v3/time")
    http("aviationweather METAR", "https://aviationweather.gov/api/data/metar", {"ids": "EGLC", "format": "json"},
         required=False)
    asyncio.run(clob_ws())
    asyncio.run(ws("RTDS Chainlink websocket", "wss://ws-live-data.polymarket.com",
                   {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*",
                                                              "filters": ""}]}))
    asyncio.run(ws("Binance websocket", "wss://data-stream.binance.vision/ws/btcusdt@bookTicker"))
    try:
        g = requests.get("https://polymarket.com/api/geoblock", timeout=15).json()
        st = FAIL if (g.get("blocked") and live) else (WARN if g.get("blocked") else OK)
        report(st, "geoblock", f"blocked={g.get('blocked')} country={g.get('country')} region={g.get('region')}")
    except Exception as e:  # noqa: BLE001
        report(FAIL if live else WARN, "geoblock", str(e)[:100])
    if live:
        for k in ("PMBOT_PRIVATE_KEY", "PMBOT_LIVE_CONFIRM"):
            report(OK if os.environ.get(k) else FAIL, f"env {k}", "set" if os.environ.get(k) else "missing")
    fails = results.count(FAIL)
    print(f"\n{len(results)} checks: {fails} failed, {results.count(WARN)} warnings")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main("--live" in sys.argv))
