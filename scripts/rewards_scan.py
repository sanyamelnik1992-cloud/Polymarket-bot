"""Liquidity-rewards farming estimate: what share of each market's daily reward pool a small two-sided quote would earn.

Polymarket pays makers daily from per-market pools (docs: Programs -> Liquidity Rewards). Per one-minute sample
an order scores S = ((v - s) / v)^2 * size, v = max qualifying spread (cents), s = distance from the
size-cutoff-adjusted midpoint. Side one = YES bids + NO asks, side two = YES asks + NO bids; a maker scores
min(side one, side two), or max(...)/3 single-sided when the midpoint is in [0.10, 0.90]. Rewards are split
by the share of the total score.

For each rewarded market we read the live book (the NO book is its mirror), add a hypothetical two-sided quote
of `size` shares `dist` cents around the displayed price (YES bid + NO bid: needs pUSD, no inventory to start),
recompute the size-cutoff-adjusted midpoint, and estimate the competitors' total score D from the aggregate book
(an approximation: it treats all other makers as one):
    our reward/day = rate * Q / (D + Q)
Risk proxy: size * max(0, |1-day price change| - dist) -- a move through our quote leaves us holding the loser.
Markets ending within 2 days or already past their end date (outcome often known) are dropped.

usage: python scripts/rewards_scan.py OUT_CSV [top=600] [--size=0 (=min size)] [--dist=1.0]
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.http import get_json  # noqa: E402

CLOB = "https://clob.polymarket.com"


def reward_markets():
    rows, cur = [], None
    for _ in range(100):
        p = {"order_by": "rate_per_day", "position": "DESC", "page_size": 500}
        if cur:
            p["next_cursor"] = cur
        r = get_json(f"{CLOB}/rewards/markets/multi", p, timeout=90)
        d = r.get("data", [])
        rows += d
        cur = r.get("next_cursor")
        if not d or cur in (None, "LTE="):
            break
    return rows


def rate(m):
    return sum(float(c.get("rate_per_day") or 0) for c in (m.get("rewards_config") or []))


def books(token_ids):
    out = {}
    s = requests.Session()
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        for k in range(5):
            try:
                r = s.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk], timeout=60)
                if r.status_code == 200:
                    for b in r.json():
                        out[b["asset_id"]] = b
                    break
            except requests.RequestException:
                pass
            time.sleep(1 + k)
    return out


def levels(b, side):
    xs = [(float(x["price"]), float(x["size"])) for x in (b.get(side) or [])]
    return sorted(xs, reverse=(side == "bids"))


def adj_price(lv, min_size):
    cum = 0.0
    for p, s in lv:
        cum += s
        if cum >= min_size:
            return p
    return None


def side_score(lv, mid, v_c):
    q = 0.0
    for p, s in lv:
        d = abs(p - mid) * 100
        if d < v_c:
            q += ((v_c - d) / v_c) ** 2 * s
    return q


def main():
    out = sys.argv[1]
    pos = [a for a in sys.argv[2:] if not a.startswith("--")]
    top = int(pos[0]) if pos else 600
    size_over = float(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--size=")), 0))
    dist = float(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--dist=")), 1.0))
    ms = [m for m in reward_markets() if rate(m) > 0 and len(m.get("tokens") or []) == 2]
    print(f"rewarded markets: {len(ms)}, pools total ${sum(rate(m) for m in ms):,.0f}/day")
    ms = sorted(ms, key=rate, reverse=True)[:top]
    bk = books([m["tokens"][0]["token_id"] for m in ms])
    now = datetime.now(timezone.utc)
    rows = []
    for m in ms:
        t_yes = m["tokens"][0]
        by = bk.get(t_yes["token_id"])
        if not by:
            continue
        # the NO book is the mirror of the YES book (NO bid q == YES ask 1-q), so the YES book holds every order once
        v = float(m.get("rewards_max_spread") or 0)
        mn = float(m.get("rewards_min_size") or 0)
        tick = float(by.get("tick_size") or 0.01)
        size = max(mn, size_over, float(by.get("min_order_size") or 0))
        yb, ya = levels(by, "bids"), levels(by, "asks")
        fair = float(t_yes.get("price") or 0)          # displayed price (mid, or last trade when the book is wide)
        if not 0 < fair < 1 or v <= dist:
            continue
        b = np.floor((fair - dist / 100) / tick + 1e-9) * tick
        a = np.ceil((fair + dist / 100) / tick - 1e-9) * tick
        if b <= 0 or a >= 1:
            continue
        # our YES bid at b and NO bid at 1-a (shows as a YES ask at a); never cross the existing book
        if (ya and b >= ya[0][0]) or (yb and a <= yb[0][0]):
            continue
        bids = sorted(yb + [(b, size)], reverse=True)
        asks = sorted(ya + [(a, size)])
        bid_adj, ask_adj = adj_price(bids, mn), adj_price(asks, mn)
        if bid_adj is None or ask_adj is None:
            continue
        mid = (bid_adj + ask_adj) / 2
        q1, q2 = side_score(yb, mid, v), side_score(ya, mid, v)
        two_only = not (0.10 <= mid <= 0.90)
        comp = min(q1, q2) if two_only else max(min(q1, q2), max(q1, q2) / 3)
        s1 = side_score([(b, size)], mid, v)
        s2 = side_score([(a, size)], mid, v)
        q_ours = min(s1, s2) if two_only else max(min(s1, s2), max(s1, s2) / 3)
        if q_ours <= 0:
            continue
        share = q_ours / (comp + q_ours)
        rew = rate(m) * share
        cap = size * b + size * (1 - a)
        end = m.get("end_date")
        try:
            days_left = (datetime.fromisoformat(end.replace("Z", "+00:00").replace(" ", "T")) - now).total_seconds() / 86400
        except (AttributeError, ValueError):
            days_left = np.nan
        chg = abs(float(m.get("one_day_price_change") or 0))
        rows.append({"question": (m.get("question") or "")[:90], "slug": m.get("market_slug"), "rate": rate(m),
                     "v_c": v, "size": size, "fair": fair, "mid": round(mid, 3),
                     "book_spread": (ya[0][0] - yb[0][0]) if ya and yb else np.nan,
                     "comp_Q": round(comp, 1), "our_Q": round(q_ours, 1), "share": share, "reward_day": rew,
                     "capital": cap, "yield_day": rew / cap if cap else np.nan, "chg_1d": chg,
                     "move_loss_day": size * max(0.0, chg - dist / 100),
                     "vol_24h": float(m.get("volume_24hr") or 0), "days_left": days_left})
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 60)
    print(f"priced {len(df)} markets (quote {dist}c from adjusted mid, size = max(min_size, {size_over}))")
    df["net_day"] = df.reward_day - df.move_loss_day
    df["net_yield"] = df.net_day / df.capital
    cols = ["question", "rate", "v_c", "size", "fair", "book_spread", "comp_Q", "share", "reward_day", "capital",
            "chg_1d", "net_day", "net_yield", "days_left"]
    df = df[(df.days_left > 2)]                        # skip markets about to end or already awaiting resolution
    print(f"after dropping markets ending within 2 days / awaiting resolution: {len(df)}")
    print("\nTop by daily net yield on capital:")
    print(df.sort_values("net_yield", ascending=False)[cols].head(30).round(3).to_string(index=False))
    calm = df[(df.chg_1d <= 0.02) & (df.days_left > 7)]
    print(f"\nCalm (|1d change| <= 2c) and > 7 days to resolution: {len(calm)} markets")
    print(calm.sort_values("net_yield", ascending=False)[cols].head(30).round(3).to_string(index=False))
    for lab, d in (("all priced", df), ("calm", calm)):
        best = d.sort_values("net_yield", ascending=False)
        for budget in (500, 2000, 10000):
            pick = best[best.capital.cumsum() <= budget]
            print(f"{lab:10s} budget ${budget:>6}: {len(pick):3d} markets, reward ${pick.reward_day.sum():8.2f}/day, "
                  f"move-loss proxy ${pick.move_loss_day.sum():7.2f}/day, net ${pick.net_day.sum():8.2f}/day "
                  f"({pick.net_day.sum() / budget:.1%}/day), median share {pick.share.median():.2f}")


if __name__ == "__main__":
    main()
