"""Nested 5m/15m arbitrage TRADER (paper by default, live with --live): buys the two legs of a set that pays >= $1 in
every state for less than $1 including taker fees. Discovery, books and strikes come from pmbot.arb_scanner.

Why it is (almost) riskless: in the last 5 minutes of a 15m window [T, T+900] the 15m market and the 5m market
[T+600, T+900] resolve on the same Chainlink TWAP-60s value C at the same second (identical rules, checked on the
Gamma descriptions 8 Nov 2026). With strikes O15 <= O5, {C >= O5} is inside {C >= O15}: 15m Up + 5m Down pays 1, or
2 when O15 <= C < O5. Mirror for O15 > O5.

Execution (the only real risk is legging):
  1. only with PUBLISHED strikes (never estimated), only when the set is >= min_edge below $1 after fees
  2. leg A (thinner book) first: FAK buy with a price limit
  3. leg B: FAK buy of the same share count with limit = 1 - price_A - fees - margin, i.e. any fill of B inside the
     limit is still a locked profit; retried until 10 s before the end while asks exist inside the limit
  4. a leg that cannot be completed is reported (Telegram) and held to resolution (it is a normal position)
Limits: $ per set trade, $ per day, one trade per pair, kill file STOP.

  python -m pmbot.arb_live OUT_DIR [--live] [--minutes N] [--min-edge 0.02] [--usd-per-pair 5] [--usd-per-day 20]
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time

from .arb_scanner import ArbScanner, walk
from .fees import fee_per_share
from .gamma import fetch_event, parse_event, slug_for

KILL_FILE = os.path.join(os.path.dirname(__file__), "..", "STOP")
MIN_SHARES = 5.0


def leg_b_limit(price_a, margin):
    """Highest price of leg B that still locks >= margin per set after both taker fees (tick 0.01, rounded down)."""
    best = 0.0
    p = 0.01
    while p < 0.995:
        if price_a + fee_per_share(price_a) + p + fee_per_share(p) <= 1 - margin + 1e-9:
            best = p
        p = round(p + 0.01, 2)
    return best


class PaperExec:
    """Fills a FAK buy against the current ask ladder of the scanner's book (no market impact model)."""
    mode = "paper"

    def __init__(self, scanner):
        self.s = scanner

    async def start(self):
        return True

    async def buy(self, token, shares, limit):
        got, spent = 0.0, 0.0
        for px, sz in self.s.asks(token):
            if px > limit + 1e-9 or got >= shares - 1e-9:
                break
            q = min(sz, shares - got)
            got += q
            spent += q * px
        return (spent / got if got else 0.0), got


class LiveExec:
    mode = "live"

    def __init__(self, max_exposure_usd, journal):
        from .bot.live import TakerLiveBroker
        self.b = TakerLiveBroker(max_exposure_usd, journal=journal)

    async def start(self):
        return await self.b.start()

    async def buy(self, token, shares, limit):
        r = await self.b.place_fak(token, "arb", "BUY", limit, round(shares * limit, 2))
        return (r["price"], r["size"]) if r else (0.0, 0.0)


class _J:
    """Minimal journal adapter for TakerLiveBroker.log + Telegram alerts."""
    def __init__(self, f, alerts):
        self.f, self.alerts = f, alerts

    def log(self, event, **kw):
        rec = {"t": round(time.time(), 3), "type": event, **kw}
        self.f.write(json.dumps(rec, default=str) + "\n")
        if self.alerts:
            self.alerts(event, rec)


class ArbTrader(ArbScanner):
    def __init__(self, out_dir, minutes, min_edge, usd_per_pair, usd_per_day, live=False, alerts=None):
        super().__init__(out_dir, minutes, min_edge=min_edge)
        self.trades = open(os.path.join(out_dir, "arb_trades.jsonl"), "a", buffering=1)
        self.j = _J(self.trades, alerts)
        self.usd_per_pair, self.usd_per_day = usd_per_pair, usd_per_day
        self.exec = LiveExec(usd_per_pair, self.j) if live else PaperExec(self)
        self.done = set()          # (asset, T15) traded or being traded
        self.spent_day = {}        # utc day -> $
        self.open_sets = {}        # (asset, T15) -> record for settlement

    def tlog(self, event, **kw):
        self.j.log(event, mode=self.exec.mode, **kw)

    def day(self):
        return time.strftime("%Y-%m-%d", time.gmtime())

    def check(self):
        super().check()                     # scanner logging (opportunities, best per pair)
        if os.path.exists(KILL_FILE):
            return
        now = time.time()
        for p in self.active_pairs():
            key = (p["asset"], p["T15"])
            if key in self.done or p["o15"] is None or p["o5"] is None or p.get("est"):
                continue
            if now > p["T15"] + 880:        # too close to the end to complete a second leg
                continue
            if p["o15"] <= p["o5"]:
                legs = (p["m15"]["token_up"], p["m5"]["token_down"], "15U+5D")
            else:
                legs = (p["m15"]["token_down"], p["m5"]["token_up"], "15D+5U")
            a1, a2 = self.asks(legs[0]), self.asks(legs[1])
            if not a1 or not a2:
                continue
            shares, cost, edge = walk(a1, a2, self.min_edge)
            budget = min(self.usd_per_pair, self.usd_per_day - self.spent_day.get(self.day(), 0.0))
            n = math.floor(min(shares, budget / max(cost / shares, 1e-9) if shares else 0))
            if n < MIN_SHARES:
                continue
            self.done.add(key)
            asyncio.get_running_loop().create_task(self.execute(p, legs, n, edge))

    async def execute(self, p, legs, n, edge):
        key = (p["asset"], p["T15"])
        tok1, tok2, kind = legs
        a1, a2 = self.asks(tok1), self.asks(tok2)
        # thinner best level first: it is the one most likely to vanish
        first, second = (tok1, tok2) if a1[0][1] <= a2[0][1] else (tok2, tok1)
        lim_a = max(px for px, _ in self.asks(first)[:3])          # up to 3 levels of slippage on leg A ...
        lim_a = min(lim_a, 1 - self.min_edge - self.asks(second)[0][0] - fee_per_share(self.asks(second)[0][0])
                    - fee_per_share(lim_a))                           # ... while the set is still >= min_edge
        lim_a = math.floor(lim_a * 100 + 1e-9) / 100
        self.tlog("arb_start", asset=p["asset"], T15=p["T15"], kind=kind, shares=n, edge=round(edge, 4),
                  first=first, lim_a=lim_a, o15=p["o15"], o5=p["o5"])
        try:
            pa, sa = await self.exec.buy(first, n, lim_a)
        except Exception as e:  # noqa: BLE001
            self.tlog("arb_error", leg="A", error=str(e)[:300])
            return
        if sa < 1e-6:
            self.tlog("arb_leg_a_unfilled", asset=p["asset"], T15=p["T15"])
            self.done.discard(key)          # nothing bought: the pair may be tried again
            return
        lim_b = leg_b_limit(pa, self.min_edge / 2)                  # half the edge may go to leg-B slippage
        got_b, spent_b = 0.0, 0.0
        while got_b < sa - 0.01 and time.time() < p["T15"] + 890:
            try:
                pb, sb = await self.exec.buy(second, sa - got_b, lim_b)
            except Exception as e:  # noqa: BLE001
                self.tlog("arb_error", leg="B", error=str(e)[:300])
                pb, sb = 0.0, 0.0
            got_b += sb
            spent_b += pb * sb
            if got_b < sa - 0.01:
                await asyncio.sleep(0.5)
        spent = pa * sa + fee_per_share(pa) * sa + spent_b + (fee_per_share(spent_b / got_b) * got_b if got_b else 0)
        self.spent_day[self.day()] = self.spent_day.get(self.day(), 0.0) + spent
        rec = {"asset": p["asset"], "T15": p["T15"], "kind": kind, "first": first, "second": second,
               "a_price": round(pa, 4), "a_shares": round(sa, 4), "b_price": round(spent_b / got_b, 4) if got_b else None,
               "b_shares": round(got_b, 4), "spent": round(spent, 4), "sets": round(min(sa, got_b), 4),
               "legged": got_b < sa - 0.01}
        self.open_sets[key] = {**rec, "p": p}
        self.tlog("arb_filled" if not rec["legged"] else "arb_legged", **rec)

    async def settle_old(self):
        await super().settle_old()
        now = time.time()
        for key, r in list(self.open_sets.items()):
            p = r["p"]
            if now < p["T15"] + 900 + 120:
                continue
            e5 = await asyncio.to_thread(fetch_event, slug_for(p["asset"], p["T15"] + 600, "5m"))
            e15 = await asyncio.to_thread(fetch_event, slug_for(p["asset"], p["T15"], "15m"))
            w5, w15 = parse_event(e5)["winner"] if e5 else None, parse_event(e15)["winner"] if e15 else None
            if not (w5 and w15):
                continue
            want15, want5 = ("Up", "Down") if r["kind"] == "15U+5D" else ("Down", "Up")
            tok15 = p["m15"]["token_up"] if want15 == "Up" else p["m15"]["token_down"]
            sh15 = r["a_shares"] if r["first"] == tok15 else r["b_shares"]
            sh5 = r["b_shares"] if r["first"] == tok15 else r["a_shares"]
            payoff = sh15 * (w15 == want15) + sh5 * (w5 == want5)
            self.tlog("arb_settle", asset=p["asset"], T15=p["T15"], kind=r["kind"], w15=w15, w5=w5,
                      payoff=round(payoff, 4), spent=r["spent"], pnl=round(payoff - r["spent"], 4))
            self.open_sets.pop(key, None)

    async def run(self):
        await self.exec.start()
        self.tlog("arb_trader_start", min_edge=self.min_edge, usd_per_pair=self.usd_per_pair,
                  usd_per_day=self.usd_per_day)
        await super().run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--minutes", type=float, default=10 ** 7)
    ap.add_argument("--min-edge", type=float, default=0.02)
    ap.add_argument("--usd-per-pair", type=float, default=5)
    ap.add_argument("--usd-per-day", type=float, default=20)
    a = ap.parse_args()
    alerts = None
    try:
        from .bot.alerts import send_html
        tok, chat = os.environ.get("PMBOT_TG_TOKEN", "").strip(), os.environ.get("PMBOT_TG_CHAT", "").strip()
        if tok and chat:
            import threading

            def alerts(kind, rec):
                if kind in ("arb_filled", "arb_legged", "arb_settle", "arb_error"):
                    threading.Thread(target=send_html, daemon=True, args=(tok, chat, f"⚖️ <b>арбитраж 5m/15m</b> {'💰' if a.live else '(paper)'} {kind}: "
                                         f"{rec.get('asset')} {rec.get('kind', '')} "
                                         f"{'PnL ' + format(rec['pnl'], '+.2f') + '$' if 'pnl' in rec else ''}")).start()
    except Exception:  # noqa: BLE001
        alerts = None
    asyncio.run(ArbTrader(a.out, a.minutes, a.min_edge, a.usd_per_pair, a.usd_per_day, live=a.live,
                          alerts=alerts).run())


if __name__ == "__main__":
    sys.exit(main())
