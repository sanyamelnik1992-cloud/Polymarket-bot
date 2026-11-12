"""Find rewarded markets and pick the ones worth quoting (REST: CLOB rewards collection + order books + market details).

Selection (reports/68): every pool is ranked by what a minimum-size two-sided quote would earn per $ of capital.
The share of a pool comes from two sources and the lower one counts:
  * Polymarket's own `market_competitiveness` (total qualifying score of all makers per $1 of the daily pool), and
  * the live book (other makers' levels inside the reward zone, scored with the documented formula).
A market must be expected to pay at least `min_expected_reward` a day (payouts under $1 are not paid).
"""
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import requests

from ..http import get_json
from .scoring import adjusted_mid, official_share, order_score, our_share, plan_quotes

CLOB = "https://clob.polymarket.com"
_DETAILS = {}                 # condition_id -> {"fee_rate", "min_age", "tick", "min_order", "neg_risk"} (static per market)


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
    price: float = 0.5       # displayed price at selection: the MIDDLE of the book when it is wide (not a price!)
    last_trade: float | None = None  # YES price of the last trade at selection (CLOB /last-trades-prices)
    comp: float = 0.0        # official competitiveness at selection (total score per $1 of the daily pool)
    est_share: float = 0.0
    est_reward: float = 0.0  # $ per day at selection (the lower of the book and the official estimate)
    capital: float = 0.0     # pUSD our two bids need
    size: float = 0.0
    empty: bool = False      # nobody else quoted inside the reward zone at selection
    fee_rate: float | None = None   # taker fee rate of the market (None = not looked up yet)
    min_age: float | None = None    # seconds an order must rest before it scores (None = not looked up yet)
    neg_risk: bool = False
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
    s = str(s).replace("Z", "+00:00").replace(" ", "T")
    if re.search(r"[+-]\d\d$", s):                          # "2026-12-31T21:59:00+00" (the collection's own format)
        s += ":00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return float("nan")


def fetch_reward_markets(max_pages=10, page_size=500, min_rate=0.0):
    """Rewarded markets, highest daily pool first (the collection is public, no auth). Stops at the first page whose
    last row is under min_rate: the collection has ~100 000 rows and only ~11 000 carry a running pool."""
    rows, cur = [], None
    for _ in range(max_pages):
        p = {"order_by": "rate_per_day", "position": "DESC", "page_size": page_size}
        if cur:
            p["next_cursor"] = cur
        r = get_json(f"{CLOB}/rewards/markets/multi", p, timeout=90)
        d = r.get("data") or []
        rows += d
        cur = r.get("next_cursor")
        if not d or cur in (None, "LTE=") or rate_of(d[-1]) < min_rate:
            break
    return rows


def fetch_last_trades(token_ids, session=None):
    """token -> price of its last trade (None-free: tokens that never traded are left out)."""
    s = session or requests.Session()
    out = {}
    for i in range(0, len(token_ids), 500):
        chunk = token_ids[i:i + 500]
        for k in range(4):
            try:
                r = s.post(f"{CLOB}/last-trades-prices", json=[{"token_id": t} for t in chunk], timeout=60)
                if r.status_code == 200:
                    for x in r.json():
                        if x.get("side") and x.get("price") not in (None, ""):      # side "" = no trade yet
                            out[x["token_id"]] = float(x["price"])
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1 + k)
    return out


MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_DATE = re.compile(r"(week of\s+)?\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(20\d\d))?\b", re.I)


def date_passed(question, now=None):
    """The question is about a day that is already over ("Will Russia target Kyiv on November 8?" on 12 Nov): the
    outcome is known to somebody, the market just is not resolved yet. True when every date in the question is
    before today (UTC); "the week of <date>" lasts 7 days; a date without a year more than half a year back is read
    as next year's."""
    today = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).date()
    last = None
    for week, mon, day, year in _DATE.findall(question or ""):
        try:
            d = date(int(year) if year else today.year, MONTHS.index(mon.lower()) + 1, int(day))
        except ValueError:
            continue
        if not year and (today - d).days > 180:
            d = date(d.year + 1, d.month, d.day)
        if week:
            d = date.fromordinal(d.toordinal() + 6)
        last = d if last is None or d > last else last
    return last is not None and last < today


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


def parse_details(j):
    """Static per-market parameters from GET /clob-markets/{condition_id} (compact keys)."""
    r = j.get("r") or {}
    fd = j.get("fd") or {}
    return {"fee_rate": float(fd.get("r") or 0.0), "min_age": float(r["moas"]) if r.get("moas") is not None else None,
            "tick": float(j["mts"]) if j.get("mts") else None, "min_order": float(j["mos"]) if j.get("mos") else None,
            "neg_risk": bool(j.get("nr")), "rewards_on": bool(r.get("e", True)), "accepting": bool(j.get("ao", True))}


def fetch_details(condition_ids, get=get_json):
    """Fill the module cache for markets not seen yet; a failed lookup is retried at the next selection."""
    for cid in condition_ids:
        if cid in _DETAILS:
            continue
        try:
            _DETAILS[cid] = parse_details(get(f"{CLOB}/clob-markets/{cid}", None, timeout=30))
        except Exception:  # noqa: BLE001   (the market is still quoted with the config defaults)
            continue
    return _DETAILS


def book_levels(raw):
    bids = sorted(((float(x["price"]), float(x["size"])) for x in raw.get("bids") or [] if float(x["size"]) > 0),
                  reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in raw.get("asks") or [] if float(x["size"]) > 0)
    return bids, asks


def quote_dist(rc, v, alone):
    """Distance from the reference price in cents. Alone in the zone: far out (the whole pool is ours at any score,
    and a quote further from the price is hit less often and at a better price). With others: rc.dist_cents."""
    far = rc.alone_frac * v
    return far if alone else min(rc.dist_cents, far)


def is_alone(rm, q1, q2):
    """Nobody else scores in the reward zone: the live book shows no level there AND Polymarket's own number says
    under one point of total score (it lags the book; either one saying "somebody is here" is believed)."""
    return max(q1, q2) <= 1e-9 and rm.comp * rm.rate < 1.0


def order_size(rc, min_size, min_order=5.0):
    return max(min_size, min_order, 5.0) * rc.size_mult


def candidates(raw_markets, rc, now=None):
    """Filter the rewards collection by the config (pool, time to resolution, price, excluded topics, capital) and
    by the official estimate of what our quote would earn."""
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    excl = re.compile(rc.exclude_regex, re.I) if rc.exclude_regex else None
    deadline = re.compile(rc.deadline_regex, re.I) if rc.deadline_regex else None
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
        if (excl and excl.search(q)) or (deadline and deadline.search(q)) or date_passed(q, now):
            continue
        if rc.max_volume_24h and float(m.get("volume_24hr") or 0) > rc.max_volume_24h:
            continue
        e = end_ts(m)
        if not (e - now > rc.min_days_left * 86400):          # also drops unknown end dates (nan)
            continue
        price = float(toks[0].get("price") or 0)
        if not rc.price_lo <= price <= rc.price_hi:
            continue
        v = float(m.get("rewards_max_spread") or 0)
        min_size = float(m.get("rewards_min_size") or 0)
        if v < 1.0 or min_size <= 0:                          # a zone under one cent cannot hold a 1c-tick quote
            continue
        size = order_size(rc, min_size)
        if size > rc.max_per_market_usd / 0.9:                # two bids cost about $1 per share together
            continue
        comp = float(m.get("market_competitiveness") or 0)
        d = quote_dist(rc, v, alone=comp * rate < 1.0)
        share = official_share(rate, comp, order_score(v, d, size))
        if rate * share < rc.min_expected_reward:
            continue
        rm = RMarket(slug=m.get("market_slug") or m["condition_id"], question=q[:120], condition_id=m["condition_id"],
                     yes=toks[0]["token_id"], no=toks[1]["token_id"], rate=rate, v=v, min_size=min_size, end_ts=e,
                     price=price, comp=comp, size=size, est_share=share, est_reward=rate * share)
        out.append(rm)
    return out


def reference_price(bids, asks, rc, v, min_size, last_trade):
    """The price our quotes are centred on, or None when there is no price we can trust.
    * the book is tight (spread <= competitive_spread_mult x the max spread): its size-adjusted mid;
    * the book is wide: the last trade, if it lies inside the book. NOT the middle of the book: in a book of
      0.21 / 0.89 Polymarket shows 0.54 while the market trades at 0.88, and a quote around 0.54 sells YES 34 c
      too cheap (12 Nov: 5 such fills, -$31 of the day's -$93);
    * a wide book and no usable last trade: None (do not quote)."""
    if bids and asks and asks[0][0] - bids[0][0] <= rc.competitive_spread_mult * v / 100:
        return adjusted_mid(bids, asks, min_size) or (bids[0][0] + asks[0][0]) / 2
    if last_trade is None:
        return None
    lo = bids[0][0] if bids else 0.0
    hi = asks[0][0] if asks else 1.0
    return last_trade if lo < last_trade < hi else None


def price_market(rm, raw_book, rc):
    """Fill in tick, size, capital and the estimated reward of quoting rm now. False if it cannot be quoted."""
    if not raw_book:
        return False
    rm.tick = float(raw_book.get("tick_size") or 0.01)
    rm.min_order = float(raw_book.get("min_order_size") or 5)
    bids, asks = book_levels(raw_book)
    size = order_size(rc, rm.min_size, rm.min_order)
    ref = reference_price(bids, asks, rc, rm.v, rm.min_size, rm.last_trade)
    if ref is None or not rc.price_lo <= ref <= rc.price_hi:
        return False
    q1 = sum(order_score(rm.v, abs(p - ref) * 100, s) for p, s in bids)
    q2 = sum(order_score(rm.v, abs(p - ref) * 100, s) for p, s in asks)
    alone = is_alone(rm, q1, q2)
    plan = plan_quotes(ref, quote_dist(rc, rm.v, alone), rm.tick, bids[0][0] if bids else None,
                       asks[0][0] if asks else None, v_cents=rm.v)
    if plan is None:
        return False
    b, a = plan
    cap = size * b + size * (1 - a)
    if cap > rc.max_per_market_usd:
        return False
    share, mid, q_ours, q_comp = our_share(bids, asks, (b, size), (a, size), rm.v, rm.min_size)
    if share <= 0:
        return False
    share = min(share, official_share(rm.rate, rm.comp, q_ours))    # the lower of the book and the official estimate
    rm.size, rm.capital, rm.est_share = size, cap, share
    rm.est_reward = rm.rate * share
    rm.empty = q_comp <= 1e-9
    rm.extra = {"mid": mid, "q_comp": round(q_comp, 2), "q_ours": round(q_ours, 2),
                "spread": (asks[0][0] - bids[0][0]) if bids and asks else None}
    return True


def apply_details(rm, details):
    d = (details or {}).get(rm.condition_id)
    if not d:
        return True
    rm.fee_rate, rm.min_age, rm.neg_risk = d["fee_rate"], d["min_age"], d["neg_risk"]
    return d.get("rewards_on", True) and d.get("accepting", True)


def select(rc, journal=None, raw_markets=None, books=None, now=None, details=None, held=(), lasts=None):
    """Pick markets by estimated reward per $ of capital until the budget or max_markets is reached.
    Empty-book markets (we would be the only quote) are capped at rc.max_empty_frac of the picks.
    `held` = condition ids we quote now: they rank rc.hold_bonus higher, so a small change of the numbers does not
    throw a market out (an order has to rest 30 s before it scores, and every re-entry risks a bad first quote)."""
    raw_markets = raw_markets if raw_markets is not None else fetch_reward_markets(rc.max_pages, min_rate=rc.min_rate)
    if not raw_markets:                        # an empty answer must not look like "no markets": keep the old selection
        raise RuntimeError("empty rewards collection")
    cands = candidates(raw_markets, rc, now)
    cands = sorted(cands, key=lambda x: -(x.est_reward / x.size))[: rc.max_candidates]
    books = books if books is not None else fetch_books([c.yes for c in cands])
    lasts = lasts if lasts is not None else fetch_last_trades([c.yes for c in cands])
    for c in cands:
        c.last_trade = lasts.get(c.yes)
    priced = [c for c in cands if price_market(c, books.get(c.yes), rc) and c.est_reward >= rc.min_expected_reward]
    held = set(held)
    priced.sort(key=lambda c: -(c.est_reward / c.capital) * (rc.hold_bonus if c.condition_id in held else 1.0))
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
    details = details if details is not None else fetch_details([c.condition_id for c in picks])
    picks = [c for c in picks if apply_details(c, details)]       # rewards switched off / not accepting orders: skip
    if journal:
        journal.log("select", candidates=len(cands), priced=len(priced), picked=len(picks),
                    capital=round(sum(c.capital for c in picks), 2), kept=sum(c.condition_id in held for c in picks),
                    est_reward_day=round(sum(c.est_reward for c in picks), 2), empty=sum(c.empty for c in picks),
                    markets=[{"slug": c.slug, "rate": c.rate, "v": c.v, "size": c.size, "price": c.price,
                              "comp": round(c.comp, 3), "share": round(c.est_share, 3), "est": round(c.est_reward, 2),
                              "empty": c.empty} for c in picks])
    return picks
