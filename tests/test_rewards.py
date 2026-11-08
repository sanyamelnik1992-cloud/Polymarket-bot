"""Liquidity-rewards farmer: scoring formula, selection, quoting, fills, rewards, risk, settlement, state."""
import asyncio

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


def test_share_empty_book_and_competition():
    share, mid, q_ours, q_comp = our_share([], [], (0.49, 20), (0.51, 20), 4.5, 20)
    assert share == 1 and mid == pytest.approx(0.50) and q_comp == 0
    share, *_ = our_share([(0.49, 20)], [(0.51, 20)], (0.49, 20), (0.51, 20), 4.5, 20)
    assert share == pytest.approx(0.5)
    share, *_ = our_share([], [], None, None, 4.5, 20)
    assert share == 0


# ---------------------------------------------------------------- selection
def raw_market(slug, rate=50, price=0.5, days=10, q="Will X happen?", v=4.5, min_size=20, cid=None):
    import time
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
    return {"market_slug": slug, "question": q, "condition_id": cid or "c-" + slug, "rewards_max_spread": v,
            "rewards_min_size": min_size, "end_date": end,
            "rewards_config": [{"rate_per_day": rate, "start_date": "2000-01-01", "end_date": "2500-12-31"}],
            "tokens": [{"token_id": slug + "-Y", "price": price}, {"token_id": slug + "-N", "price": 1 - price}]}


def test_candidates_filters():
    rc = RewardsCfg()
    raw = [raw_market("ok"), raw_market("small", rate=5), raw_market("soon", days=1),
           raw_market("cheap", price=0.03), raw_market("sport", q="Lakers vs. Celtics"),
           raw_market("crypto", q="Bitcoin Up or Down on November 9?")]
    assert [c.slug for c in mk.candidates(raw, rc)] == ["ok"]


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
    picks = mk.select(rc, j, raw_markets=raw, books=books)
    assert len(picks) == 2 and sum(p.empty for p in picks) == 1     # only one empty-book market allowed
    assert any(p.slug == "c" for p in picks)
    assert j.ev[0]["type"] == "select" and j.ev[0]["picked"] == 2


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
                    end_ts=f.clock() + 10 * 86400, price=0.50, size=20)
    f.apply_selection([rm])
    b = f.feed.books["Y"]
    b.bids, b.asks, b.last_rx = {0.30: 100.0}, {0.70: 100.0}, 1      # wide book: nobody inside the reward zone
    f.feed.last_msg = f.clock()                                       # the market websocket is alive
    return f


def test_quotes_fill_cooldown_and_position(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    ms = f.markets["m"]
    assert ms.orders["Y"]["price"] == 0.49 and ms.orders["N"]["price"] == 0.49     # YES bid 0.49, NO bid 0.49
    f.clock.t += 1
    f.on_trade("Y", "SELL", 0.40, 50, int(f.clock() * 1000))        # a taker sells YES through our bid
    assert f.pos["m"].yes == 20 and f.pos["m"].cost == pytest.approx(9.8)
    assert ms.cooldown_until > f.clock()
    assert f.targets(ms, f.clock()) == {}                            # cooling down: no quotes
    asyncio.run(f.quote_once())
    assert not ms.orders                                              # the NO bid was pulled too


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


def test_crossing_order_is_replaced_at_once(tmp_path):
    """Our YES bid at 0.49 must not stay after the book moves so that the best ask is 0.49 (it would be a taker)."""
    f = farmer_with_market(tmp_path, requote_min_s=999)              # a long re-quote timer must not hold it
    asyncio.run(f.quote_once())
    assert f.markets["m"].orders["Y"]["price"] == 0.49
    b = f.feed.books["Y"]
    b.asks = {0.49: 50.0, 0.70: 100.0}                               # the ask moved down onto our bid
    asyncio.run(f.quote_once())
    assert f.markets["m"].orders["Y"]["price"] < 0.49


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
    assert f.mfills["m"] == 2 and f.targets(f.markets["m"], f.clock()) == {}
    g = Farmer(f.rc, J(), clock=f.clock)                              # restart from the state file
    assert g.mfills.get("m") == 2


# ---------------------------------------------------------------- rewards
def test_sample_credits_share_of_pool(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 1                                                    # orders go live after the 150 ms latency
    inc = f.sample_once()
    assert inc == pytest.approx(144.0 * 60 / 86400)                   # alone in the zone: the whole pool, one minute


def test_accrual_is_time_weighted(tmp_path):
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 1
    f.sample_once()                                                   # first sample credits one nominal minute
    f.clock.t += 30
    f.feed.last_msg = f.clock()
    inc = f.sample_once()
    assert inc == pytest.approx(144.0 * 30 / 86400)                   # the real 30 s, not a fixed minute


def test_stress_is_half_the_estimate_when_alone(tmp_path):
    """Alone in the zone the estimate is the whole pool; if one identical maker joined we would get half of it."""
    f = farmer_with_market(tmp_path)
    asyncio.run(f.quote_once())
    f.clock.t += 1
    f.sample_once()
    st = f.status()
    assert st["rewards_total"] > 0
    assert st["stress_total"] == pytest.approx(st["rewards_total"] / 2)


def test_position_value_counts_pairs_at_one_dollar():
    p = Position(yes=30, no=20, cost=24)
    assert p.value(0.4) == pytest.approx(20 + 10 * 0.4)


# ---------------------------------------------------------------- risk
def test_daily_loss_halts_quotes(tmp_path):
    f = farmer_with_market(tmp_path, daily_loss_limit=5)
    f.pos["m"] = Position(yes=100, no=0, cost=50)                    # marks at the 0.50 reference (wide book)
    st = f.status()
    f.day_base = st["trading_pnl"] + 10                               # as if we were 10$ better this morning
    f.check_risk(f.status())
    assert f.halted_until > f.clock()
    assert f.targets(f.markets["m"], f.clock()) == {}
    assert any(e["type"] == "kill" for e in f.journal.ev)


def test_inventory_cap_removes_the_side_that_adds_to_it(tmp_path):
    f = farmer_with_market(tmp_path, max_inventory_usd_per_market=10)
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
    f.realized = 1.25
    f.mfills["m"] = 2
    f._save()
    g = Farmer(f.rc, J(), clock=f.clock)
    assert g.pos["m"].yes == 20 and g.rewards_by_day["2026-11-08"] == 3.5
    assert g.meta["m"]["yes"] == "Y" and g.realized == 1.25 and g.mfills["m"] == 2


def test_config_is_paper_only(tmp_path):
    c = load_rewards_config("config/rewards.toml")
    assert c.mode == "paper"
    p = tmp_path / "live.toml"
    p.write_text('[rewards]\nmode = "live"\n')
    with pytest.raises(ValueError):
        load_rewards_config(str(p))
