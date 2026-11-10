"""Compare Polymarket's new price socket (PolyBolt) with the legacy RTDS feed the lock bots read. READ ONLY: no orders.

Why (reports/52): the bots take Chainlink prints from the legacy socket wss://ws-live-data.polymarket.com, which arrives
~1.4 s after the print time and is announced for removal ("RTDS is legacy for reference prices", docs changelog 15 Sep
2026). PolyBolt (wss://ws-live-v2.polymarket.com/ws) serves the same Chainlink prints and the 60 s Chainlink TWAP the
markets resolve on, but needs the CLOB API credentials. Before moving the bots this script answers:
  1. how late each feed delivers a print (arrival time minus the print's own timestamp) and how many seconds it drops;
  2. how much earlier PolyBolt delivers the same print;
  3. which 60 prints the published TWAP averages (the bots assume [t-59, t] of the TWAP stamp; strike = TWAP at T-3).

usage, on the server, as the bot user with the secrets env (the key is only used to log in to the price socket):
    set -a; . /etc/pmbot/pmbot.env; set +a; .venv/bin/python scripts/polybolt_probe.py [SECONDS=120]

Not run in CI and not used by any bot. The network part could not be tested from the development sandbox (no websockets,
no credentials there); the statistics are covered by tests/test_polybolt_probe.py.
"""
import asyncio
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ASSETS = ("btc", "eth", "sol", "xrp")


def now_ms():
    return int(time.time() * 1000)


def _q(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def asset_of(symbol):
    """'btcusd' / 'btc/usd' / 'BTCUSD' -> 'btc'."""
    s = "".join(ch for ch in str(symbol).lower() if ch.isalnum())
    return s[:-3] if s.endswith("usd") else s


def latency_stats(recs):
    """recs: {(asset, print second): (value, rx_ms)} -> per asset (n, median, p90, max latency in ms, seconds missing)."""
    out = {}
    for a in sorted({k[0] for k in recs}):
        secs = sorted(s for (x, s) in recs if x == a)
        lat = [recs[(a, s)][1] - s * 1000 for s in secs]
        missing = (secs[-1] - secs[0] + 1) - len(secs) if secs else 0
        out[a] = {"n": len(secs), "median": statistics.median(lat), "p90": _q(lat, 0.9), "max": max(lat),
                  "missing": missing}
    return out


def lead_stats(fast, slow):
    """How much earlier `fast` delivered the prints both feeds have: per asset (n, median, p90 lead in ms; > 0 = fast first)."""
    out = {}
    for a in sorted({k[0] for k in fast} & {k[0] for k in slow}):
        lead = [slow[k][1] - fast[k][1] for k in fast if k[0] == a and k in slow]
        if lead:
            out[a] = {"n": len(lead), "median": statistics.median(lead), "p90": _q(lead, 0.9),
                      "same_value": sum(abs(fast[k][0] / slow[k][0] - 1) < 1e-9 for k in fast if k[0] == a and k in slow)}
    return out


def twap_alignment(prints, twaps, shifts=range(-3, 6)):
    """Which 60 prints does the TWAP stamped t average? For each shift d: median |TWAP(t) / mean(prints[t-d-59 .. t-d]) - 1|
    in bp over the TWAP updates with all 60 prints present. Returns {asset: [(d, median error bp, n), ...]} best first."""
    out = {}
    for a in sorted({k[0] for k in twaps}):
        rows = []
        for d in shifts:
            errs = []
            for (x, t), (v, _) in twaps.items():
                if x != a:
                    continue
                xs = [prints.get((a, s)) for s in range(t - d - 59, t - d + 1)]
                if any(p is None for p in xs):
                    continue
                errs.append(abs(v / (sum(p[0] for p in xs) / 60) - 1) * 1e4)
            if errs:
                rows.append((d, statistics.median(errs), len(errs)))
        if rows:
            out[a] = sorted(rows, key=lambda r: r[1])
    return out


def report(pb, rt, tw):
    lines = []
    for name, recs in (("PolyBolt price.crypto (Chainlink)", pb), ("legacy RTDS crypto_prices_chainlink", rt),
                       ("PolyBolt price.crypto.twap (60 s)", tw)):
        lines.append(f"\n{name}: arrival minus print time, ms")
        if not recs:
            lines.append("   no prints received")
            continue
        for a, s in latency_stats(recs).items():
            lines.append(f"   {a}: n {s['n']:4d}  median {s['median']:6.0f}  p90 {s['p90']:6.0f}  max {s['max']:6.0f}"
                         f"  seconds missing {s['missing']}")
    ls = lead_stats(pb, rt)
    lines.append("\nPolyBolt ahead of RTDS on the same print, ms (> 0 = PolyBolt first)")
    for a, s in ls.items():
        lines.append(f"   {a}: n {s['n']:4d}  median {s['median']:+6.0f}  p90 {s['p90']:+6.0f}  same value {s['same_value']}/{s['n']}")
    if not ls:
        lines.append("   no common prints")
    al = twap_alignment(pb or rt, tw)
    lines.append("\nTWAP stamp t = mean of the prints [t-d-59, t-d]: best shift d, median error bp")
    for a, rows in al.items():
        d, err, n = rows[0]
        lines.append(f"   {a}: d = {d} (error {err:.4f} bp on {n} updates); next best d = {rows[1][0]} ({rows[1][1]:.4f} bp)"
                     if len(rows) > 1 else f"   {a}: d = {d} (error {err:.4f} bp on {n} updates)")
    if not al:
        lines.append("   not enough data (needs 60 consecutive prints before a TWAP update: run for 150 s or more)")
    return "\n".join(lines)


async def run(seconds):
    import websockets
    from polymarket import AsyncSecureClient
    from polymarket.streams import CryptoPriceSpec, CryptoTwapPriceSpec

    from pmbot.config import CHAINLINK_SYMBOL, WS_RTDS

    syms = [a + "usd" for a in ASSETS]
    pb, rt, tw = {}, {}, {}
    stop = time.time() + seconds
    client = await AsyncSecureClient.create(private_key=os.environ["PMBOT_PRIVATE_KEY"],
                                            wallet=os.environ.get("PMBOT_WALLET") or None)

    async def polybolt():
        async with await client.subscribe([CryptoPriceSpec(symbols=syms), CryptoTwapPriceSpec(symbols=syms)]) as stream:
            async for ev in stream:
                rx = now_ms()
                if time.time() > stop:
                    return
                if getattr(ev, "type", None) != "update":
                    continue                      # "subscribe" = history snapshot, not a live print
                pl = ev.payload
                key = (asset_of(pl.symbol), int(pl.timestamp.timestamp()))
                (tw if ev.topic == "prices.crypto.twap" else pb).setdefault(key, (float(pl.value), rx))

    async def rtds():
        rev = {v: k for k, v in CHAINLINK_SYMBOL.items() if k in ASSETS}
        async with websockets.connect(WS_RTDS, open_timeout=15, ping_interval=20) as ws:
            await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
            while time.time() < stop:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                rx = now_ms()
                if not msg:
                    continue
                p = json.loads(msg).get("payload") or {}
                a = rev.get(p.get("symbol"))
                if a:
                    rt.setdefault((a, int(p["timestamp"]) // 1000), (float(p["value"]), rx))

    res = await asyncio.gather(asyncio.wait_for(polybolt(), seconds + 20), asyncio.wait_for(rtds(), seconds + 20),
                               return_exceptions=True)
    for name, r in zip(("PolyBolt", "RTDS"), res):
        if isinstance(r, Exception):
            print(f"{name} stream ended with {type(r).__name__}: {str(r)[:200]}")
    close = getattr(client, "close", None)
    if close:
        await close()
    return pb, rt, tw


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    if not os.environ.get("PMBOT_PRIVATE_KEY"):
        sys.exit("PMBOT_PRIVATE_KEY is not set: run with the secrets env (see the usage line at the top)")
    print(f"PolyBolt vs RTDS, {secs:.0f} s, assets {', '.join(ASSETS)}; read only")
    print(report(*asyncio.run(run(secs))))
