"""Main loop: discover windows, compute fair value, run filters + risk, keep quotes in sync, settle windows."""
import asyncio
import logging
import time

from ..config import BINANCE_REST, BINANCE_SYMBOL
from ..gamma import fetch_event, parse_event, slug_for, window_start
from ..http import get_json
from .config import BotConfig
from .fair import fair_value
from .feeds import ClobFeed, binance_feed, rtds_feed
from .filters import check, imbalance_blocks, queue_imbalance, side_blocks
from .journal import Journal
from .risk import RiskManager
from .state import State, Window, now_ms
from .strategy import target_quotes

log = logging.getLogger("pmbot.engine")


class Engine:
    def __init__(self, cfg: BotConfig, broker_factory, journal: Journal, duration_s: float | None = None,
                 clock=None):
        self.cfg = cfg
        self.journal = journal
        self.clock = clock or now_ms          # ms; replaced by a simulated clock in replay
        self.st = State(cfg.strategy.assets, cfg.strategy.vol_halflife_s)
        self.risk = RiskManager(cfg.risk, journal, clock=lambda: self.clock() / 1000)
        self.broker = broker_factory(on_fill=self.on_fill, journal=journal)
        self.deadline = time.time() + duration_s if duration_s else None
        self.quotes = {}          # (window key, side) -> {"oid","price","size","t"}
        self.feeds = {a: ClobFeed(self.st, a, on_trade=self.on_public_trade, alive=self.alive) for a in self.st.assets}
        self.to_settle = {}       # window key -> Window (ended, has position)
        self.last_reasons = {}
        self._last_fv_log = {}
        self.stats = {"placed": 0, "cancelled": 0, "fills": 0, "rejected_rate": 0, "fast_wakes": 0,
                      "order_errors": 0, "lag_max_ms": 0, "lag_blocks": 0}
        self._need_reset = False          # an order call failed: cancel_all and forget local quotes first
        self._reset_after = 0             # ms: earliest next cancel_all attempt (backoff)
        self._reset_failures = 0
        self._broker_down = ""            # last broker health reason (journal only on change)
        self._stop = False
        self._wake = asyncio.Event()
        self._decided_mid = {}            # asset -> Binance mid at the last decision
        if cfg.execution.fast_react:
            self.st.on_tick = self._maybe_wake

    def _maybe_wake(self, asset):
        """Wake the decision loop immediately when Binance moved enough since the last decision."""
        px = self.st.prices[asset]
        mid, ref, sig = px.bn_mid(), self._decided_mid.get(asset), px.sigma()
        if mid and ref and sig and abs(mid / ref - 1) > self.cfg.execution.fast_move_sigmas * sig:
            if not self._wake.is_set():
                self.stats["fast_wakes"] += 1
                self._wake.set()

    def alive(self):
        return not self._stop and (self.deadline is None or time.time() < self.deadline)

    # ------------------------------------------------------------ callbacks
    def on_public_trade(self, token, side, price, size, ts_ms, rx):
        w = self.st.token_window.get(token)
        if w is None:
            return
        self.broker.on_public_trade(token, w.other, side, price, size, ts_ms)

    def on_fill(self, fill):
        self.stats["fills"] += 1
        self._log_fill_ctx(fill)
        self.st.record_fill(fill["key"], fill["side"], self.clock(), fill["size"])
        self.risk.on_fill(fill["key"], fill["side"], fill["price"], fill["size"], fill["fee_eq"])
        q = self.quotes.get((fill["key"], fill["side"]))
        if q and q["oid"] == fill["order_id"]:
            q["size"] -= fill["size"]
            if q["size"] <= 1e-9:
                self.quotes.pop((fill["key"], fill["side"]), None)
        w = self.st.windows.get(fill["key"])
        if w:
            self.to_settle[w.key] = w
            # toxic-flow guard reacts on the fill itself, not on the next decision tick
            if fill["side"] in side_blocks(w, self.st, self.clock(), self.cfg.filters):
                cur = self.quotes.get((w.key, fill["side"]))
                if cur:
                    asyncio.get_running_loop().create_task(self._cancel((w.key, fill["side"]), cur, self.clock()))

    def _log_fill_ctx(self, fill):
        """Fair value, both books and Binance at the moment the fill reached us (the 1 Hz fv trace is too coarse)."""
        try:
            w = self.st.windows.get(fill["key"])
            fh = self.st.fair_hist.get(fill["key"])
            ctx = {"key": fill["key"], "order_id": fill["order_id"], "ts": fill["ts"], "side": fill["side"],
                   "price": fill["price"], "p_up": round(fh[-1][1], 4) if fh else None,
                   "p_age_ms": (self.clock() - fh[-1][0]) if fh else None}
            if w:
                for nm, tok in (("up", w.token_up), ("dn", w.token_down)):
                    b = self.st.books.get(tok)
                    ctx[nm + "_bid"] = b.best_bid() if b else None
                    ctx[nm + "_ask"] = b.best_ask() if b else None
                    ctx[nm + "_imb"] = round(queue_imbalance(b), 3) if b and queue_imbalance(b) is not None else None
                ctx["bn"] = self.st.prices[w.asset].bn_mid()
                ctx["rel"] = round(self.clock() / 1000 - w.start, 2)
            self.journal.log("fill_ctx", **ctx)
        except Exception as e:  # noqa: BLE001  (diagnostics must never break fill accounting)
            log.warning("fill_ctx failed: %s", e)

    def _order_error(self, op, e):
        self.stats["order_errors"] += 1
        self._need_reset = True
        self.journal.log("order_error", op=op, err=type(e).__name__, error=str(e)[:300])
        log.warning("order %s failed: %s %s", op, type(e).__name__, str(e)[:200])

    async def _reset_orders(self, now):
        """After a failed place/cancel we do not know what rests on the exchange: cancel everything, forget quotes."""
        if now < self._reset_after:
            return
        self.quotes.clear()
        try:
            await self.broker.cancel_all(now)
        except Exception as e:  # noqa: BLE001
            self._reset_failures += 1
            self._reset_after = now + min(30_000, 1000 * 2 ** min(self._reset_failures, 5))
            self.journal.log("reset_failed", error=str(e)[:200], failures=self._reset_failures)
            if self._reset_failures >= 8:
                raise          # crash: heartbeats stop, the exchange cancels our orders, systemd restarts
            return
        self._need_reset, self._reset_failures = False, 0
        self.journal.log("orders_reset")

    # ------------------------------------------------------------ discovery & strikes
    async def bootstrap_vol(self):
        for a in self.st.assets:
            try:
                end = int(time.time() * 1000)
                k = await asyncio.to_thread(get_json, f"{BINANCE_REST}/klines",
                                            {"symbol": BINANCE_SYMBOL[a], "interval": "1s", "startTime": end - 1800_000,
                                             "limit": 1000})
                k2 = await asyncio.to_thread(get_json, f"{BINANCE_REST}/klines",
                                             {"symbol": BINANCE_SYMBOL[a], "interval": "1s",
                                              "startTime": k[-1][0] + 1000, "limit": 1000}) if k else []
                closes = [float(r[4]) for r in (k + k2)]
                self.st.prices[a].seed_vol(closes)
                self.journal.log("vol_seed", asset=a, n=len(closes), sigma_bp=(self.st.prices[a].sigma() or 0) * 1e4)
            except Exception as e:  # noqa: BLE001
                log.warning("vol seed %s failed: %s", a, e)

    async def discover_loop(self):
        while self.alive():
            ws0 = window_start(time.time())
            for start in (ws0, ws0 + 300):
                for a in self.st.assets:
                    key = f"{a}-{start}"
                    if key in self.st.windows:
                        continue
                    try:
                        e = await asyncio.to_thread(fetch_event, slug_for(a, start))
                    except Exception as ex:  # noqa: BLE001
                        log.warning("discover %s: %s", key, ex)
                        continue
                    if not e:
                        continue
                    m = parse_event(e)
                    mk = e["markets"][0]
                    w = Window(asset=a, start=start, slug=m["slug"], condition_id=m["condition_id"],
                               token_up=m["token_up"], token_down=m["token_down"],
                               tick=float(mk.get("orderPriceMinTickSize") or 0.01),
                               min_size=float(mk.get("orderMinSize") or 5))
                    self.st.add_window(w)
                    self.journal.log("window", key=key, slug=w.slug, cond=w.condition_id)
            for f in self.feeds.values():
                await f.sync()
            await self.update_strikes()
            self.drop_old_windows()
            await asyncio.sleep(3)

    async def update_strikes(self):
        now = time.time()
        for w in list(self.st.windows.values()):
            if w.strike_src == "gamma" or now < w.start:
                continue
            # exact: own priceToBeat or previous window's finalPrice (published after it resolves)
            try:
                for slug, k in ((w.slug, "priceToBeat"), (slug_for(w.asset, w.start - 300), "finalPrice")):
                    e = await asyncio.to_thread(fetch_event, slug)
                    v = ((e or {}).get("eventMetadata") or {}).get(k)
                    if v:
                        if w.strike is not None and abs(v / w.strike - 1) * 1e4 > 1.0:
                            self.journal.log("strike_correction", key=w.key, est=w.strike, exact=v)
                        w.strike, w.strike_src = float(v), "gamma"
                        break
            except Exception as ex:  # noqa: BLE001
                log.warning("strike %s: %s", w.key, ex)
            if w.strike is None:
                v, n = self.st.prices[w.asset].cl_twap(w.start)
                if v and n >= self.cfg.filters.min_strike_seconds:
                    w.strike, w.strike_src = v, "chainlink"
                    self.journal.log("strike_est", key=w.key, strike=v, seconds=n)

    def drop_old_windows(self):
        now = time.time()
        for key, w in list(self.st.windows.items()):
            if now > w.end + 30 and key not in self.to_settle:
                self.st.drop_window(key)

    # ------------------------------------------------------------ quoting
    async def set_quote(self, w, side, tgt, now):
        key = (w.key, side)
        cur = self.quotes.get(key)
        sc = self.cfg.strategy
        if tgt is None:
            if cur:
                await self._cancel(key, cur, now)
            return
        price, size = tgt
        if cur:
            shrink = size < cur["size"] - 1e-9          # limits tightened (fills / budget): resize now
            if abs(cur["price"] - price) < 1e-9 and not shrink:
                return
            moving_down = price < cur["price"] - 1e-9
            big_up = price >= cur["price"] + sc.requote_ticks * 0.01 - 1e-9
            if not (moving_down or shrink) and (not big_up or now - cur["t"] < sc.min_requote_ms):
                return
            if not await self._cancel(key, cur, now):
                return
        resting = sum(q["price"] * q["size"] for q in self.quotes.values())
        size = self.risk.clamp_size(w.key, price, size, resting)
        if size < max(w.min_size, 5):
            return
        if not self.risk.allow_action():
            self.stats["rejected_rate"] += 1
            return
        token = w.token_up if side == "U" else w.token_down
        exp = max(int(now / 1000) + self.cfg.execution.gtd_min_lifetime_s, w.start + int(sc.stop_s) + 60)
        book = self.st.books.get(token)
        queue_ahead = book.bids.get(price, 0.0) if book else 0.0
        try:
            oid = await self.broker.place(token, w.key, side, price, size, now, expiration_s=exp,
                                          queue_ahead=queue_ahead)
        except Exception as e:  # noqa: BLE001
            self._order_error("place", e)
            return
        if oid:
            self.quotes[key] = {"oid": oid, "price": price, "size": size, "t": now}
            self.stats["placed"] += 1
            bb = book.best_bid() if book else None
            self.journal.log("quote", key=w.key, side=side, price=price, size=size, oid=oid, q_ahead=queue_ahead,
                             at_best=(bb is not None and abs(price - bb) < 1e-9),
                             imb=round(queue_imbalance(book), 3) if book and queue_imbalance(book) is not None else None)

    async def _cancel(self, key, cur, now):
        self.risk.allow_action()  # cancels always go through, but consume budget
        self.quotes.pop(key, None)
        self.stats["cancelled"] += 1
        try:
            gone = await self.broker.cancel([cur["oid"]], now)
        except Exception as e:  # noqa: BLE001
            self._order_error("cancel", e)
            return False
        if gone:
            # live: the order was matched while we cancelled it. Its fill reaches us 0.2-1 s later, so position and
            # risk limits do not know it yet, and the market is moving through our price: wait before re-bidding
            k, side = key
            self.st.side_block_until[(k, side)] = max(self.st.side_block_until.get((k, side), 0),
                                                      now + self.cfg.execution.hit_pause_ms)
            self.journal.log("side_hit", key=k, side=side, pause_ms=self.cfg.execution.hit_pause_ms)
            return False
        return True

    async def cancel_everything(self, reason):
        now = self.clock()
        for key, cur in list(self.quotes.items()):
            await self._cancel(key, cur, now)
        try:
            await self.broker.cancel_all(now)
        except Exception as e:  # noqa: BLE001
            self._order_error("cancel_all", e)
        self.journal.log("cancel_everything", reason=reason)

    async def decide_loop(self):
        tick = self.cfg.execution.tick_ms / 1000
        while self.alive():
            t0 = time.time()
            self._wake.clear()
            if not await self.decide_once(self.clock()):
                break
            try:   # sleep until the next tick, or less if a fast Binance move wakes us
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, tick - (time.time() - t0)))
            except asyncio.TimeoutError:
                pass

    async def decide_once(self, now) -> bool:
        """One decision pass over all windows at time `now` (ms). Returns False when the bot must stop."""
        fatal = getattr(self.broker, "fatal", "")
        if fatal:
            self.risk.kill(f"broker: {fatal}", kind="fatal")
        ok, why = self.risk.can_quote(now / 1000)
        if not ok:
            if self.quotes:
                await self.cancel_everything(why)
            if self.risk.killed and not self.risk.stays_alive_when_killed:
                log.error("KILL SWITCH: %s", why)
                self._stop = True
                return False
        if self._need_reset:
            await self._reset_orders(now)
            if self._need_reset:
                return True
        health = getattr(self.broker, "health", None)
        b_ok, b_why = health() if health else (True, "")
        if b_why != self._broker_down:
            self.journal.log("broker_health", ok=b_ok, reason=b_why)
            self._broker_down = b_why
        if not b_ok and ok:
            ok, why = False, b_why
            if self.quotes:
                await self.cancel_everything(why)
        sc = self.cfg.strategy
        for a in self.st.assets:
            self._decided_mid[a] = self.st.prices[a].bn_mid()
        for w in list(self.st.windows.values()):
            if now / 1000 >= w.end:
                for side in ("U", "D"):
                    if (w.key, side) in self.quotes:
                        await self._cancel((w.key, side), self.quotes[(w.key, side)], now)
                continue
            if now / 1000 < w.start:
                continue
            fv = fair_value(w, self.st.prices[w.asset], now, sc.vol_mult, sc.t_df)
            reasons = check(w, self.st, fv, now, self.cfg.filters, sc)
            mid = self._mid(w.token_up)
            if fv is not None:
                self.st.record_fair(w.key, now, fv.p_up)
                sec = now // 1000
                if self._last_fv_log.get(w.key) != sec:      # 1 Hz trace for offline calibration
                    self._last_fv_log[w.key] = sec
                    self.journal.log("fv", key=w.key, rel=round(fv.rel, 1), p=round(fv.p_up, 4),
                                     mid=mid, spot=round(fv.spot, 6),
                                     strike=w.strike, src=w.strike_src, cl_age=fv.cl_age_ms)
            if not ok:
                reasons.append(why)
            prev = self.last_reasons.get(w.key)
            if prev != reasons:
                self.journal.log("filters", key=w.key, reasons=reasons,
                                 p=round(fv.p_up, 4) if fv else None, mid=mid)
                self.last_reasons[w.key] = reasons
            if reasons:
                tg = {"U": None, "D": None}
            else:
                # quote around a blend of the model and the book mid (model_weight=1 -> pure model)
                p_q = fv.p_up if (sc.model_weight >= 1 or mid is None) else \
                    sc.model_weight * fv.p_up + (1 - sc.model_weight) * mid
                pos = self.risk.positions[w.key]
                tg = target_quotes(w, p_q, self.st.books[w.token_up], self.st.books[w.token_down], pos, sc,
                                   self.cfg.risk)
                blocked = side_blocks(w, self.st, now, self.cfg.filters)
                if self.cfg.filters.min_queue_imbalance > 0:
                    own = {}
                    for s_, tok in (("U", w.token_up), ("D", w.token_down)):
                        q = self.quotes.get((w.key, s_))
                        b = self.st.books.get(tok)
                        if q and b and b.best_bid() is not None and abs(q["price"] - b.best_bid()) < 1e-9 \
                                and self.broker.mode == "live":
                            own[s_] = q["size"]          # our live order is part of the public best bid
                    quoting = {s_: (w.key, s_) in self.quotes for s_ in ("U", "D")}
                    blocked = blocked | imbalance_blocks(w, self.st, self.cfg.filters, quoting, own)
                for side in blocked:
                    if tg.get(side) is not None:
                        tg[side] = None
                if blocked != self.last_reasons.get((w.key, "blocked"), set()):
                    self.journal.log("side_block", key=w.key, sides=sorted(blocked))
                    self.last_reasons[(w.key, "blocked")] = blocked
            for side in ("U", "D"):
                await self.set_quote(w, side, tg[side], now)
        self.broker.gc(now)
        return True

    def _mid(self, token):
        b = self.st.books.get(token)
        if b and b.best_bid() is not None and b.best_ask() is not None:
            return round((b.best_bid() + b.best_ask()) / 2, 4)
        return None

    # ------------------------------------------------------------ settlement
    async def settle_loop(self):
        while self.alive() or self.to_settle:
            now = time.time()
            for key, w in list(self.to_settle.items()):
                if now < w.end + 60:
                    continue
                try:
                    e = await asyncio.to_thread(fetch_event, w.slug)
                    m = parse_event(e) if e else None
                except Exception as ex:  # noqa: BLE001
                    log.warning("settle %s: %s", key, ex)
                    continue
                if not m or m["winner"] is None:
                    if now > w.end + 1800:
                        self.journal.log("settle_timeout", key=key)
                        self.to_settle.pop(key, None)
                    continue
                res = self.risk.on_settle(key, m["winner"])
                if res:
                    self.journal.log("settle", key=key, winner=m["winner"], **{k: round(v, 4) for k, v in res.items()})
                    log.info("SETTLE %s %s pnl=%+.2f (up=%.0f down=%.0f cost=%.2f) total=%+.2f", key, m["winner"],
                             res["pnl"], res["up"], res["down"], res["cost"], self.risk.realized_total)
                self.to_settle.pop(key, None)
            if not self.alive() and self.to_settle and now > max(w.end for w in self.to_settle.values()) + 900:
                break
            await asyncio.sleep(10)

    # ------------------------------------------------------------ status
    async def status_loop(self):
        while self.alive():
            await asyncio.sleep(30)
            now = self.clock()
            parts = []
            for w in sorted(self.st.windows.values(), key=lambda x: (x.asset, x.start)):
                if not (w.start <= now / 1000 < w.end):
                    continue
                fv = fair_value(w, self.st.prices[w.asset], now, self.cfg.strategy.vol_mult, self.cfg.strategy.t_df)
                qs = {s: (self.quotes.get((w.key, s)) or {}).get("price") for s in ("U", "D")}
                pos = self.risk.positions.get(w.key)
                parts.append(f"{w.asset} rel={now / 1000 - w.start:5.0f}s p={fv.p_up if fv else float('nan'):.3f} "
                             f"mid={self._mid(w.token_up)} q={qs} pos={(pos.up, pos.down) if pos else (0, 0)} "
                             f"filt={self.last_reasons.get(w.key)}")
            msg = (f"pnl_total={self.risk.realized_total:+.2f} open_worst={self.risk.open_worst_loss():.2f} "
                   f"stats={self.stats} | " + " | ".join(parts))
            log.info(msg)
            self.journal.log("status", pnl=self.risk.realized_total, open_worst=self.risk.open_worst_loss(),
                             stats=dict(self.stats))
            self.stats["lag_max_ms"] = 0          # max event-loop lag per 30 s status interval

    async def lag_loop(self, period_s=0.05):
        """Event-loop lag: a busy/slow process sees the market late (and the exchange drops slow consumers).
        Above filters.max_loop_lag_ms quoting stops for a second (filter 'loop_lag')."""
        lim = self.cfg.filters.max_loop_lag_ms
        while self.alive():
            t = time.perf_counter()
            await asyncio.sleep(period_s)
            lag = (time.perf_counter() - t - period_s) * 1000
            if lag > self.stats["lag_max_ms"]:
                self.stats["lag_max_ms"] = int(lag)
            if lim and lag > lim:
                self.stats["lag_blocks"] += 1
                self.st.lag_block_until = now_ms() + 1000

    # ------------------------------------------------------------ run
    async def load_existing_positions(self):
        """Live restart safety: count positions the wallet already holds in 5m windows."""
        if not hasattr(self.broker, "existing_positions"):
            return
        rows = await asyncio.to_thread(self.broker.existing_positions, self.st.assets)
        for asset, start, side, shares, avg, slug in rows:
            if shares <= 0:
                continue
            if self.risk.is_settled(f"{asset}-{start}"):
                continue          # resolved and already counted; losing tokens stay in the wallet forever
            e = await asyncio.to_thread(fetch_event, slug)
            if not e:
                continue
            m = parse_event(e)
            w = self.st.add_window(Window(asset=asset, start=start, slug=slug, condition_id=m["condition_id"],
                                          token_up=m["token_up"], token_down=m["token_down"]))
            self.risk.on_fill(w.key, side, avg, shares, 0.0)
            self.to_settle[w.key] = w
            self.journal.log("existing_position", key=w.key, side=side, shares=shares, avg_price=avg)
        if rows:
            log.warning("loaded %d existing positions; open worst-case loss %.2f", len(rows),
                        self.risk.open_worst_loss())

    async def run(self):
        await self.broker.start()
        bal = getattr(self.broker, "balance_usd", None)
        if bal is not None and bal < self.cfg.risk.max_open_worst_loss:
            # never commit more than the wallet holds (after losses the bot keeps working with what is left)
            self.journal.log("risk_capped_by_balance", balance=bal, was=self.cfg.risk.max_open_worst_loss)
            self.cfg.risk.max_open_worst_loss = round(bal * 0.95, 2)
        self.journal.log("start", mode=self.broker.mode, config=_cfg_dict(self.cfg))
        await self.load_existing_positions()
        await self.bootstrap_vol()
        tasks = [asyncio.create_task(c) for c in (
            self.discover_loop(), rtds_feed(self.st, self.alive), binance_feed(self.st, self.alive),
            self.decide_loop(), self.status_loop(), self.lag_loop())]
        tasks += [asyncio.create_task(f.run()) for f in self.feeds.values()]
        settle = asyncio.create_task(self.settle_loop())
        try:
            while self.alive():
                await asyncio.sleep(1)
                dead = [t for t in tasks if t.done() and t.exception()]
                if dead:
                    raise dead[0].exception()
        finally:
            self._stop = True if self.risk.killed else self._stop
            await self.cancel_everything("shutdown")
            await self.broker.stop()
            for t in tasks:
                t.cancel()
            self.journal.log("stopped", pnl=self.risk.realized_total, stats=dict(self.stats))
            log.info("stopped; waiting for settlements of %d windows", len(self.to_settle))
            try:
                await asyncio.wait_for(settle, timeout=1200)
            except asyncio.TimeoutError:
                settle.cancel()
            self.journal.log("final", pnl=self.risk.realized_total, rebates=self.risk.rebates_total,
                             unresolved=len(self.risk.unresolved))


def _cfg_dict(cfg):
    from dataclasses import asdict
    return asdict(cfg)
