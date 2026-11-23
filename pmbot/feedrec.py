"""Price-source race recorder (reports/87): the best bid / ask of BTC, ETH, SOL and XRP on many public venues, stamped with
the time each update reached this server, plus the Polymarket 5m Up books and the Chainlink prints. scripts/feed_race.py
reads the files and tells which source shows a move first, by how many ms, and when the Polymarket makers react.

Read-only public sockets: no keys, no orders. One CSV row per kept update:
    rx_ms,src,asset,bid,ask,ts_ms,extra
rx_ms = our receive time (wall clock, 0.1 ms), ts_ms = the venue's own timestamp when it sends one (else empty), extra =
the window start for Polymarket rows. A venue row is kept when its mid moved >= `min_bp` from the last kept row of that
(src, asset) or `heartbeat_ms` passed with any change: moves of a few bp keep their first update to ~0.2 bp, and BTC's
hundreds of size-only updates a second are not written. Rows with src "lag" mark event-loop stalls (asset empty, bid = the
stall in ms): timings inside a stall are unreliable. Rows with src "sys" mark (re)connections and errors.
"""
import gzip
import json
import math
import os
import shutil
import time
from datetime import datetime

ASSETS = ("btc", "eth", "sol", "xrp")

# venue symbol -> asset, per source family
BN_SYM = {"BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol", "XRPUSDT": "xrp"}
CB_SYM = {"BTC-USD": "btc", "ETH-USD": "eth", "SOL-USD": "sol", "XRP-USD": "xrp"}
OKX_SYM = {"BTC-USDT": "btc", "ETH-USDT": "eth", "SOL-USDT": "sol", "XRP-USDT": "xrp"}
OKXF_SYM = {k + "-SWAP": v for k, v in OKX_SYM.items()}
KR_SYM = {"BTC/USD": "btc", "ETH/USD": "eth", "SOL/USD": "sol", "XRP/USD": "xrp"}
DB_SYM = {"BTC-PERPETUAL": "btc", "ETH-PERPETUAL": "eth", "SOL_USDC-PERPETUAL": "sol", "XRP_USDC-PERPETUAL": "xrp"}
HL_SYM = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
CL_SYM = {"btc/usd": "btc", "eth/usd": "eth", "sol/usd": "sol", "xrp/usd": "xrp"}


def _inv(d, assets):
    return [k for k, v in d.items() if v in assets]


def iso_ms(s):
    """'2026-11-23T12:27:55.532599Z' -> epoch ms (float)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000.0


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 and math.isfinite(v) else None


def _between(msg, key, start=0):
    i = msg.find(key, start)
    if i < 0:
        return None
    i += len(key)
    return msg[i:msg.find('"', i)]


class PriceGate:
    """Skip the JSON of a message whose best prices did not change: Binance sends ~600 (spot) and ~2000 (futures) book updates a
    second for the four coins, most of them size-only; two string finds cost ~1 us, json.loads + the writer ~15 us."""

    def __init__(self, parse, keys):
        self.parse, self.keys, self.last = parse, keys, {}

    def __call__(self, msg):
        if not msg.startswith("{"):
            return self.parse(msg)
        k = tuple(_between(msg, x) for x in self.keys)
        if None in k:
            return self.parse(msg)
        name = _between(msg, '"stream":"') or _between(msg, '"instId":"')
        if self.last.get(name) == k:
            return []
        self.last[name] = k
        return self.parse(msg)


# ---- parsers: one message (text) -> list of (asset, bid, ask, ts_ms or None); [] for anything else. Never raise on odd input.
def parse_binance(msg, with_ts=False):
    d = json.loads(msg).get("data") or {}
    a = BN_SYM.get(d.get("s"))
    if not a:
        return []
    return [(a, _f(d.get("b")), _f(d.get("a")), float(d["E"]) if with_ts and d.get("E") else None)]


def parse_coinbase(msg):
    d = json.loads(msg)
    if d.get("type") != "ticker":
        return []
    a = CB_SYM.get(d.get("product_id"))
    if not a:
        return []
    return [(a, _f(d.get("best_bid")), _f(d.get("best_ask")), iso_ms(d["time"]) if d.get("time") else None)]


def parse_okx(msg, syms=OKX_SYM):
    if msg == "pong":
        return []
    d = json.loads(msg)
    a = syms.get((d.get("arg") or {}).get("instId"))
    if not a or not d.get("data"):
        return []
    out = []
    for x in d["data"]:
        bids, asks = x.get("bids") or [], x.get("asks") or []
        if bids and asks:
            out.append((a, _f(bids[0][0]), _f(asks[0][0]), _f(x.get("ts"))))
    return out


class BybitBook:
    """Bybit orderbook.1 keeps the last side when a message leaves one of them empty."""

    def __init__(self):
        self.last = {}

    def parse(self, msg):
        d = json.loads(msg)
        topic = d.get("topic") or ""
        if not topic.startswith("orderbook.1."):
            return []
        a = BN_SYM.get(topic.split(".")[-1])
        x = d.get("data") or {}
        if not a:
            return []
        pb, pa = self.last.get(a, (None, None))
        b = _f(x["b"][0][0]) if x.get("b") else pb
        k = _f(x["a"][0][0]) if x.get("a") else pa
        self.last[a] = (b, k)
        ts = d.get("cts") or d.get("ts")
        return [(a, b, k, float(ts) if ts else None)]


def parse_kraken(msg):
    d = json.loads(msg)
    if d.get("channel") != "ticker":
        return []
    out = []
    for x in d.get("data") or []:
        a = KR_SYM.get(x.get("symbol"))
        if a:
            out.append((a, _f(x.get("bid")), _f(x.get("ask")), iso_ms(x["timestamp"]) if x.get("timestamp") else None))
    return out


def parse_deribit(msg):
    d = json.loads(msg)
    x = (d.get("params") or {}).get("data") or {}
    a = DB_SYM.get(x.get("instrument_name"))
    if d.get("method") != "subscription" or not a:
        return []
    return [(a, _f(x.get("best_bid_price")), _f(x.get("best_ask_price")), _f(x.get("timestamp")))]


def parse_hyperliquid(msg):
    d = json.loads(msg)
    if d.get("channel") != "bbo":
        return []
    x = d.get("data") or {}
    a = HL_SYM.get(x.get("coin"))
    bbo = x.get("bbo") or []
    if not a or len(bbo) < 2:
        return []
    return [(a, _f((bbo[0] or {}).get("px")), _f((bbo[1] or {}).get("px")), _f(x.get("time")))]


def parse_chainlink(msg):
    if not msg:
        return []
    pl = json.loads(msg).get("payload") or {}
    a = CL_SYM.get(pl.get("symbol"))
    if not a or pl.get("value") is None:
        return []
    v = _f(pl["value"])
    return [(a, v, v, _f(pl.get("timestamp")))]


BN_KEYS = ('"b":"', '"a":"')                  # Binance bookTicker: best bid / ask price strings
OKX_KEYS = ('"asks":[["', '"bids":[["')        # OKX bbo-tbt: first ask / bid level price strings


# ---- sources: name -> (url, subscribe messages, parser factory, (ping text, every s) or None, protocol ping interval)
def sources(assets=ASSETS):
    bn = "/".join(f"{s.lower()}@bookTicker" for s in _inv(BN_SYM, assets))
    return {
        "bn": (f"wss://data-stream.binance.vision/stream?streams={bn}", [], lambda: PriceGate(parse_binance, BN_KEYS), None, 20),
        "bnc": (f"wss://stream.binance.com:9443/stream?streams={bn}", [], lambda: PriceGate(parse_binance, BN_KEYS), None, 20),
        "bnf": (f"wss://fstream.binance.com/stream?streams={bn}", [],
                lambda: PriceGate(lambda m: parse_binance(m, True), BN_KEYS), None, 20),
        "cb": ("wss://ws-feed.exchange.coinbase.com",
               [{"type": "subscribe", "product_ids": _inv(CB_SYM, assets), "channels": ["ticker"]}],
               lambda: parse_coinbase, None, 20),
        "okx": ("wss://ws.okx.com:8443/ws/v5/public",
                [{"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": s} for s in _inv(OKX_SYM, assets)]}],
                lambda: PriceGate(parse_okx, OKX_KEYS), ("ping", 20), None),
        "okxf": ("wss://ws.okx.com:8443/ws/v5/public",
                 [{"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": s} for s in _inv(OKXF_SYM, assets)]}],
                 lambda: PriceGate(lambda m: parse_okx(m, OKXF_SYM), OKX_KEYS), ("ping", 20), None),
        "bybit": ("wss://stream.bybit.com/v5/public/spot",
                  [{"op": "subscribe", "args": [f"orderbook.1.{s}" for s in _inv(BN_SYM, assets)]}],
                  lambda: BybitBook().parse, ('{"op":"ping"}', 20), None),
        "bybitf": ("wss://stream.bybit.com/v5/public/linear",
                   [{"op": "subscribe", "args": [f"orderbook.1.{s}" for s in _inv(BN_SYM, assets)]}],
                   lambda: BybitBook().parse, ('{"op":"ping"}', 20), None),
        "kraken": ("wss://ws.kraken.com/v2",
                   [{"method": "subscribe", "params": {"channel": "ticker", "symbol": _inv(KR_SYM, assets), "event_trigger": "bbo"}}],
                   lambda: parse_kraken, None, 20),
        "deribit": ("wss://www.deribit.com/ws/api/v2",
                    [{"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
                      "params": {"channels": [f"quote.{s}" for s in _inv(DB_SYM, assets)]}}],
                    lambda: parse_deribit, None, 20),
        "hl": ("wss://api.hyperliquid.xyz/ws",
               [{"method": "subscribe", "subscription": {"type": "bbo", "coin": s}} for s in _inv(HL_SYM, assets)],
               lambda: parse_hyperliquid, ('{"method":"ping"}', 30), None),
    }


EVERY_CHANGE = ("pm", "cl")      # these sources keep every change (Polymarket book tops, Chainlink prints)


class Writer:
    """Buffered hourly CSV files in `out_dir` (YYYYmmdd-HH.csv by the UTC hour of rx); a finished hour is gzipped."""

    def __init__(self, out_dir, min_bp=0.2, heartbeat_ms=1000.0):
        self.dir, self.min_bp, self.heartbeat_ms = out_dir, min_bp, heartbeat_ms
        os.makedirs(out_dir, exist_ok=True)
        self.buf, self.last, self.hour, self.f = [], {}, None, None
        self.rows = 0
        self.need_compress = True        # gzip the finished hours off the event loop (the runner calls compress_old)

    def add(self, rx, src, asset, bid, ask, ts=None, extra=""):
        if bid is None or ask is None:
            return False
        k = (src, asset)
        prev = self.last.get(k)
        if prev is not None:
            if prev[2] == bid and prev[3] == ask:
                return False
            if src not in EVERY_CHANGE and rx - prev[1] < self.heartbeat_ms and \
                    abs(math.log((bid + ask) / prev[0])) * 1e4 < self.min_bp:
                return False
        self.last[k] = (bid + ask, rx, bid, ask)
        self.buf.append(f"{rx:.1f},{src},{asset},{bid:.10g},{ask:.10g},{'' if ts is None else f'{ts:.0f}'},{extra}\n")
        return True

    def mark(self, rx, src, asset="", value="", extra=""):
        """A row outside the price filter: a loop stall ("lag", value = ms) or a connection event ("sys")."""
        self.buf.append(f"{rx:.1f},{src},{asset},{value},,,{str(extra).replace(',', ';')[:120]}\n")

    def flush(self, now_ms=None):
        if not self.buf:
            return 0
        hour = time.strftime("%Y%m%d-%H", time.gmtime((now_ms or time.time() * 1000) / 1000))
        if hour != self.hour:
            self._rotate(hour)
        n = len(self.buf)
        self.f.write("".join(self.buf))
        self.f.flush()
        self.rows += n
        self.buf = []
        return n

    def _rotate(self, hour):
        if self.f is not None:
            self.f.close()
        self.hour = hour
        self.f = open(os.path.join(self.dir, f"{hour}.csv"), "a")
        self.need_compress = True

    def compress_old(self, current=None, everything=False):
        """gzip every plain hour file but the current one (crash-safe: the open hour stays plain CSV); everything=True at the
        end of the run. Runs in a thread."""
        current = current or self.hour
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".csv") and (everything or name != f"{current}.csv"):
                src = os.path.join(self.dir, name)
                with open(src, "rb") as fi, gzip.open(src + ".gz", "wb", compresslevel=6) as fo:
                    shutil.copyfileobj(fi, fo)
                os.remove(src)

    def close(self):
        self.flush()
        if self.f is not None:
            self.f.close()
            self.f = None
