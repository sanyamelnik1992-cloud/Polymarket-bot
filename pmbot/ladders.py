"""Model-free consistency checks across the markets of one event (any sector).

1. Partition (neg-risk event: exactly one outcome wins):
     buy every YES  if  sum(ask_i) + fees < 1          -> pays exactly 1
     buy every NO   if  sum(bid_i) - fees > 1          -> pays exactly N-1 for cost N - sum(bid_i)
2. Date ladder ("... by <date>?", cumulative):  P(by d1) <= P(by d2) for d1 < d2
     buy YES(d2) + NO(d1) if  ask_yes(d2) + (1 - bid_yes(d1)) + fees < 1   -> pays >= 1
   ("... through <date>?" / "continues" is the mirror: decreasing in date)
3. Strike ladder (BTC "above K" / "hit ↑K": decreasing in K; "dip ↓K" / "below K": increasing in K)
     same two-leg trade between the violating pair.
All prices are best bid/ask of the YES token; NO is bought at 1 - YES bid (books are mirrored).
"""
import json
import re
from dataclasses import dataclass

from .fees import fee_per_share

NUM = re.compile(r"[-+]?\d[\d,]*\.?\d*")


@dataclass
class Leg:
    market_id: str
    question: str
    group: str
    token_yes: str
    token_no: str
    bid: float | None
    ask: float | None
    fee_rate: float
    key: float | None = None      # sort key: strike / date
    end: str = ""


def legs_of(event):
    out = []
    for m in event.get("markets") or []:
        if m.get("closed") or not m.get("active", True) or m.get("acceptingOrders") is False:
            continue
        try:
            toks = json.loads(m["clobTokenIds"])
            outs = json.loads(m["outcomes"])
        except (KeyError, ValueError, TypeError):
            continue
        if len(toks) != 2:
            continue
        yi = outs.index("Yes") if "Yes" in outs else 0
        fee = (m.get("feeSchedule") or {}).get("rate") or 0.0
        bid = m.get("bestBid")
        ask = m.get("bestAsk")
        out.append(Leg(str(m.get("id")), m.get("question", ""), m.get("groupItemTitle") or "", toks[yi], toks[1 - yi],
                       float(bid) if bid not in (None, "") else None, float(ask) if ask not in (None, "") else None,
                       float(fee), end=m.get("endDate") or ""))
    return out


def partition_check(event, legs, min_edge=0.0):
    """Only for neg-risk events where every listed outcome is tradable (no hidden 'other' placeholder)."""
    if not event.get("negRisk") or event.get("negRiskAugmented") or len(legs) < 2:
        return []
    if len(legs) != len(event.get("markets") or []):
        return []  # some outcomes closed / not tradable -> set may not be exhaustive among tradable legs
    res = []
    asks = [lg.ask for lg in legs]
    bids = [lg.bid for lg in legs]
    if all(a is not None and 0 < a < 1 for a in asks):
        cost = sum(a + fee_per_share(a, lg.fee_rate) for a, lg in zip(asks, legs))
        if 1 - cost > min_edge:
            res.append({"type": "partition_buy_yes", "edge_per_set": 1 - cost, "legs": len(legs),
                        "sum_ask": sum(asks)})
    if all(b is not None and 0 < b < 1 for b in bids):
        n = len(legs)
        cost = sum((1 - b) + fee_per_share(1 - b, lg.fee_rate) for b, lg in zip(bids, legs))
        if (n - 1) - cost > min_edge:
            res.append({"type": "partition_buy_no", "edge_per_set": (n - 1) - cost, "legs": n, "sum_bid": sum(bids)})
    return res


def _pair_violations(sorted_legs, increasing, kind, min_edge):
    """For a ladder whose YES prob must be monotone in key, find buy-YES(high side)+buy-NO(low side) arbs."""
    res = []
    n = len(sorted_legs)
    for i in range(n):
        for j in range(i + 1, n):
            lo, hi = sorted_legs[i], sorted_legs[j]          # lo.key < hi.key
            must_ge, must_le = (hi, lo) if increasing else (lo, hi)   # P(must_ge) >= P(must_le)
            a, b = must_ge.ask, must_le.bid
            if a is None or b is None or not (0 < a < 1 and 0 < b < 1):
                continue
            cost = a + fee_per_share(a, must_ge.fee_rate) + (1 - b) + fee_per_share(1 - b, must_le.fee_rate)
            if 1 - cost > min_edge:
                res.append({"type": kind, "edge_per_set": 1 - cost, "buy_yes": must_ge.question,
                            "buy_no": must_le.question, "yes_ask": a, "no_price": 1 - b})
    return res


def parse_strike(text):
    """Strike from a groupItemTitle like '↑ $400', '$785', '100,000', '↓ 70k'."""
    t = text.replace("$", "").strip()
    m = NUM.search(t)
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    if re.search(r"\d\s*[kK]\b", t):
        v *= 1000
    return v


PRICE_EVENT = re.compile(r"what price|price of|closes? (above|below)|\babove\b|\bbelow\b|\bhit\b|\breach\b|\bdip\b")
SPORTS_TAGS = {"sports", "esports", "soccer", "basketball", "football", "tennis", "games"}


def direction(lg: Leg):
    g, q = lg.group, lg.question.lower()
    if "↓" in g or "(low)" in q or re.search(r"\bdip\b|\bbelow\b|\bunder\b|\bfall to\b", q):
        return "down"
    if "↑" in g or "(high)" in q or re.search(r"\babove\b|\breach\b|\bhit\b|\bexceed", q):
        return "up"
    return None


def ladder_checks(event, legs, min_edge=0.0):
    title = (event.get("title") or "").lower()
    tags = {t.get("slug") for t in (event.get("tags") or [])}
    res = []
    if len(legs) < 2 or event.get("negRisk") or tags & SPORTS_TAGS:
        return res
    # ---- date ladders: one question asked for several deadlines
    ends = {lg.end for lg in legs}
    if len(ends) == len(legs) and all(ends):
        if re.search(r"\bby\b|\bbefore\b", title):
            res += _pair_violations(sorted(legs, key=lambda x: x.end), True, "date_ladder_by", min_edge)
        elif re.search(r"through|continue|still", title):
            res += _pair_violations(sorted(legs, key=lambda x: x.end), False, "date_ladder_through", min_edge)
    # ---- strike ladders: same underlying and deadline, strikes from groupItemTitle
    if len(ends) == 1 and PRICE_EVENT.search(title):
        ups, downs = [], []
        for lg in legs:
            k = parse_strike(lg.group)
            d = direction(lg)
            if k is None or d is None:
                continue
            lg.key = k
            (ups if d == "up" else downs).append(lg)
        if len(ups) >= 2:   # P(reach/above K) decreases with K
            res += _pair_violations(sorted(ups, key=lambda x: x.key), False, "strike_ladder_up", min_edge)
        if len(downs) >= 2:  # P(dip to/below K) increases with K
            res += _pair_violations(sorted(downs, key=lambda x: x.key), True, "strike_ladder_down", min_edge)
    return res
