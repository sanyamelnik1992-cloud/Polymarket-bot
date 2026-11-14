"""Live probe of the liquidity rewards (reports/68 section 8, step 2; result of the first run: reports/72): does Polymarket pay a lone, minimum-size
two-sided quote in an (almost) empty pool? REAL MONEY in `run` mode only.

  python reports/72_tools/rewards_probe.py plan   [--budget 20]                 read-only: the market the probe would quote, prices, balance
  python reports/72_tools/rewards_probe.py run    [--budget 20] [--minutes 10] --real-money
  python reports/72_tools/rewards_probe.py payout [--date 2026-11-14]           read-only: what the exchange counted / paid for that day

`run` rests ONE pair of post-only bids (YES at ref - d, NO at 1 - (ref + d), the farmer's own prices and size) in one market
picked by the paper farmer's selection among the markets where nobody else scores, for `--minutes`, and reads the exchange's
own counters every 30 s (order scoring, our reward percentage). Safety: both orders carry an exchange-side expiration (they
die by themselves if this process does), the pair costs at most `--budget`, at least FREE_USD stays free for the lock bots,
a fill cancels the other order and ends the probe (the position is kept and reported: no hedge code here), the kill file
STOP ends it, the orders are cancelled and the open-order list is checked at the end. Needs PMBOT_LIVE_CONFIRM like every
live path. Journal: data/journal-rewards-probe/<run>/events.jsonl.
"""
import argparse
import asyncio
import dataclasses
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.bot.journal import Journal  # noqa: E402
from pmbot.bot.live import check_env, check_geoblock  # noqa: E402
from pmbot.rewards import markets as mk  # noqa: E402
from pmbot.rewards.config import load_rewards_config  # noqa: E402
from pmbot.rewards.scoring import order_score, plan_quotes  # noqa: E402

FREE_USD = 40.0          # reports/68 section 8: the wallet is shared with the live lock bots (5m $10, 15m $5, sniper $5 at risk)
MAX_BUDGET = 25.0        # a probe, not a strategy: larger numbers need new code review, not a flag
MAX_MINUTES = 30.0
TICK_S = 30.0


def quote(rm, rc, book=None):
    """The farmer's quote for rm on the live book: dict(b, no_bid, size, cost, ref, alone, v) or None."""
    raw = book if book is not None else mk.fetch_books([rm.yes]).get(rm.yes)
    if not raw:
        return None
    tick = float(raw.get("tick_size") or 0.01)
    bids, asks = mk.book_levels(raw)
    size = mk.order_size(rc, rm.min_size, float(raw.get("min_order_size") or 5))
    ref = mk.reference_price(bids, asks, rc, rm.v, rm.min_size, rm.last_trade)
    if ref is None or not rc.price_lo <= ref <= rc.price_hi:
        return None
    q1 = sum(order_score(rm.v, abs(p - ref) * 100, s) for p, s in bids)
    q2 = sum(order_score(rm.v, abs(p - ref) * 100, s) for p, s in asks)
    alone = mk.is_alone(rm, q1, q2)
    plan = plan_quotes(ref, mk.quote_dist(rc, rm.v, alone), tick, bids[0][0] if bids else None, asks[0][0] if asks else None,
                       v_cents=rm.v)
    if plan is None:
        return None
    b, a = plan
    return {"b": round(b, 4), "no_bid": round(1 - a, 4), "size": size, "cost": round(size * (b + 1 - a), 2), "ref": round(ref, 4),
            "alone": alone, "v": rm.v, "best_bid": bids[0][0] if bids else None, "best_ask": asks[0][0] if asks else None}


def pick(rc, budget, alone_only=True):
    """The best market of the farmer's own selection that fits the budget; alone_only = nobody else scores there now."""
    rc = dataclasses.replace(rc, quote_budget_usd=1e9, max_per_market_usd=budget, max_markets=40, max_empty_frac=1.0)
    out = []
    for rm in mk.select(rc):
        q = quote(rm, rc)
        if q and q["cost"] <= budget and (q["alone"] or not alone_only):
            out.append((rm, q))
    out.sort(key=lambda x: -x[0].rate)            # the largest pool: the clearest answer in dollars
    return out


async def client_from_env():
    from polymarket import AsyncSecureClient
    return await AsyncSecureClient.create(private_key=os.environ["PMBOT_PRIVATE_KEY"], wallet=os.environ.get("PMBOT_WALLET") or None)


async def open_orders(c):
    out = []
    async for page in c.list_open_orders():
        out.extend(getattr(page, "items", None) or [])
    return out


def show(rm, q):
    return (f"{rm.question[:90]}\n  slug {rm.slug}\n  pool ${rm.rate:.0f}/day, zone {rm.v:.1f}c, min size {rm.min_size:.0f}, "
            f"competitiveness {rm.comp:.3f}, 24h volume ${rm.vol:.0f}\n  price {q['ref']:.3f} (book {q['best_bid']} / {q['best_ask']}), "
            f"alone {q['alone']}\n  YES bid {q['b']:.3f} x {q['size']:.0f} + NO bid {q['no_bid']:.3f} x {q['size']:.0f} = ${q['cost']:.2f}")


async def cmd_plan(args, rc):
    picks = pick(rc, args.budget)
    print(f"markets where nobody else scores and the pair costs <= ${args.budget:.0f}: {len(picks)}")
    for rm, q in picks[:5]:
        print("-", show(rm, q))
    if os.environ.get("PMBOT_PRIVATE_KEY"):
        c = await client_from_env()
        bal = (await c.get_balance_allowance(asset_type="COLLATERAL")).balance / 1e6
        print(f"balance ${bal:.2f}, open orders {len(await open_orders(c))}, reward percentages now {dict(await c.get_reward_percentages())}")


async def cmd_payout(args, rc):
    c = await client_from_env()
    print("total:", await c.get_total_earnings_for_user_for_day(date=args.date))
    async for page in c.list_user_earnings_for_day(date=args.date):
        for e in getattr(page, "items", None) or []:
            print(" ", e.condition_id, e.earnings, e.asset_rate)


async def cmd_run(args, rc):
    if not args.real_money:
        raise SystemExit("run places real orders: add --real-money")
    if not (0 < args.budget <= MAX_BUDGET and 0 < args.minutes <= MAX_MINUTES):
        raise SystemExit(f"budget <= {MAX_BUDGET}, minutes <= {MAX_MINUTES}")
    check_env()
    check_geoblock()
    j = Journal(os.path.join("data/journal-rewards-probe", time.strftime("%Y%m%d-%H%M%S")))
    c = await client_from_env()
    bal = (await c.get_balance_allowance(asset_type="COLLATERAL")).balance / 1e6
    if bal < args.budget + FREE_USD:
        raise SystemExit(f"balance ${bal:.2f} < budget + ${FREE_USD:.0f} kept free for the lock bots")
    if await open_orders(c):
        raise SystemExit("the wallet already has open orders: not touching them")
    picks = pick(rc, args.budget)
    if args.slug:
        picks = [p for p in picks if p[0].slug == args.slug]
    if not picks:
        raise SystemExit("no market fits")
    rm, q = picks[0]
    print(show(rm, q))
    j.log("probe_start", slug=rm.slug, question=rm.question, condition_id=rm.condition_id, rate=rm.rate, comp=rm.comp,
          min_size=rm.min_size, balance=bal, minutes=args.minutes, **q)
    exp = int(time.time() + args.minutes * 60 + 120)       # the exchange drops a GTD order 60 s before its stamp
    ids, filled = {}, {}
    try:
        for name, token, px in (("yes", rm.yes, q["b"]), ("no", rm.no, q["no_bid"])):
            r = await c.place_limit_order(token_id=token, side="BUY", price=f"{px:.3f}".rstrip("0").rstrip("."),
                                          size=f"{q['size']:.2f}", post_only=True, expiration=exp)
            j.log("probe_order", leg=name, price=px, size=q["size"], ok=bool(getattr(r, "ok", False)),
                  order_id=getattr(r, "order_id", None), status=getattr(r, "status", None), code=getattr(r, "code", None),
                  message=str(getattr(r, "message", ""))[:200])
            if not getattr(r, "ok", False):
                raise RuntimeError(f"{name} order refused: {getattr(r, 'code', '?')} {getattr(r, 'message', '')}")
            ids[name] = r.order_id
        t_end = time.time() + args.minutes * 60
        while time.time() < t_end and not os.path.exists("STOP") and not filled:
            await asyncio.sleep(TICK_S)
            row = {"left_s": round(t_end - time.time())}
            try:
                row["scoring"] = dict(await c.get_orders_scoring(order_ids=list(ids.values())))
                row["percent"] = (await c.get_reward_percentages()).get(rm.condition_id)
            except Exception as e:  # noqa: BLE001
                row["counter_error"] = repr(e)[:150]
            for name, oid in ids.items():
                try:
                    o = await c.get_order(order_id=oid)
                    m = float(getattr(o, "size_matched", 0) or 0)
                    row[name] = {"status": str(getattr(o, "status", "")), "matched": m}
                    if m > 0:
                        filled[name] = m
                except Exception as e:  # noqa: BLE001
                    row[name] = {"error": repr(e)[:150]}
            j.log("probe_tick", **row)
            print(row, flush=True)
    finally:
        if ids:
            try:
                r = await c.cancel_orders(order_ids=list(ids.values()))
                j.log("probe_cancel", canceled=list(getattr(r, "canceled", ()) or ()),
                      not_canceled={k: str(v)[:80] for k, v in (getattr(r, "not_canceled", {}) or {}).items()})
            except Exception as e:  # noqa: BLE001
                j.log("probe_cancel_error", error=repr(e)[:200])
            await asyncio.sleep(2)
            left = await open_orders(c)
            if left:                               # belt and braces: nothing of ours may stay on the book
                await c.cancel_orders(order_ids=[o.id for o in left])
            for name, oid in ids.items():
                try:
                    o = await c.get_order(order_id=oid)
                    filled[name] = float(getattr(o, "size_matched", 0) or 0)
                except Exception:  # noqa: BLE001
                    pass
        bal2 = (await c.get_balance_allowance(asset_type="COLLATERAL")).balance / 1e6
        j.log("probe_end", filled=filled, balance=bal2, open_orders=len(await open_orders(c)))
        print(f"done: filled {filled}, balance ${bal:.2f} -> ${bal2:.2f}; payout tomorrow: rewards_probe.py payout --date "
              f"{time.strftime('%Y-%m-%d', time.gmtime())}")
        j.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("plan", "run", "payout"))
    ap.add_argument("--config", default="config/rewards.toml")
    ap.add_argument("--budget", type=float, default=20.0)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--slug", default=None)
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d", time.gmtime()))
    ap.add_argument("--real-money", action="store_true")
    args = ap.parse_args()
    rc = load_rewards_config(args.config)
    asyncio.run({"plan": cmd_plan, "run": cmd_run, "payout": cmd_payout}[args.cmd](args, rc))


if __name__ == "__main__":
    main()
