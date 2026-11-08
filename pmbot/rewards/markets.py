"""Find rewarded markets and pick the ones worth quoting (REST: CLOB rewards collection + order books)."""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from ..http import get_json
from .scoring import our_share, plan_quotes

CLOB = "https://clob.polymarket.com"


@dataclass
class RMarket:
    slug: str
    question: str
    condition_id: str
    yes: str                 # YES token id
    no: str                  # NO token id
    rate: float              # $ per day for the whole market
    v: float                 # max qualifying spread, cents
    min_size: float          # rewards min size (shares)
    end_ts: float            # market end (unix s), nan if unknown
    tick: float = 0.01
    min_order: float = 5.0
    price: float = 0.5       # displayed price at selection (mid, or last trade when the book is wide)
    est_share: float = 0.0
    est_reward: float = 0.0  # $ per day at selection
    capital: float = 0.0     # pUSD our two bids need
    size: float = 0.0
    empty: bool = False      # nobody else quoted inside the reward zone at selection
    extra: dict = field(default_factory=dict)


def rate_of(m, today=None):
    """Daily pool of a market: only the reward configs that are running today (UTC dates)."""
    today = today or time.strftime("%Y-%m-%d", time.gmtime())
    tot = 0.0
    for c in m.get("rewards_config") or []:
        if (c.get("start_date") or "0000-01-01") <= today <= (c.get("end_date") or "9999-12-31"):
            tot += float(c.get("rate_per_day") or 0)
    return tot


def end_ts(m):
    s = m.get("end_date")
    if not s:
        return float("nan")
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T")).timestamp()
    except ValueError:
        return float("nan")


def fetch_reward_markets(max_pages=4, page_size=500):
    """Rewarded markets, highest daily pool first (the collection is public, no auth)."""
    rows, cur = [], None
    for _ in range(max_pages):
        p = {"order_by": "rate_per_day", "position": "DESC", "page_size": page_size}
        if cur:
            p["next_cursor"] = cur
        r = get_json(f"{CLOB}/rewards/markets/multi", p, timeout=90)
        d = r.get("data") or []
        rows += d
        cur = r.get("next_cursor")
        if not d or cur in (None, "LTE="):
            break
    return rows


def fetch_books(token_ids, session=None):
    """token -> raw book (bids/asks lists of {price,size}, tick_size, min_order_size)."""
    s = session or requests.Session()
    out = {}
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        for k in range(4):
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


def book_levels(raw):
    bids = sorted(((float(x["price"]), float(x["size"])) for x in raw.get("bids") or [] if float(x["size"]) > 0),
                  reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in raw.get("asks") or [] if float(x["size"]) > 0)
    return bids, asks


def candidates(raw_markets, rc, now=None):
    """Filter the rewards collection by the config (pool size, time to resolution, price, excluded topics)."""
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    excl = re.compile(rc.exclude_regex, re.I) if rc.exclude_regex else None
    out, seen = [], set()
    for m in raw_markets:
        toks = m.get("tokens") or []
        if len(toks) != 2 or not m.get("condition_id"):
            continue
        if m["condition_id"] in seen:          # the collection repeats a row at page boundaries
            continue
        seen.add(m["condition_id"])
        rate = rate_of(m, today)
        if rate < rc.min_rate:
            continue
        q = m.get("question") or ""
        if excl and excl.search(q):
            continue
        e = end_ts(m)
        if not (e - now > rc.min_days_left * 86400):          # also drops unknown end dates (nan)
            continue
        price = float(toks[0].get("price") or 0)
        if not rc.price_lo <= price <= rc.price_hi:
            continue
        v = float(m.get("rewards_max_spread") or 0)
        if v <= rc.dist_cents:
            continue
        out.append(RMarket(slug=m.get("market_slug") or m["condition_id"], question=q[:120],
                           condition_id=m["condition_id"], yes=toks[0]["token_id"], no=toks[1]["token_id"],
                           rate=rate, v=v, min_size=float(m.get("rewards_min_size") or 0), end_ts=e, price=price))
    return out


def price_market(rm, raw_book, rc):
    """Fill in tick, size, capital and the estimated reward of quoting rm now. False if it cannot be quoted."""
    if not raw_book:
        return False
    rm.tick = float(raw_book.get("tick_size") or 0.01)
    rm.min_order = float(raw_book.get("min_order_size") or 5)
    bids, asks = book_levels(raw_book)
    plan = plan_quotes(rm.price, rc.dist_cents, rm.tick, bids[0][0] if bids else None, asks[0][0] if asks else None)
    if plan is None:
        return False
    b, a = plan
    size = max(rm.min_size, rm.min_order, 5.0) * rc.size_mult
    cap = size * b + size * (1 - a)
    if cap > rc.max_per_market_usd:
        return False
    share, mid, q_ours, q_comp = our_share(bids, asks, (b, size), (a, size), rm.v, rm.min_size)
    if share <= 0:
        return False
    rm.size, rm.capital, rm.est_share = size, cap, share
    rm.est_reward = rm.rate * share
    rm.empty = q_comp <= 1e-9
    rm.extra = {"mid": mid, "q_comp": round(q_comp, 2), "spread": (asks[0][0] - bids[0][0]) if bids and asks else None}
    return True


def select(rc, journal=None, raw_markets=None, books=None, now=None):
    """Pick markets by estimated reward per $ of capital until the budget or max_markets is reached.
    Empty-book markets (we would be the only quote) are capped at rc.max_empty_frac of the picks."""
    raw_markets = raw_markets if raw_markets is not None else fetch_reward_markets(rc.max_pages)
    if not raw_markets:                        # an empty answer must not look like "no markets": keep the old selection
        raise RuntimeError("empty rewards collection")
    cands = candidates(raw_markets, rc, now)
    cands = sorted(cands, key=lambda x: -x.rate)[: rc.max_candidates]
    books = books if books is not None else fetch_books([c.yes for c in cands])
    priced = [c for c in cands if price_market(c, books.get(c.yes), rc)]
    priced.sort(key=lambda c: -(c.est_reward / c.capital))
    picks, cap, n_empty = [], 0.0, 0
    max_empty = int(rc.max_markets * rc.max_empty_frac)
    for c in priced:
        if len(picks) >= rc.max_markets:
            break
        if cap + c.capital > rc.quote_budget_usd:
            continue
        if c.empty and n_empty >= max_empty:
            continue
        picks.append(c)
        cap += c.capital
        n_empty += c.empty
    if journal:
        journal.log("select", candidates=len(cands), priced=len(priced), picked=len(picks), capital=round(cap, 2),
                    est_reward_day=round(sum(c.est_reward for c in picks), 2), empty=n_empty,
                    markets=[{"slug": c.slug, "rate": c.rate, "v": c.v, "size": c.size, "price": c.price,
                              "share": round(c.est_share, 3), "est": round(c.est_reward, 2), "empty": c.empty}
                             for c in picks])
    return picks
