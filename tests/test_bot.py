import asyncio
import os
import types

import pytest

from pmbot.bot.config import BotConfig, load_config
from pmbot.bot.fair import fair_value
from pmbot.bot.filters import check
from pmbot.bot.live import CONFIRM_PHRASE, LiveBroker, LiveGateError, check_env, check_geoblock
from pmbot.bot.paper import PaperBroker
from pmbot.bot.risk import RiskManager
from pmbot.bot.state import AssetPrices, Book, State, Window
from pmbot.bot.strategy import Position, target_quotes

T0 = 1_800_000_000  # window start (s)


def mk_window():
    return Window(asset="btc", start=T0, slug="btc-updown-5m-x", condition_id="c", token_up="U1", token_down="D1",
                  strike=100.0, strike_src="gamma")


def book(bid, ask, size=100):
    return Book(bids={bid: size}, asks={ask: size}, last_rx=1)


def mk_state(now_ms, cl=100.0, bn=100.0):
    st = State(["btc"])
    w = st.add_window(mk_window())
    st.books["U1"] = book(0.49, 0.50)
    st.books["D1"] = book(0.50, 0.51)
    px = st.prices["btc"]
    px.var = (0.5e-4) ** 2
    for i in range(30):
        px.on_binance(bn, now_ms - 3000 + i * 100)
    px.on_chainlink(cl, now_ms - 1500, now_ms - 100)
    st.feed_alive["clob"]["btc"] = now_ms
    return st, w


# ---------------------------------------------------------------- config
def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[risk]\nmax_daly_loss = 10\n")
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(str(p))


def test_config_validates_limits():
    with pytest.raises(ValueError):
        load_config(None, {"strategy": {"quote_size": 2}})
    with pytest.raises(ValueError):
        load_config(None, {"risk": {"max_cost_per_window": 500, "max_open_worst_loss": 100}})
    with pytest.raises(ValueError):
        load_config(None, {"execution": {"mode": "yolo"}})


# ---------------------------------------------------------------- strategy
def test_quotes_are_below_fair_postonly_and_join_only():
    cfg = BotConfig()
    w = mk_window()
    q = target_quotes(w, 0.60, book(0.58, 0.59), book(0.41, 0.42), Position(), cfg.strategy, cfg.risk)
    # fair-h = 0.56 for Up (below best bid 0.58 -> stays 0.56), 0.36 for Down
    assert q["U"] == (0.56, 10.0)
    assert q["D"] == (0.36, 10.0)
    # fair far above the book: join_only caps at best bid, post-only below best ask
    q = target_quotes(w, 0.90, book(0.58, 0.59), book(0.41, 0.42), Position(), cfg.strategy, cfg.risk)
    assert q["U"][0] == 0.58


def test_quotes_respect_inventory_and_budget():
    cfg = BotConfig()
    w = mk_window()
    pos = Position(up=50, down=0, cost=25)
    q = target_quotes(w, 0.5, book(0.49, 0.50), book(0.50, 0.51), pos, cfg.strategy, cfg.risk)
    assert q["U"] is None                        # net long Up at the cap -> no more Up
    assert q["D"] is not None and q["D"][1] <= 10  # $5 budget left at ~0.46 -> 10 shares max
    pos = Position(up=0, down=0, cost=29.0)      # only $1 left -> below min size
    q = target_quotes(w, 0.5, book(0.49, 0.50), book(0.50, 0.51), pos, cfg.strategy, cfg.risk)
    assert q["U"] is None and q["D"] is None


def test_skew_lowers_bid_of_the_side_we_are_long():
    cfg = BotConfig()
    cfg.strategy.skew_per_share = 0.002
    w = mk_window()
    q0 = target_quotes(w, 0.5, book(0.49, 0.50), book(0.49, 0.50), Position(), cfg.strategy, cfg.risk)
    q1 = target_quotes(w, 0.5, book(0.49, 0.50), book(0.49, 0.50), Position(up=20, cost=9), cfg.strategy, cfg.risk)
    assert q1["U"][0] < q0["U"][0]
    assert q1["D"][0] >= q0["D"][0]


# ---------------------------------------------------------------- fair value + filters
def test_fair_value_uses_chainlink_and_binance_nowcast():
    now = (T0 + 60) * 1000
    st, w = mk_state(now, cl=100.0, bn=100.0)
    fv = fair_value(w, st.prices["btc"], now)
    assert abs(fv.p_up - 0.5) < 0.02
    # Binance jumps up after the Chainlink print -> nowcast spot moves up -> P(Up) rises
    st.prices["btc"].on_binance(100.05, now)
    fv2 = fair_value(w, st.prices["btc"], now)
    assert fv2.spot > 100.0 and fv2.p_up > fv.p_up + 0.1


def test_filters_pass_in_normal_conditions_and_fail_on_stale_feeds():
    now = (T0 + 60) * 1000
    st, w = mk_state(now)
    cfg = BotConfig()
    fv = fair_value(w, st.prices["btc"], now)
    assert check(w, st, fv, now, cfg.filters, cfg.strategy) == []
    later = now + 10_000  # nothing received for 10s
    r = check(w, st, fv, later, cfg.filters, cfg.strategy)
    assert "chainlink_stale" in r and "binance_stale" in r


def test_filters_timing_spread_disagreement_and_jump():
    now = (T0 + 60) * 1000
    st, w = mk_state(now)
    cfg = BotConfig()
    fv = fair_value(w, st.prices["btc"], now)
    assert "too_early" in check(w, st, fv, (T0 + 1) * 1000, cfg.filters, cfg.strategy)
    assert "too_late" in check(w, st, fv, (T0 + 250) * 1000, cfg.filters, cfg.strategy)
    st.books["U1"] = book(0.40, 0.50)
    assert "wide_spread" in check(w, st, fv, now, cfg.filters, cfg.strategy)
    st.books["U1"] = book(0.89, 0.90)
    assert "model_disagrees" in check(w, st, fv, now, cfg.filters, cfg.strategy)
    st.books["U1"] = book(0.49, 0.50)
    st.prices["btc"].on_binance(100.2, now)  # 20bp in <1s with sigma 0.5bp/s
    assert "jump_cooldown" in check(w, st, fv, now, cfg.filters, cfg.strategy)
    assert "jump_cooldown" in check(w, st, fv, now + 1000, cfg.filters, cfg.strategy)


def test_no_strike_blocks_quoting():
    now = (T0 + 60) * 1000
    st, w = mk_state(now)
    w.strike = None
    cfg = BotConfig()
    r = check(w, st, fair_value(w, st.prices["btc"], now), now, cfg.filters, cfg.strategy)
    assert "no_strike" in r and "no_fair_value" in r


def test_asset_prices_twap_and_ewma():
    px = AssetPrices()
    for s in range(T0 - 60, T0):
        px.on_chainlink(100.0 + (s - (T0 - 60)) * 0.01, s * 1000, s * 1000 + 1400)
    v, n = px.cl_twap(T0)
    assert n == 60 and abs(v - (100.0 + 0.295)) < 1e-9
    px.seed_vol([100, 100.01, 100.0, 100.01])
    assert px.sigma() > 0


# ---------------------------------------------------------------- risk
def test_daily_loss_kill_switch_and_losing_streak():
    cfg = BotConfig()
    cfg.risk.daily_loss_limit = 10
    cfg.risk.max_losing_windows_in_row = 2
    r = RiskManager(cfg.risk)
    r.rc.kill_file = ""
    r.on_fill("a", "U", 0.5, 10, 0.1)
    r.on_settle("a", "Down", rebate_share=0.0)   # -5
    assert r.can_quote()[0]
    r.on_fill("b", "U", 0.5, 12, 0.1)
    r.on_settle("b", "Down", rebate_share=0.0)   # -6 -> -11 total, and 2 losses in a row
    ok, why = r.can_quote()
    assert not ok and "daily loss" in why and r.killed


def test_open_risk_clamp_counts_resting_orders():
    cfg = BotConfig()
    cfg.risk.max_open_worst_loss = 20
    r = RiskManager(cfg.risk)
    r.on_fill("a", "U", 0.5, 20, 0.0)            # worst loss 10
    assert r.open_worst_loss() == pytest.approx(10)
    assert r.clamp_size("b", 0.5, 10, resting_notional=0.0) == 10
    assert r.clamp_size("b", 0.5, 10, resting_notional=8.0) == 4
    assert r.clamp_size("b", 0.5, 10, resting_notional=12.0) == 0


def test_hedged_position_has_small_worst_loss():
    p = Position(up=10, down=10, cost=9.2)
    assert p.worst_loss() == pytest.approx(-0.8)   # locked profit: complete set bought for 0.92


def test_kill_file(tmp_path):
    cfg = BotConfig()
    cfg.risk.kill_file = str(tmp_path / "STOP")
    r = RiskManager(cfg.risk)
    assert r.can_quote()[0]
    open(cfg.risk.kill_file, "w").close()
    assert not r.can_quote()[0] and r.killed


def test_rate_limiter():
    cfg = BotConfig()
    cfg.risk.max_actions_per_sec = 2
    r = RiskManager(cfg.risk)
    assert sum(r.allow_action() for _ in range(20)) <= 5


# ---------------------------------------------------------------- paper broker
def run(coro):
    return asyncio.run(coro)


def test_paper_fill_rules_and_latency():
    fills = []
    pb = PaperBroker(place_latency_ms=100, cancel_latency_ms=100, on_fill=fills.append)
    comp = {"U1": "D1", "D1": "U1"}.get
    oid = run(pb.place("U1", "k", "U", 0.45, 10, now_ms=1000))
    pb.on_public_trade("U1", comp, "SELL", 0.44, 5, ts_ms=1050)   # before live -> no fill
    pb.on_public_trade("U1", comp, "SELL", 0.45, 5, ts_ms=1200)   # same price (queue) -> no fill
    pb.on_public_trade("U1", comp, "BUY", 0.40, 5, ts_ms=1200)    # buy of our token does not hit a bid
    assert fills == []
    pb.on_public_trade("U1", comp, "SELL", 0.44, 4, ts_ms=1200)   # trade-through -> fill 4 @ our price
    assert fills[-1]["size"] == 4 and fills[-1]["price"] == 0.45
    pb.on_public_trade("D1", comp, "BUY", 0.57, 20, ts_ms=1300)   # mint: 1-0.57=0.43 < 0.45 -> fill rest 6
    assert fills[-1]["size"] == 6 and sum(f["size"] for f in fills) == 10
    oid2 = run(pb.place("U1", "k", "U", 0.45, 10, now_ms=2000))
    run(pb.cancel([oid2], now_ms=2200))                           # effective at 2300
    pb.on_public_trade("U1", comp, "SELL", 0.40, 5, ts_ms=2250)   # still live -> fills
    pb.on_public_trade("U1", comp, "SELL", 0.40, 5, ts_ms=2400)   # cancelled -> no fill
    assert sum(f["size"] for f in fills if f["order_id"] == oid2) == 5
    assert oid != oid2


def test_paper_gtd_expiry():
    fills = []
    pb = PaperBroker(0, 0, on_fill=fills.append)
    run(pb.place("U1", "k", "U", 0.45, 10, now_ms=0, expiration_s=200))  # effective expiry 140s
    pb.on_public_trade("U1", {"U1": "D1"}.get, "SELL", 0.4, 5, ts_ms=150_000)
    assert fills == []


# ---------------------------------------------------------------- live gating (no network, no keys)
def test_live_env_gate():
    with pytest.raises(LiveGateError):
        check_env({})
    with pytest.raises(LiveGateError):
        check_env({"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE})
    check_env({"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0xabc"})


def test_live_geoblock_gate():
    def blocked(url, timeout):
        return types.SimpleNamespace(json=lambda: {"blocked": True, "country": "US", "region": "OH"})

    def ok(url, timeout):
        return types.SimpleNamespace(json=lambda: {"blocked": False, "country": "XX"})

    with pytest.raises(LiveGateError, match="blocked"):
        check_geoblock(blocked)
    assert check_geoblock(ok)["country"] == "XX"


class FakeClient:
    def __init__(self, balance_usd):
        self.balance = int(balance_usd * 1e6)
        self.calls = []

    async def get_balance_allowance(self, asset_type):
        return types.SimpleNamespace(balance=self.balance)

    async def cancel_all(self):
        self.calls.append("cancel_all")
        return types.SimpleNamespace(canceled=())

    async def send_order_heartbeat(self, heartbeat_id=""):
        self.calls.append("hb")
        return types.SimpleNamespace(heartbeat_id="h1")

    async def place_limit_order(self, **kw):
        self.calls.append(("place", kw))
        return types.SimpleNamespace(ok=True, order_id="o1")

    async def cancel_orders(self, order_ids):
        self.calls.append(("cancel", tuple(order_ids)))
        return types.SimpleNamespace(canceled=tuple(order_ids), not_canceled={})


def test_live_broker_refuses_without_gates_and_insufficient_balance():
    cfg = BotConfig()
    env = {"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0xabc"}
    ok_geo = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": False})  # noqa: E731
    with pytest.raises(LiveGateError):
        run(LiveBroker(cfg, client=FakeClient(1000), geoblock_get=ok_geo, env={}).start())
    with pytest.raises(LiveGateError, match="balance"):
        run(LiveBroker(cfg, client=FakeClient(10), geoblock_get=ok_geo, env=env).start())


def test_live_broker_orders_are_post_only_gtd_and_cancel_all_on_start():
    cfg = BotConfig()
    env = {"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0xabc"}
    ok_geo = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": False})  # noqa: E731
    fc = FakeClient(1000)
    lb = LiveBroker(cfg, client=fc, geoblock_get=ok_geo, env=env)

    async def scenario():
        lb._user_stream = lambda: asyncio.sleep(0)   # no websocket in tests
        await lb.start()
        oid = await lb.place("U1", "k", "U", 0.45, 10, now_ms=0, expiration_s=1234)
        await lb.cancel([oid], now_ms=0)
        await lb.stop()

    run(scenario())
    assert fc.calls[0] == "cancel_all" and "hb" in fc.calls
    place = [c for c in fc.calls if isinstance(c, tuple) and c[0] == "place"][0][1]
    assert place["post_only"] is True and place["side"] == "BUY" and place["expiration"] == 1234
    assert place["price"] == "0.45" and place["size"] == "10.00"
    assert fc.calls[-1] == "cancel_all"


def test_private_key_never_in_journal(tmp_path):
    from pmbot.bot.journal import Journal
    j = Journal(str(tmp_path))
    cfg = BotConfig()
    env = {"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0xSECRETKEY123"}
    ok_geo = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": False})  # noqa: E731
    lb = LiveBroker(cfg, client=FakeClient(1000), geoblock_get=ok_geo, env=env, journal=j)

    async def scenario():
        lb._user_stream = lambda: asyncio.sleep(0)
        await lb.start()
        await lb.place("U1", "k", "U", 0.45, 10, now_ms=0, expiration_s=1234)
        await lb.stop()

    run(scenario())
    j.close()
    assert "SECRETKEY" not in open(os.path.join(str(tmp_path), "events.jsonl")).read()


# ---------------------------------------------------------------- v2 filters
def v2_cfg():
    return load_config(None, {"filters": {"vol_spike_ratio": 2.0, "max_fair_move": 0.10, "toxic_shares": 10,
                                          "toxic_fills": 2, "toxic_window_ms": 5000, "toxic_cooldown_ms": 30000}})


def test_toxic_flow_blocks_only_the_swept_side():
    from pmbot.bot.filters import side_blocks
    cfg = v2_cfg()
    now = (T0 + 60) * 1000
    st, w = mk_state(now)
    assert side_blocks(w, st, now, cfg.filters) == set()
    st.record_fill(w.key, "U", now - 1000, 4)
    assert side_blocks(w, st, now, cfg.filters) == set()
    st.record_fill(w.key, "U", now - 500, 3)          # 2 fills in 5s -> toxic
    assert side_blocks(w, st, now, cfg.filters) == {"U"}
    assert side_blocks(w, st, now + 20_000, cfg.filters) == {"U"}   # cooldown persists
    assert side_blocks(w, st, now + 31_000, cfg.filters) == set()


def test_fair_unstable_and_vol_spike():
    cfg = v2_cfg()
    now = (T0 + 60) * 1000
    st, w = mk_state(now)
    fv = fair_value(w, st.prices["btc"], now)
    st.record_fair(w.key, now - 3000, fv.p_up + 0.15)
    assert "fair_unstable" in check(w, st, fv, now, cfg.filters, cfg.strategy)
    st2, w2 = mk_state(now)
    px = st2.prices["btc"]
    px.var, px.var_short = (0.5e-4) ** 2, (1.5e-4) ** 2   # short vol 3x long
    fv2 = fair_value(w2, px, now)
    assert "vol_spike" in check(w2, st2, fv2, now, cfg.filters, cfg.strategy)
    px.var_short = (0.6e-4) ** 2
    assert "vol_spike" not in check(w2, st2, fair_value(w2, px, now), now, cfg.filters, cfg.strategy)


def test_v2_config_file_loads():
    c = load_config("config/paper_v2.toml")
    assert c.filters.toxic_fills == 2 and c.risk.max_cost_per_window == 12


def test_live_existing_positions_are_parsed():
    cfg = BotConfig()
    fc = FakeClient(1000)
    fc.wallet = "0xW"
    lb = LiveBroker(cfg, client=fc, env={})
    pages = [{"data": [
        {"slug": "btc-updown-5m-1800000000", "outcome": "Up", "current_size": 12.0, "avg_price": 0.41},
        {"slug": "eth-updown-15m-1800000000", "outcome": "Up", "current_size": 5.0, "avg_price": 0.5},   # not 5m
        {"slug": "xrp-updown-5m-1800000000", "outcome": "Down", "current_size": 5.0, "avg_price": 0.5},  # not traded
        {"slug": "will-x-happen", "outcome": "Yes", "current_size": 5.0, "avg_price": 0.5},
    ], "pagination": {"has_more": False}}]
    calls = []

    def fake_get(url, params):
        calls.append(params)
        return pages.pop(0)

    rows = lb.existing_positions(["btc", "eth", "sol"], get_json=fake_get)
    assert rows == [("btc", 1800000000, "U", 12.0, 0.41, "btc-updown-5m-1800000000")]
    assert calls[0]["user"] == "0xW" and calls[0]["status"] == "OPEN"


def test_risk_state_survives_restart(tmp_path):
    cfg = BotConfig()
    cfg.risk.state_file = str(tmp_path / "risk.json")
    cfg.risk.kill_file = ""
    cfg.risk.daily_loss_limit = 5
    r = RiskManager(cfg.risk)
    r.on_fill("a", "U", 0.5, 20, 0.0)
    r.on_settle("a", "Down", rebate_share=0.0)        # -10 -> over the limit
    assert not r.can_quote()[0] and r.killed
    r2 = RiskManager(cfg.risk)                          # "restart"
    assert r2.killed and not r2.can_quote()[0]
    assert r2.realized_by_day[r2.today()] == pytest.approx(-10)


def test_live_requires_state_file():
    with pytest.raises(ValueError, match="state_file"):
        load_config(None, {"execution": {"mode": "live"}})
    assert load_config("config/live.example.toml").risk.state_file
