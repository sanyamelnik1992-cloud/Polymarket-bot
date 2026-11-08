"""Liquidity-rewards farmer: scoring formula, quote placement, selection filters, fills, rewards, risk, state."""
import asyncio
import time

import pytest

from pmbot.rewards import markets as mk
from pmbot.rewards.config import RewardsCfg, load_rewards_config
from pmbot.rewards.farmer import Farmer, Position
from pmbot.rewards.scoring import adj_price, order_score, our_share, plan_quotes, q_min


class J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append({"type": kind, **kw})


def test_docs_worked_example():
    # Polymarket docs: adjusted mid 0.50, max spread 3c; 100@0.49, 200@0.48 bids and 100 asks at 0.51 on the complement
    q_one = order_score(3, 1, 100) + order_score(3, 2, 200) + order_score(3, 1, 100)
    assert q_one == pytest.approx((2 / 3) ** 2 * 100 + (1 / 3) ** 2 * 200 + (2 / 3) ** 2 * 100)
    assert order_score(3, 3, 100) == 0 and order_score(3, 4, 100) == 0


def test_q_min_single_sided_rules():
    assert q_min(90, 0, 0.5) == pytest.approx(30)        # one side counts at a third inside [0.10, 0.90]
    assert q_min(90, 0, 0.95) == 0                        # outside: two-sided only
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


def test_share_empty_book_and_competition():
    share, mid, q_ours, q_comp = our_share([], [], (0.49, 20), (0.51, 20), 4.5, 20)
    assert share == 1 and mid == pytest.approx(0.50) and q_comp == 0
    share, *_ = our_share([(0.49, 20)], [(0.51, 20)], (0.49, 20), (0.51, 20), 4.5, 20)
    assert share == pytest.approx(0.5)
    share, *_ = our_share([], [], None, None, 4.5, 20)
    assert share == 0


def raw_market(slug, rate=50, price=0.5, days=10, q="Will X happen?", v=4.5, min_size=20):
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
    return {"market_slug": slug, "question": q, "condition_id": "c-" + slug, "rewards_max_spread": v,
            "rewards_min_size": min_size, "end_date": end, "rewards_config": [{"rate_per_day": rate}],
            "tokens": [{"token_id": slug + "-Y", "price": price}, {"token_id": slug + "-N", "price": 1 - price}]}


def test_candidates_filters():
    rc = RewardsCfg()
    raw = [raw_market("ok"), raw_market("small", rate=5), raw_market("soon", days=1),
           raw_market("cheap", price=0.03), raw_market("sport", q="Lakers vs. Celtics"),
           raw_market("crypto", q="Bitcoin Up or Down on November 9?")]
    assert [c.slug for c in mk.candidates(raw, rc)] == ["ok"]


def test_select_prices_and_caps_empty_books():
    rc = RewardsCfg(max_markets=2, max_empty_frac=0.5, quote_budget_usd=1000)
    raw = [raw_market("a"), raw_market("b"), raw_market("c")]
    books = {"a-Y": {"bids": [], "asks": [], "tick_size": "0.01"},
             "b-Y": {"bids": [], "asks": [], "tick_size": "0.01"},
             "c-Y": {"bids": [{"price": "0.49", "size": "40"}], "asks": [{"price": "0.51", "size": "40"}],
                     "tick_size": "0.01"}}
    j = J()
    picks = mk.select(rc, j, raw_markets=raw, books=books)
    assert len(picks) == 2 and sum(p.empty for p in picks) == 1     # only one empty-book market allowed
    assert any(p.slug == "c" for p in picks)
    assert j.ev[0]["type"] == "select" and j.ev[0]["picked"] == 2


def farmer_with_market(tmp_path, **cfg):
    rc = RewardsCfg(state_file=str(tmp_path / "state.json"), **cfg)
    f = Farmer(rc, J())
    rm = mk.RMarket(slug="m", question="q", condition_id="c", yes="Y", no="N", rate=144.0, v=4.5, min_size=20,
                    end_ts=time.time() + 10 * 86400, price=0.50, size=20)
    f.apply_selection([rm])
    b = f.feed.books["Y"]
    b.bids, b.asks, b.last_rx = {0.30: 100.0}, {0.70: 100.0}, 1      # wide book: nobody inside the reward zone
    return f


def test_quotes_fill_cooldown_and_position(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    ms = f.markets["m"]
    assert ms.orders["Y"]["price"] == 0.49 and ms.orders["N"]["price"] == 0.49     # YES bid 0.49, NO bid 0.49
    # a taker sells YES at 0.40 -- through our 0.49 bid
    ts = int(time.time() * 1000) + 1000
    f.on_trade("Y", "SELL", 0.40, 50, ts)
    assert f.pos["m"].yes == 20 and f.pos["m"].cost == pytest.approx(9.8)
    assert ms.last_trade == 0.40 and ms.cooldown_until > time.time()
    assert f.targets(ms, time.time()) == {}                      # cooling down: no quotes
    asyncio.run(f.quote_once())
    assert not ms.orders                                          # the NO bid was pulled too


def test_sample_credits_share_of_pool(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    for o in f.broker.orders.values():
        o.live_from = 0                                           # skip the simulated placement latency
    inc = f.sample_once()
    assert inc == pytest.approx(144.0 / 1440)                    # alone in the zone: the whole per-minute pool


def test_position_value_counts_pairs_at_one_dollar():
    p = Position(yes=30, no=20, cost=24)
    assert p.value(0.4) == pytest.approx(20 + 10 * 0.4)


def test_daily_loss_halts_quotes(tmp_path):
    f = farmer_with_market(tmp_path, daily_loss_limit=5)
    f.pos["m"] = Position(yes=100, no=0, cost=50)                 # mark at mid 0.50 (book 0.30/0.70 is wide -> ref)
    st = f.status()
    f.day_base = st["mtm_pnl"] + 10                               # pretend we were 10$ better at the start of the day
    f.check_risk(f.status())
    assert f.halted_until > time.time()
    assert f.targets(f.markets["m"], time.time()) == {}
    assert any(e["type"] == "kill" for e in f.journal.ev)


def test_state_roundtrip(tmp_path):
    f = farmer_with_market(tmp_path)
    f.pos["m"] = Position(yes=20, no=5, cost=12.5)
    f.rewards_by_day["2026-11-08"] = 3.5
    f._save()
    g = Farmer(f.rc, J())
    assert g.pos["m"].yes == 20 and g.rewards_by_day["2026-11-08"] == 3.5
    assert g.meta["m"]["yes"] == "Y"


def test_config_is_paper_only(tmp_path):
    c = load_rewards_config("config/rewards.toml")
    assert c.mode == "paper"
    p = tmp_path / "live.toml"
    p.write_text('[rewards]\nmode = "live"\n')
    with pytest.raises(ValueError):
        load_rewards_config(str(p))


def test_zero_size_print_is_not_a_fill(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.on_trade("Y", "SELL", 0.40, 0.0, int(time.time() * 1000) + 1000)
    assert "m" not in f.pos and f.markets["m"].cooldown_until == 0
