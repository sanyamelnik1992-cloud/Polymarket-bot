"""Liquidity-rewards farmer: scoring formula, selection, quoting, fills, rewards, risk, settlement, state."""
import asyncio

import pytest

from pmbot.rewards import markets as mk
from pmbot.rewards.config import RewardsCfg, load_rewards_config
from pmbot.rewards.farmer import Farmer, Position
from pmbot.rewards.scoring import adj_price, official_share, order_score, our_share, plan_quotes, q_min, walk_asks


class J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append({"type": kind, **kw})

    def of(self, kind):
        return [e for e in self.ev if e["type"] == kind]


class Clock:
    """Injected clock (seconds); the farmer and the fake feed read the same time."""
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- scoring
def test_docs_worked_example():
    # Polymarket docs: adjusted mid 0.50, max spread 3c; 100@0.49 and 200@0.48 bids, 100 ask on the complement at 0.51
    q_one = order_score(3, 1, 100) + order_score(3, 2, 200) + order_score(3, 1, 100)
    assert q_one == pytest.approx((2 / 3) ** 2 * 100 + (1 / 3) ** 2 * 200 + (2 / 3) ** 2 * 100)
    assert order_score(3, 3, 100) == 0 and order_score(3, 4, 100) == 0


def test_q_min_single_sided_rules():
    assert q_min(90, 0, 0.5) == pytest.approx(30)          # one side counts at a third inside [0.10, 0.90]
    assert q_min(90, 0, 0.95) == 0                          # outside: two-sided only
    assert q_min(40, 90, 0.5) == 40


def test_adjusted_mid_skips_dust():
    assert adj_price([(0.50, 5), (0.48, 30)], 20) == 0.48
    assert adj_price([(0.50, 5)], 20) is None


def test_plan_quotes_never_crosses():
    assert plan_quotes(0.50, 1, 0.01) == (0.49, 0.51)
    assert plan_quotes(0.50, 1, 0.01, best_bid=0.50, best_ask=0.49) == (0.48, 0.51)   # stays inside a crossed book
    b, a = plan_quotes(0.50, 1, 0.01, best_bid=0.45, best_ask=0.49)
    assert b == 0.48 and a == 0.51
    b, a = plan_quotes(0.30, 1, 0.01, best_bid=0.31, best_ask=0.40)
    assert a == 0.32 and b == 0.29
    assert plan_quotes(0.005, 1, 0.01) is None


def test_plan_quotes_stays_inside_the_reward_zone():
    """Rounding to the tick pushes a quote away from the price: at v cents or further it scores nothing."""
    assert plan_quotes(0.505, 4.0, 0.01) == (0.46, 0.55)                    # 4.5c away on both sides: no zone given
    b, a = plan_quotes(0.505, 4.0, 0.01, v_cents=4.5)
    assert (b, a) == (0.47, 0.54) and order_score(4.5, (0.505 - b) * 100, 20) > 0
    assert plan_quotes(0.50, 3.825, 0.01, v_cents=4.5) == (0.46, 0.54)      # the lone-maker distance of a 4.5c zone
    assert plan_quotes(0.50, 3.825, 0.001, v_cents=4.5) == (0.461, 0.539)   # 0.001-tick market
    assert plan_quotes(0.50, 1.0, 0.01, v_cents=1.0) is None                # a 1c zone has no room for a 1c-tick quote


def test_official_share_from_competitiveness():
    """market_competitiveness = total score of all makers per $1 of the daily pool (reports/68)."""
    q = order_score(4.5, 2.0, 20)                                           # our 20 shares 2c from the mid
    assert official_share(100, 0.0, q) == 1.0                               # nobody scores: the whole pool
    assert official_share(100, 0.33, q) == pytest.approx(q / (33 + q))      # 33 points compete for $100 a day
    assert official_share(100, 0.33, 0.0) == 0.0


def test_walk_asks_average_price_and_thin_book():
    assert walk_asks([(0.52, 10), (0.55, 30)], 20) == pytest.approx((10 * 0.52 + 10 * 0.55) / 20)
    assert walk_asks([(0.52, 10)], 20) is None and walk_asks([], 5) is None


def test_share_empty_book_and_competition():
    share, mid, q_ours, q_comp = our_share([], [], (0.49, 20), (0.51, 20), 4.5, 20)
    assert share == 1 and mid == pytest.approx(0.50) and q_comp == 0
    share, *_ = our_share([(0.49, 20)], [(0.51, 20)], (0.49, 20), (0.51, 20), 4.5, 20)
    assert share == pytest.approx(0.5)
    share, *_ = our_share([], [], None, None, 4.5, 20)
    assert share == 0


# ---------------------------------------------------------------- selection
def raw_market(slug, rate=50, price=0.5, days=10, q="Will X happen?", v=4.5, min_size=20, cid=None, comp=0.0):
    import time
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
    return {"market_slug": slug, "question": q, "condition_id": cid or "c-" + slug, "rewards_max_spread": v,
            "rewards_min_size": min_size, "end_date": end, "market_competitiveness": comp,
            "rewards_config": [{"rate_per_day": rate, "start_date": "2000-01-01", "end_date": "2500-12-31"}],
            "tokens": [{"token_id": slug + "-Y", "price": price}, {"token_id": slug + "-N", "price": 1 - price}]}


LASTS = {t + "-Y": 0.50 for t in "abcd"}                               # every test market last traded at 0.50


def test_candidates_filters():
    rc = RewardsCfg()
    raw = [raw_market("ok"), raw_market("small", rate=2), raw_market("soon", days=1),
           raw_market("cheap", price=0.03), raw_market("sport", q="Lakers vs. Celtics"),
           raw_market("crypto", q="Bitcoin Up or Down on November 9?"),
           raw_market("big", min_size=200),                       # 200 shares a side = ~$200: over max_per_market_usd
           raw_market("crowded", rate=50, comp=40.0),             # 2000 points compete: our 6 points earn 15 cents a day
           raw_market("deadline", q="US x Iran ceasefire by November 30?"),
           raw_market("deadline2", q="Will the U.S. invade Iran before 2027?"),
           raw_market("deadline3", q="Next Astra Model (6.1+) released by June 30, 2027?"),
           dict(raw_market("busy"), volume_24hr=2500.0)]          # $2500 traded in 24 h: fills cost more than rewards
    assert [c.slug for c in mk.candidates(raw, rc)] == ["ok"]
    # both filters can be switched off
    loose = RewardsCfg(deadline_regex="", max_volume_24h=0)
    assert {c.slug for c in mk.candidates(raw, loose)} == {"ok", "deadline", "deadline2", "deadline3", "busy"}


def test_candidates_expected_reward_uses_competitiveness():
    rc = RewardsCfg()
    alone, shared = mk.candidates([raw_market("a", rate=30), raw_market("b", rate=30, comp=0.5)], rc)
    assert alone.est_share == 1.0 and alone.est_reward == 30          # nobody scores: the whole pool (if the formula holds)
    q = order_score(4.5, 2.0, 20)
    assert shared.comp == 0.5 and shared.est_reward == pytest.approx(30 * q / (15 + q))


def test_end_date_formats():
    assert mk.end_ts({"end_date": "2026-12-31 21:59:00+00"}) == mk.end_ts({"end_date": "2026-12-31T21:59:00Z"})
    assert mk.end_ts({"end_date": "garbage"}) != mk.end_ts({"end_date": "garbage"})        # nan


def test_market_details_parse_and_apply():
    d = mk.parse_details({"r": {"mi": 20, "ma": 4.5, "e": True, "moas": 30}, "mos": 5, "mts": 0.001, "nr": True,
                          "ao": True, "fd": {"r": 0.04, "e": 1, "to": True}})
    assert d["fee_rate"] == 0.04 and d["min_age"] == 30 and d["tick"] == 0.001 and d["neg_risk"] is True
    assert mk.parse_details({"r": {"e": True}})["fee_rate"] == 0.0     # geopolitics: no fee block
    rm = mk.RMarket(slug="m", question="q", condition_id="c", yes="Y", no="N", rate=10, v=4.5, min_size=20, end_ts=0)
    assert mk.apply_details(rm, {"c": d}) and rm.fee_rate == 0.04 and rm.min_age == 30
    off = dict(d, rewards_on=False)
    assert not mk.apply_details(rm, {"c": off})                        # rewards switched off: not quoted
    assert mk.apply_details(rm, {})                                    # unknown market: quoted with the defaults


def test_candidates_drop_repeated_rows():
    """The rewards collection repeats a row at page boundaries: one market must not be picked twice."""
    rc = RewardsCfg()
    raw = [raw_market("a", cid="same"), raw_market("a", cid="same"), raw_market("b")]
    assert sorted(c.slug for c in mk.candidates(raw, rc)) == ["a", "b"]


def test_rate_counts_only_running_configs():
    m = {"rewards_config": [{"rate_per_day": 100, "start_date": "2026-11-01", "end_date": "2026-11-30"},
                            {"rate_per_day": 50, "start_date": "2026-10-01", "end_date": "2026-10-31"},
                            {"rate_per_day": 7, "start_date": "2026-12-01", "end_date": "2026-12-31"}]}
    assert mk.rate_of(m, "2026-11-08") == 100


def test_select_prices_and_caps_empty_books():
    rc = RewardsCfg(max_markets=2, max_empty_frac=0.5, quote_budget_usd=1000)
    raw = [raw_market("a"), raw_market("b"), raw_market("c")]
    books = {"a-Y": {"bids": [], "asks": [], "tick_size": "0.01"},
             "b-Y": {"bids": [], "asks": [], "tick_size": "0.01"},
             "c-Y": {"bids": [{"price": "0.49", "size": "40"}], "asks": [{"price": "0.51", "size": "40"}],
                     "tick_size": "0.01"}}
    j = J()
    picks = mk.select(rc, j, raw_markets=raw, books=books, details={}, lasts=LASTS)
    assert len(picks) == 2 and sum(p.empty for p in picks) == 1     # only one empty-book market allowed
    assert any(p.slug == "c" for p in picks)
    assert j.ev[0]["type"] == "select" and j.ev[0]["picked"] == 2
    c = next(p for p in picks if p.slug == "c")                      # others score 2c from the mid: we join at 2c
    assert c.extra["q_ours"] == pytest.approx(order_score(4.5, 2.0, 20), abs=0.01) and 0 < c.est_share < 0.3


def test_select_takes_the_lower_of_the_book_and_the_official_share():
    """The book shows nobody in the zone, Polymarket's number says 100 points already compete: believe the lower,
    and quote at the contested distance (far out we would score next to nothing against them)."""
    rc = RewardsCfg(max_competitiveness=0)                           # the cap is tested on its own below
    books = {"a-Y": {"bids": [], "asks": [], "tick_size": "0.01"}}
    (p,) = mk.select(rc, J(), raw_markets=[raw_market("a", rate=50, comp=2.0)], books=books, details={}, lasts=LASTS)
    q = order_score(4.5, 2.0, 20)
    assert p.empty and p.extra["q_ours"] == pytest.approx(q, abs=0.01)
    assert p.est_share == pytest.approx(q / (100 + q)) and p.est_reward == pytest.approx(50 * q / (100 + q))
    (lone,) = mk.select(rc, J(), raw_markets=[raw_market("a", rate=50)], books=books, details={}, lasts=LASTS)
    assert lone.est_share == 1.0 and lone.extra["q_ours"] == pytest.approx(order_score(4.5, 4.0, 20), abs=0.01)
    # 2000 points compete for $50: 15 cents a day for our quote -> not worth a market
    assert mk.select(rc, J(), raw_markets=[raw_market("b", rate=50, comp=40.0)], books={"b-Y": books["a-Y"]},
                     details={}, lasts=LASTS) == []


def test_select_keeps_the_markets_we_already_quote():
    """First paper run: 45 picks were 163 different markets in 1.9 hours. A held market ranks hold_bonus higher."""
    rc = RewardsCfg(max_markets=1, max_empty_frac=1.0)
    raw = [raw_market("a", rate=50), raw_market("b", rate=60)]
    books = {t: {"bids": [], "asks": [], "tick_size": "0.01"} for t in ("a-Y", "b-Y")}
    assert [p.slug for p in mk.select(rc, J(), raw_markets=raw, books=books, details={}, lasts=LASTS)] == ["b"]
    j = J()
    assert [p.slug for p in mk.select(rc, j, raw_markets=raw, books=books, details={}, lasts=LASTS, held={"c-a"})] == ["a"]
    assert j.ev[0]["kept"] == 1
    raw[1] = raw_market("b", rate=80)                                 # 60 % more is more than the 1.5x bonus
    assert [p.slug for p in mk.select(rc, J(), raw_markets=raw, books=books, details={}, lasts=LASTS, held={"c-a"})] == ["b"]


def test_select_refuses_an_empty_collection():
    """An empty answer must raise, so the farmer keeps its previous selection instead of dropping every market."""
    with pytest.raises(RuntimeError):
        mk.select(RewardsCfg(), J(), raw_markets=[], books={})


# ---------------------------------------------------------------- quoting and fills
def farmer(tmp_path, clock=None, **cfg):
    clock = clock or Clock()
    rc = RewardsCfg(state_file=str(tmp_path / "state.json"), **cfg)
    f = Farmer(rc, J(), clock=clock)
    return f


def farmer_with_market(tmp_path, clock=None, **cfg):
    f = farmer(tmp_path, clock, **cfg)
    rm = mk.RMarket(slug="m", question="q", condition_id="c", yes="Y", no="N", rate=144.0, v=4.5, min_size=20,
                    end_ts=f.clock() + 10 * 86400, price=0.50, size=20, last_trade=0.50)
    f.apply_selection([rm])
    b = f.feed.books["Y"]
    b.bids, b.asks, b.last_rx = {0.30: 100.0}, {0.70: 100.0}, 1      # wide book: nobody inside the reward zone
    f.feed.last_msg = f.clock()                                       # the market websocket is alive
    return f


def test_quotes_fill_cooldown_and_position(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    ms = f.markets["m"]
    # alone in a 4.5c zone: 0.85 * 4.5 = 3.8c out, rounded to the tick = YES bid 0.46 and NO bid 0.46 (YES ask 0.54)
    assert ms.alone and ms.orders["Y"]["price"] == 0.46 and ms.orders["N"]["price"] == 0.46
    f.clock.t += 1
    f.on_trade("Y", "SELL", 0.40, 50, int(f.clock() * 1000))        # a taker sells YES through our bid
    assert f.pos["m"].yes == 20 and f.pos["m"].cost == pytest.approx(9.2)
    assert ms.cooldown_until > f.clock()
    # cooling down: no new risk, only the bid that pairs the 20 YES (a NO bid at the reward distance from the new
    # price 0.40: the pair would cost 0.46 + 0.56 = 1.02, cheaper than paying the spread)
    assert f.targets(ms, f.clock()) == {"N": (0.56, 20.0)}
    asyncio.run(f.quote_once())
    assert "Y" not in ms.orders and ms.orders["N"]["price"] == 0.56
    g = farmer_with_market(tmp_path / "off", reduce_only=False, hedge=False)
    asyncio.run(g.quote_once())
    g.clock.t += 1
    g.on_trade("Y", "SELL", 0.40, 50, int(g.clock() * 1000))
    assert g.targets(g.markets["m"], g.clock()) == {}                # without reduce_only: nothing while cooling down


def test_fill_wakes_the_quoter(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 1                                                    # the order goes live after 150 ms
    f.on_trade("Y", "SELL", 0.40, 50, int(f.clock() * 1000))
    assert f.pos["m"].yes > 0 and f._wake.is_set()


def test_tiny_fill_keeps_quotes_and_position(tmp_path):
    """A dust trade through our price is kept in the position but does not pause the market."""
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 1
    f.on_trade("Y", "SELL", 0.40, 0.3, int(f.clock() * 1000))
    assert f.pos["m"].yes == pytest.approx(0.3)
    assert f.markets["m"].cooldown_until == 0 and f.mfills.get("m", 0) == 0


def test_zero_size_print_is_not_a_fill(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.on_trade("Y", "SELL", 0.40, 0.0, int(f.clock() * 1000) + 1000)
    assert "m" not in f.pos and f.markets["m"].cooldown_until == 0


def test_real_order_through_our_price_is_a_fill(tmp_path):
    """Somebody posts an ask at our bid price: on the exchange it would have traded with our bid. On paper the
    book just shows it; it must count as a fill (the informed kind), not as a reason to step away for free."""
    f = farmer_with_market(tmp_path, requote_min_s=999)
    asyncio.run(f.quote_once())
    assert f.markets["m"].orders["Y"]["price"] == 0.46
    f.clock.t += 1                                                    # our order is live now
    b = f.feed.books["Y"]
    b.asks = {0.40: 12.0, 0.70: 100.0}                                # 12 shares offered at 0.40, through our 0.46 bid
    asyncio.run(f.quote_once())
    fill = f.journal.of("rfill")[0]
    assert fill["cross"] and fill["size"] == 12 and fill["price"] == 0.46 and f.pos["m"].yes == 12
    assert not f.markets["m"].orders and f.markets["m"].cooldown_until > f.clock()
    # the position is marked at what the book now offers it for (0.40), not at the old 0.50 reference
    assert f.status()["open_pnl"] == pytest.approx(12 * (0.40 - 0.46))


def test_crossing_before_our_order_is_live_just_moves_it(tmp_path):
    """Within our own placement latency the order is not on the book yet: nothing to hit, the quote steps away."""
    f = farmer_with_market(tmp_path, requote_min_s=999)
    asyncio.run(f.quote_once())
    b = f.feed.books["Y"]
    b.asks = {0.46: 50.0, 0.70: 100.0}                                # the ask moved onto our bid in the same instant
    asyncio.run(f.quote_once())
    # no fill; and the last trade (0.50) is now outside the book (0.30 / 0.46): no price to trust, no quotes at all
    assert "m" not in f.pos and not f.markets["m"].orders
    f.on_trade("Y", "SELL", 0.44, 3, int(f.clock() * 1000) + 5000)    # a trade inside the book gives a price again
    asyncio.run(f.quote_once())
    assert "m" not in f.pos and f.markets["m"].orders["Y"]["price"] < 0.44


def test_wide_book_is_priced_by_the_last_trade_not_by_its_middle(tmp_path):
    """12 Nov, Tennessee 2028: book 0.21 / 0.89, Polymarket shows 0.54 (the middle), the market trades at 0.88. The
    bot quoted around 0.54 and sold YES 34 c too cheap. The reference of a wide book is the last trade."""
    f = farmer_with_market(tmp_path)
    ms = f.markets["m"]
    ms.rm.price, ms.rm.last_trade = 0.54, 0.88
    b = f.feed.books["Y"]
    b.bids, b.asks = {0.21: 6.0}, {0.89: 30.0}
    tg = f.targets(ms, f.clock())
    assert ms.ref == 0.88 and tg["Y"][0] < 0.88 and 1 - tg["N"][0] > 0.85        # quotes around 0.88, not 0.54
    ms.rm.last_trade = None                                                      # no trade known: no quotes
    assert f.targets(ms, f.clock()) == {}
    assert mk.reference_price([(0.48, 50)], [(0.50, 50)], RewardsCfg(), 4.5, 20, 0.88) == pytest.approx(0.49)   # tight book: its mid


def test_select_skips_wide_books_without_a_trade_inside():
    rc = RewardsCfg(max_empty_frac=1.0)
    books = {t: {"bids": [{"price": "0.21", "size": "6"}], "asks": [{"price": "0.89", "size": "30"}], "tick_size": "0.01"}
             for t in ("a-Y", "b-Y")}
    raw = [raw_market("a", price=0.54), raw_market("b", price=0.54)]
    picks = mk.select(rc, J(), raw_markets=raw, books=books, details={}, lasts={"a-Y": 0.88})
    assert [p.slug for p in picks] == ["a"] and picks[0].last_trade == 0.88 and picks[0].extra["mid"] > 0.8


def test_questions_about_a_day_that_is_over_are_skipped():
    """12 Nov: "Will Russia target Kyiv on November 8?" is decided, just not resolved: somebody knows the answer."""
    import calendar
    now = calendar.timegm((2026, 11, 12, 15, 0, 0))
    assert mk.date_passed("Will Russia target Kyiv on November 8, 2026?", now)
    assert mk.date_passed("US x Iran Effective Ceasefire by November 8?", now)
    assert not mk.date_passed("Will OpenAI have the most market share the week of November 9?", now)   # Nov 9-15
    assert not mk.date_passed("Will the White House call a full lid by 6:30 PM on November 14?", now)
    assert not mk.date_passed("Will X happen by January 5?", now)                                       # next year
    assert not mk.date_passed("Will GPT-6 be released in 2027?", now)                                   # no day: no rule
    assert not mk.date_passed("Will it happen between November 6 and November 13?", now)                # last day ahead
    rc = RewardsCfg(deadline_regex="")
    assert [c.slug for c in mk.candidates([raw_market("k", q="Will Russia target Kyiv on November 8, 2026?")], rc,
                                          now=now)] == []


def test_a_bid_through_our_ask_fills_the_no_side(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    assert f.markets["m"].orders["N"]["price"] == 0.46                # = a YES ask at 0.54
    f.clock.t += 1
    f.feed.books["Y"].bids = {0.60: 100.0, 0.30: 100.0}               # a buyer bids 0.60 for YES: he would lift 0.54
    asyncio.run(f.quote_once())
    assert f.pos["m"].no == 20 and f.journal.of("rfill")[0]["cross"]
    # the book is 0.60 / 0.70 now: YES is worth 0.65 (and never less than the 0.60 bid), so NO is worth 0.35
    assert f.status()["open_pnl"] == pytest.approx(20 * (0.35 - 0.46))


def test_unselected_markets_without_position_are_forgotten(tmp_path):
    f = farmer_with_market(tmp_path)                       # selects "m"
    f.apply_selection([])                                  # the next selection drops it
    assert "m" not in f.markets and "m" not in f.meta
    f2 = farmer_with_market(tmp_path)
    f2.pos["m"] = Position(yes=5, no=0, cost=2.5)          # a position keeps its market and metadata
    f2.apply_selection([])
    assert "m" in f2.meta and "m" in f2.markets and f2.markets["m"].active is False


def test_book_and_fresh_trade_disagree_means_no_quotes(tmp_path):
    """The book mid is ~0.50 but a trade just printed at 0.40 (7c away): quoting here is being picked off."""
    f = farmer_with_market(tmp_path)
    f.feed.books["Y"].bids, f.feed.books["Y"].asks = {0.48: 100.0}, {0.52: 100.0}
    f.clock.t += 1
    f.on_trade("Y", "SELL", 0.40, 1.0, int(f.clock() * 1000))         # tiny print, but a print
    assert f.targets(f.markets["m"], f.clock()) == {}
    f.clock.t += 601                                                  # the print is now stale: quote again
    f.feed.last_msg = f.clock()
    assert f.targets(f.markets["m"], f.clock()) != {}


def test_stale_feed_quotes_nothing_and_earns_nothing(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 2
    f.feed.last_msg = f.clock() - 120                                # the websocket went silent two minutes ago
    assert f.targets(f.markets["m"], f.clock()) == {}
    f.clock.t += 60
    before = sum(f.rewards_by_day.values())
    f.sample_once()
    assert sum(f.rewards_by_day.values()) == pytest.approx(before)   # no credit against a silent book


def test_max_fills_survive_a_restart(tmp_path):
    f = farmer_with_market(tmp_path, max_fills_per_market_day=2)
    asyncio.run(f.quote_once())
    for _ in range(2):
        f.clock.t += 1
        f.on_trade("Y", "SELL", 0.40, 5, int(f.clock() * 1000))
        f.feed.last_msg = f.clock()
        f.markets["m"].cooldown_until = 0                            # skip the cooldown, count the fills
    assert f.mfills["m"] == 2 and set(f.targets(f.markets["m"], f.clock())) == {"N"}   # only the exit of the 10 YES
    g = Farmer(f.rc, J(), clock=f.clock)                              # restart from the state file
    assert g.mfills.get("m") == 2 and g.fills_24h("m", g.clock()) == 2


def test_fill_cap_is_rolling_24_hours(tmp_path):
    """Two fills late in the evening must not be forgotten at midnight: the market rests for 24 hours."""
    f = farmer_with_market(tmp_path, max_fills_per_market_day=2)
    asyncio.run(f.quote_once())
    for _ in range(2):
        f.clock.t += 1
        f.on_trade("Y", "SELL", 0.40, 5, int(f.clock() * 1000))
        f.markets["m"].cooldown_until = 0
    f.clock.t += 6 * 3600
    f.feed.last_msg = f.clock()
    f.mfills = {}                                                     # as roll_day() does at 00:00 UTC
    f.pos.clear()                                                     # flat: the cap alone decides
    assert f.targets(f.markets["m"], f.clock()) == {}
    f.clock.t += 19 * 3600
    f.feed.last_msg = f.clock()
    f.markets["m"].last_trade_t = None                                # the old print is no reason to stay away any more
    assert f.targets(f.markets["m"], f.clock()) != {}


# ---------------------------------------------------------------- distance, order age, queue
def contested(f, bid=0.48, ask=0.52, size=100.0):
    """Other makers quote inside the reward zone of market "m" (mid 0.50)."""
    b = f.feed.books["Y"]
    b.bids, b.asks = {bid: size, 0.30: 100.0}, {ask: size, 0.70: 100.0}
    n = f.feed.books["N"]                                             # the NO book is the mirror of the YES book
    n.bids, n.asks, n.last_rx = {round(1 - ask, 6): size}, {round(1 - bid, 6): size}, 1


def test_distance_depends_on_competition(tmp_path):
    """Alone in the zone: far out (the whole pool at any score). With others scoring: dist_cents from the mid."""
    f = farmer_with_market(tmp_path)
    tg = f.targets(f.markets["m"], f.clock())
    assert tg["Y"][0] == 0.46 and tg["N"][0] == 0.46 and f.markets["m"].alone
    contested(f)
    tg = f.targets(f.markets["m"], f.clock())
    assert tg["Y"][0] == 0.48 and tg["N"][0] == 0.48 and not f.markets["m"].alone
    # the book is empty again but Polymarket's number says 50 points compete: still the contested distance
    f = farmer_with_market(tmp_path)
    f.markets["m"].rm.comp = 50 / 144.0
    assert f.targets(f.markets["m"], f.clock())["Y"][0] == 0.48 and not f.markets["m"].alone


def test_resting_order_keeps_its_age_on_a_small_move(tmp_path):
    """An order scores only after 30 s on the book: one tick of drift must not replace it, a real move must."""
    f = farmer_with_market(tmp_path)
    contested(f)
    asyncio.run(f.quote_once())
    ms = f.markets["m"]
    oid, t0 = ms.orders["Y"]["oid"], ms.orders["Y"]["t"]
    f.clock.t += 40
    f.feed.last_msg = f.clock()
    contested(f, bid=0.49, ask=0.53)                                  # the mid moved up one cent: target 0.49
    asyncio.run(f.quote_once())
    assert ms.orders["Y"]["oid"] == oid and ms.orders["Y"]["t"] == t0 and ms.orders["Y"]["price"] == 0.48
    assert f.sample_once() > 0                                        # ... and it still scores (3c from the mid)
    noid = ms.orders["N"]["oid"]
    contested(f, bid=0.44, ask=0.50)                                  # the others stepped down: the mid is 0.47 now
    f.clock.t += 1
    asyncio.run(f.quote_once())
    # our 0.48 bid is above the mid (much too close) and our 0.52 ask is 5c away (outside the 4.5c zone): both move
    assert ms.orders["Y"]["oid"] != oid and ms.orders["Y"]["price"] == 0.45
    assert ms.orders["N"]["oid"] != noid and ms.orders["N"]["price"] == 0.51


def test_order_too_close_to_the_price_is_moved_at_once(tmp_path):
    f = farmer_with_market(tmp_path, requote_min_s=999)
    contested(f)
    asyncio.run(f.quote_once())
    ms = f.markets["m"]
    assert ms.orders["N"]["price"] == 0.48                            # a YES ask at 0.52
    contested(f, bid=0.50, ask=0.53)                                  # the mid is 0.515 now: our ask is half a cent away
    asyncio.run(f.quote_once())
    assert ms.orders["N"]["price"] < 0.48                             # replaced although the re-quote timer has not run out


def test_print_at_our_price_uses_the_queue_ahead_first(tmp_path):
    """We join 100 shares at 0.48: a print AT 0.48 fills the earlier orders first, the rest of it reaches us."""
    f = farmer_with_market(tmp_path, hedge=False)
    contested(f)
    asyncio.run(f.quote_once())
    f.clock.t += 1
    f.on_trade("Y", "SELL", 0.48, 60, int(f.clock() * 1000))
    assert "m" not in f.pos                                           # 60 of the 100 ahead of us
    f.on_trade("Y", "SELL", 0.48, 55, int(f.clock() * 1000))
    assert f.pos["m"].yes == pytest.approx(15)                        # 40 more ahead, then 15 of our 20


# ---------------------------------------------------------------- hedge
def test_fill_is_hedged_and_merged(tmp_path):
    """reports/68: the largest reward earners exit 97 % of their fills within 5 s by buying the other outcome and
    merging the pair. Here: YES bought at 0.48, NO bought at 0.53 + fee -> the pair cost 1.0225 and pays 1."""
    f = farmer_with_market(tmp_path)
    contested(f)
    asyncio.run(f.quote_once())
    f.clock.t += 1
    f.on_trade("Y", "SELL", 0.47, 50, int(f.clock() * 1000))          # through our 0.48 bid
    assert f.pos["m"].yes == 20
    asyncio.run(f.quote_once())
    assert f.pos["m"].yes == 20 and not f.journal.of("rhedge")        # the hedge order is still on its way (300 ms)
    assert 0 < f._next_hedge_in() <= 0.3
    f.clock.t += 0.5
    f.feed.books["N"].asks = {0.53: 100.0}                            # the 0.48 bids are gone: NO now costs 0.53
    asyncio.run(f.quote_once())
    h = f.journal.of("rhedge")[0]
    fee = 0.05 * 0.53 * 0.47
    assert h["buy"] == "N" and h["size"] == 20 and h["cost_per_share"] == pytest.approx(0.48 + 0.53 + fee - 1, abs=1e-4)
    assert "m" not in f.pos and f.realized == pytest.approx(-20 * (0.01 + fee))
    st = f.status()
    assert st["hedges_today"] == 1 and st["hedge_loss_today"] == pytest.approx(20 * (0.01 + fee), abs=0.01)
    assert st["trading_pnl"] == pytest.approx(f.realized, abs=0.01) and st["unhedged_usd"] == 0
    assert f.targets(f.markets["m"], f.clock()) == {}                 # the market still cools down after the fill


def test_expensive_hedge_waits_and_the_position_stays(tmp_path):
    f = farmer_with_market(tmp_path)                                  # wide book: nobody behind our bid
    asyncio.run(f.quote_once())
    f.clock.t += 1
    f.on_trade("Y", "SELL", 0.30, 50, int(f.clock() * 1000))          # our 0.46 bid is hit
    f.clock.t += 1
    f.feed.books["N"].asks, f.feed.books["N"].last_rx = {0.70: 100.0}, 1    # NO costs 0.70: the pair would cost 1.17
    asyncio.run(f.quote_once())
    assert f.pos["m"].yes == 20 and not f.journal.of("rhedge")
    assert f.status()["unhedged_usd"] == pytest.approx(9.2)
    f.feed.books["N"].asks = {0.57: 100.0}                            # later the book comes back: 0.46 + 0.57 + fee
    f.clock.t += 60
    f.feed.last_msg = f.clock()
    asyncio.run(f.quote_once())
    assert "m" not in f.pos
    assert f.journal.of("rhedge")[0]["cost_per_share"] == pytest.approx(0.03 + 0.05 * 0.57 * 0.43, abs=1e-4)


def test_hedge_uses_the_fee_rate_of_the_market(tmp_path):
    f = farmer_with_market(tmp_path)
    f.markets["m"].rm.fee_rate = 0.0                                  # geopolitics: no taker fee
    f.pos["m"] = Position(yes=20, no=0, cost=9.2)
    f.feed.books["N"].asks, f.feed.books["N"].last_rx = {0.55: 100.0}, 1
    assert f.hedge_once() == 1 and f.realized == pytest.approx(-20 * 0.01)


def test_hedge_off_and_dust(tmp_path):
    f = farmer_with_market(tmp_path, hedge=False)
    f.pos["m"] = Position(yes=20, no=0, cost=9.2)
    f.feed.books["N"].asks, f.feed.books["N"].last_rx = {0.50: 100.0}, 1
    assert f.hedge_once() == 0
    g = farmer_with_market(tmp_path)
    g.pos["m"] = Position(yes=3, no=0, cost=1.4)                      # under the 5-share minimum order: stays
    g.feed.books["N"].asks, g.feed.books["N"].last_rx = {0.50: 100.0}, 1
    assert g.hedge_once() == 0 and g.pos["m"].yes == 3


def test_position_merge_keeps_the_cost_of_the_rest():
    p = Position()
    p.add("Y", 30, 30 * 0.40)
    p.add("N", 20, 20 * 0.62)
    pairs, pnl = p.merge()
    assert pairs == 20 and pnl == pytest.approx(20 * (1 - 0.40 - 0.62))
    assert p.yes == 10 and p.no == 0 and p.cost == pytest.approx(10 * 0.40)
    assert Position(yes=20, no=5, cost=12.5).cost == 12.5             # one total (old state files): kept as is


def test_open_positions_use_the_quote_budget(tmp_path):
    """A real wallet pays for inventory and for resting bids from the same balance."""
    f = farmer_with_market(tmp_path, quote_budget_usd=25, max_inventory_usd_per_market=999, hedge=False)
    f.pos["m"] = Position(yes=20, no=0, cost=9.2)
    asyncio.run(f.quote_once())
    assert len(f.markets["m"].orders) == 1                            # 9.2 held + one 9.2 bid; the second does not fit
    assert f.status()["bids_usd"] == pytest.approx(9.2)


# ---------------------------------------------------------------- rewards
def test_sample_credits_share_of_pool(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 31                                                   # an order scores after resting 30 s
    inc = f.sample_once()
    assert inc == pytest.approx(144.0 * 60 / 86400)                   # alone in the zone: the whole pool, one minute
    st = f.status()
    assert st["alone_today"] == st["rewards_today"] and st["official_today"] == st["rewards_today"]
    assert st["cautious_pnl"] == 0.0                                  # the lone-maker part is not counted there


def test_young_orders_do_not_score(tmp_path):
    """reports/68: market field r.moas = 30 s on 96 of 100 markets: an order counts only after resting that long."""
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 10
    assert f.sample_once() == 0.0 and f.journal.of("rsample")[-1]["young"] == 2
    f.markets["m"].rm.min_age = 4.0                                   # the market's own value wins over the default
    f.clock.t += 5
    assert f.sample_once() == pytest.approx(144.0 * 5 / 86400)


def test_accrual_is_time_weighted(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 31
    f.sample_once()                                                   # first sample credits one nominal minute
    f.clock.t += 30
    f.feed.last_msg = f.clock()
    inc = f.sample_once()
    assert inc == pytest.approx(144.0 * 30 / 86400)                   # the real 30 s, not a fixed minute


def test_stress_is_half_the_estimate_when_alone(tmp_path):
    """Alone in the zone the estimate is the whole pool; if one identical maker joined we would get half of it."""
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 31
    f.sample_once()
    st = f.status()
    assert st["rewards_total"] > 0
    assert st["stress_total"] == pytest.approx(st["rewards_total"] / 2)


def test_position_value_counts_pairs_at_one_dollar():
    p = Position(yes=30, no=20, cost=24)
    assert p.value(0.4) == pytest.approx(20 + 10 * 0.4)


# ---------------------------------------------------------------- risk
def test_stop_survives_a_restart_and_a_lost_base_is_strict(tmp_path):
    """12 Nov: the stop of 11:14 did not survive the update restart at 11:46; the bot quoted again from a fresh base
    and stopped a second time. The stop and the base are kept per date, a missing base counts every open mark."""
    import json
    f = farmer_with_market(tmp_path, daily_loss_limit=5)
    f.pos["m"] = Position(yes=100, no=0, cost=60)                    # marked at 0.50: -10
    f.check_risk(f.status())
    assert f.halted_until > f.clock()
    f._save()
    st = json.load(open(f.rc.state_file))
    assert st["halts"] == {f.day: f.halted_until} and st["day_bases"] == {f.day: 0.0}
    st["day"] = "1999-01-01"                                         # whatever broke the old "day" check
    json.dump(st, open(f.rc.state_file, "w"))
    g = Farmer(f.rc, J(), clock=f.clock)
    assert g.halted_until == f.halted_until and g.day_base == 0.0 and g._loaded["file_day"] == "1999-01-01"
    del st["day_bases"]                                              # the base itself lost: the strict fallback
    st["realized"], st["realized_by_day"] = -7.0, {f.day: -3.0}
    json.dump(st, open(f.rc.state_file, "w"))
    h = Farmer(f.rc, J(), clock=f.clock)
    h.markets, h.meta["m"]["mid"] = f.markets, 0.5
    assert h.status()["trading_pnl_today"] == pytest.approx(-3.0 - 10.0)    # today's realised + every open mark


def test_daily_loss_halts_quotes(tmp_path):
    f = farmer_with_market(tmp_path, daily_loss_limit=5)
    f.pos["m"] = Position(yes=100, no=0, cost=50)                    # marks at the 0.50 reference (wide book)
    st = f.status()
    f.day_base = st["trading_pnl"] + 10                               # as if we were 10$ better this morning
    f.check_risk(f.status())
    assert f.halted_until > f.clock()
    assert f.targets(f.markets["m"], f.clock()) == {}
    assert any(e["type"] == "kill" for e in f.journal.ev)


def test_stop_counts_only_the_rewards_earned_next_to_others(tmp_path):
    f = farmer_with_market(tmp_path, daily_loss_limit=5)
    f.pos["m"] = Position(yes=100, no=0, cost=60)                    # -10 at the 0.50 mark
    day = f.day
    f.rewards_by_day[day], f.official_by_day[day], f.alone_by_day[day] = 12.0, 11.0, 4.0
    st = f.status()
    assert st["trading_pnl_today"] == -10 and st["cautious_today"] == pytest.approx(-10 + 11 - 4)
    f.check_risk(st)
    assert f.halted_until == 0                                       # -3: under the $5 limit
    f.alone_by_day[day] = 9.0                                         # most of it earned alone: does not count
    f.check_risk(f.status())
    assert f.halted_until > f.clock() and "осторожно" in f.journal.of("kill")[0]["reason"]
    g = farmer_with_market(tmp_path / "b", daily_loss_limit=5, stop_counts_rewards=False)
    g.pos["m"] = Position(yes=100, no=0, cost=60)
    g.rewards_by_day[g.day] = g.official_by_day[g.day] = 50.0
    g.check_risk(g.status())
    assert g.halted_until > g.clock()                                # trading only: rewards ignored


def test_inventory_quotes_only_the_side_that_pairs_it(tmp_path):
    """reduce_only: 20 unpaired YES -> only a NO bid for 20, also after the market left the selection."""
    f = farmer_with_market(tmp_path)
    f.pos["m"] = Position(yes=25, no=5, cost=12.5)
    tg = f.targets(f.markets["m"], f.clock())
    assert set(tg) == {"N"} and tg["N"][1] == 20
    f.markets["m"].active = False                                     # deselected: the exit still rests
    assert set(f.targets(f.markets["m"], f.clock())) == {"N"}
    f.pos["m"] = Position(yes=2, no=0, cost=1.0)                      # under the 5-share minimum: flat for quoting
    assert f.targets(f.markets["m"], f.clock()) == {}                 # deselected and flat: nothing


def test_pairs_are_merged_at_once(tmp_path):
    f = farmer_with_market(tmp_path, hedge=False)
    f.pos["m"] = Position()
    f.pos["m"].add("Y", 20, 20 * 0.46)
    f.pos["m"].add("N", 20, 20 * 0.50)                                # both bids filled: 20 pairs at 0.96
    assert f.merge_pairs() == 1 and "m" not in f.pos
    assert f.realized == pytest.approx(20 * 0.04) and f.journal.of("rmerge")[0]["pairs"] == 20
    assert f.budget_used() == 0


def test_exit_bid_survives_a_restart_and_ignores_the_budget(tmp_path):
    """After a restart the market of an unpaired position is known again (its exit bid can rest before any selection);
    the exit bid is placed even when the budget is used up: it frees money when it fills."""
    f = farmer_with_market(tmp_path, quote_budget_usd=5)
    f.pos["m"] = Position(yes=20, no=0, cost=9.2)
    asyncio.run(f.quote_once())
    assert set(f.markets["m"].orders) == {"N"}                       # over the $5 budget, the exit bid rests anyway
    f._save()
    g = Farmer(f.rc, J(), clock=f.clock)
    assert "m" in g.markets and not g.markets["m"].active and g.markets["m"].rm.v == 4.5
    g.feed.books["Y"] = f.feed.books["Y"]
    g.feed.last_msg = g.clock()
    assert set(g.targets(g.markets["m"], g.clock())) == {"N"}


def test_competitiveness_cap():
    raw = [raw_market("a", rate=50, comp=0.5), raw_market("b", rate=50, comp=1.5)]
    assert [c.slug for c in mk.candidates(raw, RewardsCfg())] == ["a"]
    assert {c.slug for c in mk.candidates(raw, RewardsCfg(max_competitiveness=0))} == {"a", "b"}


def test_inventory_cap_removes_the_side_that_adds_to_it(tmp_path):
    f = farmer_with_market(tmp_path, max_inventory_usd_per_market=10, reduce_only=False)
    f.pos["m"] = Position(yes=40, no=0, cost=20)                     # long YES: no more YES bids
    tg = f.targets(f.markets["m"], f.clock())
    assert "Y" not in tg and "N" in tg


# ---------------------------------------------------------------- settlement
def settled_farmer(tmp_path, end_offset):
    f = farmer_with_market(tmp_path)
    f.markets["m"].active = False                                    # no longer selected
    f.pos["m"] = Position(yes=20, no=0, cost=9.8)
    f.meta["m"] = {"yes": "Y", "no": "N", "mid": 0.4, "end": None if end_offset is None else f.clock() + end_offset}
    return f


def test_settlement_pays_the_winning_side(tmp_path, monkeypatch):
    f = settled_farmer(tmp_path, end_offset=-7200)                   # ended two hours ago
    monkeypatch.setattr(Farmer, "_gamma_resolution", staticmethod(lambda slug, y, n: (1.0, 0.0)))
    asyncio.run(f.settle_once())
    assert "m" not in f.pos
    assert f.realized == pytest.approx(20 - 9.8)
    assert f.journal.of("rsettle") and f.journal.of("rsettle")[0]["pnl"] == pytest.approx(10.2)


def test_settlement_losing_side(tmp_path, monkeypatch):
    f = settled_farmer(tmp_path, end_offset=-7200)
    monkeypatch.setattr(Farmer, "_gamma_resolution", staticmethod(lambda slug, y, n: (0.0, 1.0)))
    asyncio.run(f.settle_once())
    assert f.realized == pytest.approx(-9.8)


def test_resolution_query_asks_for_closed_markets(monkeypatch):
    """Gamma returns a market by slug only with closed=true once it has closed (live check, see reports/39)."""
    import pmbot.rewards.farmer as fm
    calls = []

    def fake_get(url, params=None, timeout=None, **kw):
        calls.append(params)
        if params and params.get("closed") == "true":
            return [{"slug": "s", "closed": True, "clobTokenIds": '["Y", "N"]', "outcomePrices": '["1", "0"]'}]
        return []

    monkeypatch.setattr(fm, "get_json", fake_get)
    assert Farmer._gamma_resolution("s", "Y", "N") == (1.0, 0.0)
    assert calls[-1].get("closed") == "true"
    assert Farmer._gamma_resolution("s", "N", "Y") == (0.0, 1.0)


def test_unresolved_market_keeps_its_position(tmp_path, monkeypatch):
    f = settled_farmer(tmp_path, end_offset=-7200)
    monkeypatch.setattr(Farmer, "_gamma_resolution", staticmethod(lambda slug, y, n: None))
    asyncio.run(f.settle_once())
    assert f.pos["m"].yes == 20 and f.realized == 0


def test_market_that_has_not_ended_is_not_checked(tmp_path, monkeypatch):
    f = settled_farmer(tmp_path, end_offset=+86400)
    called = []
    monkeypatch.setattr(Farmer, "_gamma_resolution", staticmethod(lambda *a: called.append(a) or None))
    asyncio.run(f.settle_once())
    assert called == []


def test_unknown_end_date_is_still_checked(tmp_path, monkeypatch):
    f = settled_farmer(tmp_path, end_offset=None)
    monkeypatch.setattr(Farmer, "_gamma_resolution", staticmethod(lambda slug, y, n: (1.0, 0.0)))
    asyncio.run(f.settle_once())
    assert "m" not in f.pos


# ---------------------------------------------------------------- state and config
def test_state_roundtrip(tmp_path):
    f = farmer_with_market(tmp_path)
    f.pos["m"] = Position(yes=20, no=5, cost=12.5)
    f.rewards_by_day["2026-11-08"] = 3.5
    f.official_by_day["2026-11-08"] = 3.0
    f.alone_by_day["2026-11-08"] = 1.5
    f.hedges_by_day["2026-11-08"] = [2, 40.0, 0.9]
    f.realized = 1.25
    f.mfills["m"] = 2
    f._save()
    g = Farmer(f.rc, J(), clock=f.clock)
    assert g.pos["m"].yes == 20 and g.rewards_by_day["2026-11-08"] == 3.5
    assert g.meta["m"]["yes"] == "Y" and g.realized == 1.25 and g.mfills["m"] == 2
    assert g.pos["m"].cy == pytest.approx(10.0) and g.pos["m"].cn == pytest.approx(2.5)
    assert g.official_by_day["2026-11-08"] == 3.0 and g.alone_by_day["2026-11-08"] == 1.5
    st = g.status()
    assert st["hedges_total"] == 2 and st["hedge_loss_total"] == 0.9
    assert st["cautious_pnl"] == pytest.approx(3.0 - 1.5 + st["trading_pnl"])


def test_config_is_paper_only(tmp_path):
    c = load_rewards_config("config/rewards.toml")
    assert c.mode == "paper"
    p = tmp_path / "live.toml"
    p.write_text('[rewards]\nmode = "live"\n')
    with pytest.raises(ValueError):
        load_rewards_config(str(p))
    for bad in ("alone_frac = 1.0", "dist_cents = 0", "typo = 1"):
        p.write_text("[rewards]\n" + bad + "\n")
        with pytest.raises(ValueError):
            load_rewards_config(str(p))


# ---------------------------------------------------------------- unit, status and report wiring
def test_unit_is_paper_and_wired_into_ops_and_telegram():
    import os
    from pmbot import notify, ops
    text = open(os.path.join(ops.REPO, "deploy", "pmbot-rewards.service")).read()
    assert "--config config/rewards.toml" in text and "--live" not in text
    assert "CPUWeight=20" in text                                      # yields to the live bots, like every paper unit
    assert "[Install]" in text and "pmbot-rewards" in ops.ALL_UNITS and "pmbot-rewards" not in ops.BOT_UNITS
    journal, state, mode = ops.bot_paths("pmbot-rewards")
    assert journal == "data/journal-rewards" and state == "data/state/rewards_paper.json" and mode == "paper"
    assert notify.unit_for_config("config/rewards.toml") == "pmbot-rewards"
    for script in ("autoupdate.sh", "install_units.sh"):               # restarted on updates, startable from Telegram
        assert " pmbot-rewards " in open(os.path.join(ops.REPO, "deploy", script)).read()


def test_status_and_report_show_the_farmer(tmp_path, monkeypatch):
    """/status and the hourly report: the estimate, the lone-maker part, what the fills cost and the cautious total."""
    import time
    from pmbot import notify, ops
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 31
    f.sample_once()
    st = dict(f.status(), type="rstatus", t=time.time())
    monkeypatch.setattr(ops, "unit_state", lambda u: ("active", time.time() - 60))
    monkeypatch.setattr(ops, "events_since", lambda journal, since: [st])
    txt = ops.format_rewards()
    assert "pmbot-rewards [paper]" in txt and "где мы одни в зоне" in txt and "осторожно" in txt and "хеджей всего 0" in txt
    block = "\n".join(notify._rewards_block("pmbot-rewards"))
    assert "Награды" in block and "награды, оценка" in block and "итог осторожно" in block and "бумага" in block
    monkeypatch.setattr(ops, "unit_state", lambda u: ("inactive", None))
    monkeypatch.setattr(ops, "events_since", lambda journal, since: [])
    assert ops.format_rewards() == "" and notify._rewards_block("pmbot-rewards") == []      # never ran: not shown


def test_kill_message_of_the_farmer_says_it_resumes_by_itself():
    from pmbot.notify import event_message
    msg = event_message("kill", {"reason": "награды: дневной убыток по позициям -51.00$ (лимит 50$)"}, unit="pmbot-rewards")
    assert "сами в 00:00 UTC" in msg and "/resume не нужен" in msg


def test_report_script_reads_a_journal(tmp_path, capsys):
    import importlib.util
    import json
    import os
    from pmbot import ops
    f = farmer_with_market(tmp_path)
    contested(f)
    asyncio.run(f.quote_once())
    f.clock.t += 31
    f.feed.last_msg = f.clock()
    f.sample_once()
    f.on_trade("Y", "SELL", 0.47, 50, int(f.clock() * 1000))
    f.clock.t += 1
    f.feed.books["N"].asks = {0.53: 100.0}
    asyncio.run(f.quote_once())
    f.journal.log("rstatus", **f.status())
    run = tmp_path / "journal" / "20261111-000000"
    run.mkdir(parents=True)
    with open(run / "events.jsonl", "w") as out:
        for e in f.journal.ev:
            out.write(json.dumps({"t": f.clock(), **e}) + "\n")
    spec = importlib.util.spec_from_file_location("report_rewards", os.path.join(ops.REPO, "scripts", "report_rewards.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(str(tmp_path / "journal"))
    txt = capsys.readouterr().out
    assert "hedges: 1, 20 shares" in txt and "CAUTIOUS" in txt and "fills: 1" in txt and "official $" in txt
