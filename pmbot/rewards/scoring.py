"""Polymarket liquidity-reward scoring (docs: Programs -> Liquidity Rewards -> Methodology).

Per one-minute sample an order scores S(v, s) = ((v - s) / v)^2 * size, v = max qualifying spread in cents, s = its
distance in cents from the size-cutoff-adjusted midpoint. Side one = YES bids + NO asks, side two = YES asks + NO
bids. The NO book mirrors the YES book (a NO bid at q shows as a YES ask at 1 - q), so everything here works on the
YES book: side one = YES bids, side two = YES asks. A maker scores
    Q_min = min(Q1, Q2)                                if the midpoint is outside [0.10, 0.90]
    Q_min = max(min(Q1, Q2), max(Q1, Q2) / c), c = 3   otherwise (single-sided liquidity counts at a third)
and the market's daily pool is split by each maker's share of the total Q_min.
"""
import math

C_SINGLE = 3.0


def adj_price(levels, min_size):
    """Price where the cumulative size (best first) first reaches min_size: the size-cutoff side of the midpoint."""
    cum = 0.0
    for p, s in levels:
        cum += s
        if cum >= min_size - 1e-9:
            return p
    return None


def adjusted_mid(bids, asks, min_size):
    """bids best-first (descending), asks best-first (ascending); None if a side lacks min_size in total."""
    b, a = adj_price(bids, min_size), adj_price(asks, min_size)
    if b is None or a is None:
        return None
    return (b + a) / 2


def order_score(v_cents, dist_cents, size):
    if dist_cents >= v_cents or size <= 0:
        return 0.0
    return ((v_cents - dist_cents) / v_cents) ** 2 * size


def side_score(levels, mid, v_cents):
    return sum(order_score(v_cents, abs(p - mid) * 100, s) for p, s in levels)


def q_min(q1, q2, mid):
    if mid is None:
        return 0.0
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / C_SINGLE)
    return min(q1, q2)


def merge(levels, extra, descending):
    return sorted(list(levels) + [x for x in extra if x[1] > 0], key=lambda x: -x[0] if descending else x[0])


def our_share(comp_bids, comp_asks, ours_bid, ours_ask, v_cents, min_size):
    """Score of our two orders against the others in the book.

    comp_bids / comp_asks: other makers' YES levels [(price, size)], best first.
    ours_bid: (price, size) of our YES bid or None; ours_ask: (price, size) of our YES ask (= our NO bid at 1 - price).
    Returns (share, mid, q_ours, q_comp). The competitors are aggregated as if they were one maker -- an
    approximation (real makers are scored one by one), good enough to rank markets and track trends.
    """
    bids = merge(comp_bids, [ours_bid] if ours_bid else [], True)
    asks = merge(comp_asks, [ours_ask] if ours_ask else [], False)
    mid = adjusted_mid(bids, asks, min_size)
    if mid is None:
        return 0.0, None, 0.0, 0.0
    q1o = order_score(v_cents, abs(ours_bid[0] - mid) * 100, ours_bid[1]) if ours_bid else 0.0
    q2o = order_score(v_cents, abs(ours_ask[0] - mid) * 100, ours_ask[1]) if ours_ask else 0.0
    q_ours = q_min(q1o, q2o, mid)
    q_comp = q_min(side_score(comp_bids, mid, v_cents), side_score(comp_asks, mid, v_cents), mid)
    share = q_ours / (q_ours + q_comp) if q_ours > 0 else 0.0
    return share, mid, q_ours, q_comp


def round_down(x, tick):
    return math.floor(x / tick + 1e-9) * tick


def round_up(x, tick):
    return math.ceil(x / tick - 1e-9) * tick


def plan_quotes(ref, dist_cents, tick, best_bid=None, best_ask=None):
    """Target YES bid b and YES ask a around ref, at least one tick inside (never crossing) the book.
    Our YES ask is placed as a NO bid at 1 - a. Returns (b, a) or None."""
    b = round(round_down(ref - dist_cents / 100, tick), 6)
    a = round(round_up(ref + dist_cents / 100, tick), 6)
    if best_ask is not None and b >= best_ask - 1e-9:
        b = round(best_ask - tick, 6)
    if best_bid is not None and a <= best_bid + 1e-9:
        a = round(best_bid + tick, 6)
    if b < tick - 1e-9 or a > 1 - tick + 1e-9 or b >= a - 1e-9:
        return None
    return b, a
