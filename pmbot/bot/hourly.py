"""Hourly crypto Up/Down taker (BTC/ETH): markets resolve on the Binance 1H candle (close >= open).

Evidence (reports/17-18): fills where a Binance-spot model disagreed with the price by > 8-10c in the last
15-30 minutes of the hour returned ROI +15..40% (market-clustered t 2.4-3.2 at 4s info lag, 1.5-2.5 at 8s).

Rule: during the last `window_s` seconds of the hour, if P_model(token) - best_ask - taker_fee >= min_edge,
buy the token with a FAK limit order at `best_ask` (at most `size_usd` per entry, `max_cost_per_market`
per hour, `cooldown_s` between entries, never both sides of the same hour). Hold to resolution.
"""
import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field, fields

import tomllib
from scipy import stats

from ..config import BINANCE_REST, BINANCE_SYMBOL, GAMMA
from ..fees import fee_per_share
from ..gamma import fetch_event
from ..http import get_json
from .feeds import ClobFeed, binance_feed
from .journal import Journal
from .state import State, Window, now_ms

log = logging.getLogger("pmbot.hourly")
SERIES = {"btc": "btc-up-or-down-hourly", "eth": "eth-up-or-down-hourly"}


@dataclass
class HourlyCfg:
    assets: list = field(default_factory=lambda: ["btc", "eth"])
    window_s: float = 1800.0        # only trade in the last 30 minutes
    min_ttc_s: float = 10.0         # ... but not in the final seconds
    min_edge: float = 0.08          # model prob - ask - fee
    max_price: float = 0.97
    min_price: float = 0.03
    size_usd: float = 10.0          # per entry
    max_cost_per_market: float = 30.0
    max_open_cost: float = 60.0     # all unresolved hours together
    daily_loss_limit: float = 40.0
    cooldown_s: float = 20.0
    vol_mult: float = 1.3
    t_df: float = 4.0
    bn_max_age_ms: int = 3000
    book_max_age_ms: int = 20000
    fill_latency_ms: int = 300      # paper: taker delay 150ms + network
    mode: str = "paper"
    journal_dir: str = "data/journal-hourly"
    kill_file: str = "STOP"
    state_file: str = ""            # daily PnL survives restarts (required in live mode)


def load_hourly_config(path=None, overrides=None):
    cfg = HourlyCfg()
    data = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f).get("hourly", {})
    data.update(overrides or {})
    names = {f.name for f in fields(cfg)}
    for k, v in data.items():
        if k not in names:
            raise ValueError(f"unknown hourly config key: {k}")
        setattr(cfg, k, v)
    if cfg.mode not in ("paper", "live"):
        raise ValueError("hourly.mode must be paper|live")
    if not 0 < cfg.min_edge < 0.5 or cfg.size_usd <= 0 or cfg.max_cost_per_market < cfg.size_usd:
        raise ValueError("invalid hourly limits")
    if cfg.mode == "live" and not cfg.state_file:
        raise ValueError("hourly.state_file is required in live mode")
    return cfg


def prob_up_hourly(spot, open_px, sigma, ttc_s, vol_mult=1.3, df=4.0):
    """P(close >= open) for a driftless log-price, Student-t scaled to unit variance."""
    sd = max(sigma * vol_mult * math.sqrt(max(ttc_s, 1.0)), 1e-9)
    z = math.log(spot / open_px) / sd
    return float(stats.t.cdf(z * math.sqrt(df / (df - 2)), df))


def decide(p_up, book_up, book_down, cfg: HourlyCfg, pos_side=None):
    """Return (side, limit_price, edge) or None. pos_side: side already held this hour (no flipping)."""
    best = None
    for side, p_tok, book in (("U", p_up, book_up), ("D", 1 - p_up, book_down)):
        if pos_side and side != pos_side:
            continue
        ask = book.best_ask()
        if ask is None or not (cfg.min_price <= ask <= cfg.max_price):
            continue
        edge = p_tok - ask - fee_per_share(ask)
        if edge >= cfg.min_edge and (best is None or edge > best[2]):
            best = (side, ask, edge)
    return best


@dataclass
class HourPos:
    side: str | None = None
    shares: float = 0.0
    cost: float = 0.0
    last_entry_ms: int = 0


class HourlyEngine:
    def __init__(self, cfg: HourlyCfg, journal: Journal, broker=None, duration_s=None):
        self.cfg = cfg
        self.journal = journal
        self.st = State(cfg.assets)
        self.broker = broker          # live broker (place_fak) or None for paper
        self.deadline = time.time() + duration_s if duration_s else None
        self.windows = {}             # asset -> current Window (end = start+3600)
        self.opens = {}               # window key -> Binance 1H open
        self.pos = {}                 # window key -> HourPos
        self.realized = 0.0
        self.realized_by_day = {}
        self.pending = []             # paper taker orders waiting for the fill latency
        self.feeds = {a: ClobFeed(self.st, a, alive=self.alive) for a in cfg.assets}
        self._stop = False
        if cfg.state_file and os.path.exists(cfg.state_file):
            try:
                self.realized_by_day.update(json.load(open(cfg.state_file)).get("realized_by_day", {}))
            except (OSError, ValueError):
                pass

    def _save_state(self):
        if not self.cfg.state_file:
            return
        os.makedirs(os.path.dirname(self.cfg.state_file) or ".", exist_ok=True)
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"realized_by_day": self.realized_by_day}, f)
        os.replace(tmp, self.cfg.state_file)

    def alive(self):
        return not self._stop and (self.deadline is None or time.time() < self.deadline)

    # ------------------------------------------------------------ discovery
    async def discover_loop(self):
        while self.alive():
            now = time.time()
            for a in self.cfg.assets:
                w = self.windows.get(a)
                if w and now < w.end:
                    continue
                try:
                    evs = await asyncio.to_thread(get_json, f"{GAMMA}/events", {
                        "series_slug": SERIES[a], "closed": "false", "limit": 5, "order": "startDate",
                        "ascending": "true"}, timeout=30)
                except Exception as e:  # noqa: BLE001
                    log.warning("hourly discover %s: %s", a, e)
                    continue
                for e in evs or []:
                    m = e["markets"][0]
                    st = int(_iso(m["eventStartTime"]))
                    if st <= now < st + 3600:
                        toks, outs = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
                        up = outs.index("Up")
                        w = Window(asset=a, start=st, end=st + 3600, slug=e["slug"], condition_id=m["conditionId"],
                                   token_up=toks[up], token_down=toks[1 - up])
                        self.st.add_window(w)
                        self.windows[a] = w
                        self.journal.log("hour_window", key=w.key, slug=w.slug)
            for f in self.feeds.values():
                await f.sync()
            await self.fetch_opens()
            await asyncio.sleep(10)

    async def fetch_opens(self):
        for a, w in self.windows.items():
            if w.key in self.opens:
                continue
            try:
                k = await asyncio.to_thread(get_json, f"{BINANCE_REST}/klines", {
                    "symbol": BINANCE_SYMBOL[a], "interval": "1h", "startTime": w.start * 1000, "limit": 1})
                if k and int(k[0][0]) == w.start * 1000:
                    self.opens[w.key] = float(k[0][1])
                    self.journal.log("hour_open", key=w.key, open=self.opens[w.key])
            except Exception as e:  # noqa: BLE001
                log.warning("open %s: %s", a, e)

    async def seed_vol(self):
        for a in self.cfg.assets:
            end = int(time.time() * 1000)
            k = await asyncio.to_thread(get_json, f"{BINANCE_REST}/klines", {
                "symbol": BINANCE_SYMBOL[a], "interval": "1s", "startTime": end - 1800_000, "limit": 1000})
            k2 = await asyncio.to_thread(get_json, f"{BINANCE_REST}/klines", {
                "symbol": BINANCE_SYMBOL[a], "interval": "1s", "startTime": k[-1][0] + 1000, "limit": 1000})
            self.st.prices[a].seed_vol([float(r[4]) for r in k + k2])

    # ------------------------------------------------------------ decisions
    def day_pnl(self):
        d = time.strftime("%Y-%m-%d", time.gmtime())
        return self.realized_by_day.get(d, 0.0)

    def open_cost(self):
        return sum(p.cost for k, p in self.pos.items() if not k.endswith("#settled"))

    async def decide_loop(self):
        while self.alive():
            await asyncio.sleep(0.25)
            if self.cfg.kill_file and os.path.exists(self.cfg.kill_file):
                self.journal.log("kill", reason="kill file")
                self._stop = True
                break
            if self.day_pnl() <= -self.cfg.daily_loss_limit:
                self.journal.log("kill", reason="daily loss limit")
                self._stop = True
                break
            now = now_ms()
            self._process_pending(now)
            for a, w in self.windows.items():
                ttc = w.end - now / 1000
                if not (self.cfg.min_ttc_s <= ttc <= self.cfg.window_s):
                    continue
                o = self.opens.get(w.key)
                px = self.st.prices[a]
                if o is None or px.sigma() is None or px.bn_mid() is None:
                    continue
                if now - px.bn_last_rx > self.cfg.bn_max_age_ms:
                    continue
                if now - self.st.feed_alive["clob"].get(a, 0) > self.cfg.book_max_age_ms:
                    continue
                p = prob_up_hourly(px.bn_mid(), o, px.sigma(), ttc, self.cfg.vol_mult, self.cfg.t_df)
                hp = self.pos.setdefault(w.key, HourPos())
                if now - hp.last_entry_ms < self.cfg.cooldown_s * 1000:
                    continue
                if hp.cost + self.cfg.size_usd > self.cfg.max_cost_per_market:
                    continue
                if self.open_cost() + self.cfg.size_usd > self.cfg.max_open_cost:
                    continue
                d = decide(p, self.st.books[w.token_up], self.st.books[w.token_down], self.cfg, hp.side)
                if not d:
                    continue
                side, limit, edge = d
                hp.last_entry_ms = now
                token = w.token_up if side == "U" else w.token_down
                self.journal.log("signal", key=w.key, side=side, p_up=round(p, 4), limit=limit, edge=round(edge, 4),
                                 ttc=round(ttc, 1))
                if self.broker is None:
                    self.pending.append((now + self.cfg.fill_latency_ms, w.key, token, side, limit))
                else:
                    filled = await self.broker.place_fak(token, w.key, side, limit, self.cfg.size_usd)
                    if filled:
                        self._on_fill(w.key, side, filled["price"], filled["size"])

    def _process_pending(self, now):
        keep = []
        for due, key, token, side, limit in self.pending:
            if now < due:
                keep.append((due, key, token, side, limit))
                continue
            book = self.st.books.get(token)
            budget, shares, cost = self.cfg.size_usd, 0.0, 0.0
            for p, s in sorted((book.asks if book else {}).items()):
                if p > limit + 1e-9 or budget <= 0:
                    break
                q = min(s, budget / (p + fee_per_share(p)))
                shares += q
                cost += q * (p + fee_per_share(p))
                budget -= q * (p + fee_per_share(p))
            if shares > 0:
                self._on_fill(key, side, cost / shares, shares, fee_incl=True)
            else:
                self.journal.log("missed", key=key, side=side, limit=limit)
        self.pending = keep

    def _on_fill(self, key, side, avg_px, shares, fee_incl=False):
        hp = self.pos.setdefault(key, HourPos())
        hp.side = side
        hp.shares += shares
        c = avg_px * shares if fee_incl else (avg_px + fee_per_share(avg_px)) * shares
        hp.cost += c
        self.journal.log("fill", key=key, side=side, avg_px=round(avg_px, 4), shares=round(shares, 3), cost=round(c, 4))

    # ------------------------------------------------------------ settlement
    async def settle_loop(self):
        while self.alive() or any(not k.endswith("#settled") and p.shares > 0 for k, p in self.pos.items()):
            now = time.time()
            for key, hp in list(self.pos.items()):
                if key.endswith("#settled") or hp.shares <= 0:
                    continue
                a, start = key.split("-")
                if now < int(start) + 3600 + 15:
                    continue
                winner = await self._binance_winner(a, int(start))      # the resolution rule itself
                src = "binance"
                if winner is None:
                    w = next((x for x in self.st.windows.values() if x.key == key), None)
                    e = await asyncio.to_thread(fetch_event, w.slug) if w else None
                    if not e:
                        continue
                    prices = json.loads(e["markets"][0]["outcomePrices"])
                    outs = json.loads(e["markets"][0]["outcomes"])
                    if float(prices[0]) not in (0.0, 1.0):
                        continue
                    winner, src = ("U" if float(prices[outs.index("Up")]) == 1.0 else "D"), "gamma"
                pnl = (hp.shares if hp.side == winner else 0.0) - hp.cost
                self.realized += pnl
                d = time.strftime("%Y-%m-%d", time.gmtime())
                self.realized_by_day[d] = self.realized_by_day.get(d, 0.0) + pnl
                self._save_state()
                self.journal.log("settle", key=key, winner=winner, src=src, side=hp.side, shares=round(hp.shares, 3),
                                 cost=round(hp.cost, 4), pnl=round(pnl, 4))
                log.info("HOURLY SETTLE %s side=%s winner=%s pnl=%+.2f total=%+.2f", key, hp.side, winner, pnl,
                         self.realized)
                self.pos[key + "#settled"] = self.pos.pop(key)
            if not self.alive() and now > (self.deadline or now) + 3600 * 2:
                break
            await asyncio.sleep(30)

    async def _binance_winner(self, asset, start):
        """Up iff the Binance 1H candle starting at `start` closed >= its open (official resolution rule)."""
        try:
            k = await asyncio.to_thread(get_json, f"{BINANCE_REST}/klines", {
                "symbol": BINANCE_SYMBOL[asset], "interval": "1h", "startTime": start * 1000, "limit": 1})
        except Exception:  # noqa: BLE001
            return None
        if not k or int(k[0][0]) != start * 1000 or int(k[0][6]) > time.time() * 1000:
            return None   # candle not closed yet
        return "U" if float(k[0][4]) >= float(k[0][1]) else "D"

    async def status_loop(self):
        while self.alive():
            await asyncio.sleep(60)
            log.info("hourly realized=%+.2f open_cost=%.2f positions=%s", self.realized, self.open_cost(),
                     {k: (p.side, round(p.shares, 1), round(p.cost, 2)) for k, p in self.pos.items()
                      if not k.endswith("#settled")})

    async def run(self):
        self.journal.log("start", mode=self.cfg.mode, config=self.cfg.__dict__)
        await self.seed_vol()
        tasks = [asyncio.create_task(c) for c in (self.discover_loop(), binance_feed(self.st, self.alive),
                                                  self.decide_loop(), self.status_loop())]
        tasks += [asyncio.create_task(f.run()) for f in self.feeds.values()]
        settle = asyncio.create_task(self.settle_loop())
        try:
            while self.alive():
                await asyncio.sleep(1)
        finally:
            self._stop = True
            for t in tasks:
                t.cancel()
            try:
                await asyncio.wait_for(settle, timeout=2 * 3600)
            except asyncio.TimeoutError:
                settle.cancel()
            self.journal.log("final", pnl=self.realized)


def _iso(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
