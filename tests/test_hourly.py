import asyncio
import types

import pytest

from pmbot.bot.hourly import HourlyEngine, decide, load_hourly_config, prob_up_hourly
from pmbot.bot.journal import Journal
from pmbot.bot.live import CONFIRM_PHRASE, LiveGateError, TakerLiveBroker
from pmbot.bot.state import Book


def book(bid, ask, size=100):
    return Book(bids={bid: size}, asks={ask: size}, last_rx=1)


def test_prob_up_hourly_behaviour():
    assert prob_up_hourly(100, 100, 1e-4, 600) == pytest.approx(0.5)
    hi = prob_up_hourly(100.1, 100, 1e-4, 600)
    lo = prob_up_hourly(100.1, 100, 1e-4, 60)
    assert 0.5 < hi < lo < 1  # same lead, less time left -> more certain


def test_decide_edge_bounds_and_no_flip():
    cfg = load_hourly_config(None, {"min_edge": 0.08})
    # p_up 0.75, Up ask 0.60 -> edge 0.75-0.60-fee(0.0168) = 0.133
    d = decide(0.75, book(0.59, 0.60), book(0.39, 0.41), cfg)
    assert d[0] == "U" and d[1] == 0.60 and d[2] == pytest.approx(0.75 - 0.60 - 0.07 * 0.6 * 0.4)
    assert decide(0.65, book(0.59, 0.60), book(0.39, 0.41), cfg) is None        # edge < 8c
    assert decide(0.75, book(0.59, 0.60), book(0.39, 0.41), cfg, pos_side="D") is None  # never flip
    assert decide(0.999, book(0.97, 0.98), book(0.01, 0.02), cfg) is None      # ask above max_price


def test_hourly_config_rejects_unknown_and_bad_limits():
    with pytest.raises(ValueError):
        load_hourly_config(None, {"min_edgee": 0.1})
    with pytest.raises(ValueError):
        load_hourly_config(None, {"size_usd": 50, "max_cost_per_market": 10})


def test_paper_taker_fill_walks_asks_within_limit(tmp_path):
    cfg = load_hourly_config(None, {"size_usd": 10})
    eng = HourlyEngine(cfg, Journal(str(tmp_path)))
    eng.st.books["T"] = Book(bids={0.5: 10}, asks={0.60: 5, 0.61: 100, 0.70: 100})
    eng.pending.append((0, "btc-1800000000", "T", "U", 0.61))
    eng._process_pending(1)
    hp = eng.pos["btc-1800000000"]
    assert hp.side == "U" and hp.cost == pytest.approx(10, abs=1e-6)
    assert 16 < hp.shares < 16.5          # 5 @0.60 then the rest @0.61, never @0.70


def test_taker_live_broker_gates():
    ok_geo = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": False})  # noqa: E731
    with pytest.raises(LiveGateError):
        asyncio.run(TakerLiveBroker(20, geoblock_get=ok_geo, env={}).start())

    class C:
        async def get_balance_allowance(self, asset_type):
            return types.SimpleNamespace(balance=5_000_000)

    env = {"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0x1"}
    with pytest.raises(LiveGateError, match="collateral"):
        asyncio.run(TakerLiveBroker(20, client=C(), geoblock_get=ok_geo, env=env).start())
