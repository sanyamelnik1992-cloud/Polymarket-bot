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
        {"slug": "btc-updown-5m-1794132000", "outcome": "Up", "current_size": 5.0, "avg_price": 0.64,
         "redeemable": True},                                              # ended window: worthless leftover
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


def test_fast_react_wakes_on_binance_move():
    from pmbot.bot.engine import Engine
    from pmbot.bot.journal import Journal
    import tempfile
    cfg = load_config(None, {"strategy": {"assets": ["btc"]}})
    eng = Engine(cfg, lambda on_fill, journal: PaperBroker(on_fill=on_fill), Journal(tempfile.mkdtemp()))
    px = eng.st.prices["btc"]
    px.var = (0.5e-4) ** 2
    px.on_binance(100.0, 1000)
    eng._decided_mid["btc"] = 100.0
    eng.st.on_tick("btc")
    assert not eng._wake.is_set()                 # no move
    px.on_binance(100.002, 1100)                  # 0.2bp < 1 sigma (0.5bp)
    eng.st.on_tick("btc")
    assert not eng._wake.is_set()
    px.on_binance(100.01, 1200)                   # 1bp > 1 sigma -> wake now
    eng.st.on_tick("btc")
    assert eng._wake.is_set() and eng.stats["fast_wakes"] == 1


# ---------------------------------------------------------------- live hardening (first $10 test)
def test_geoblock_allows_api_only_countries():
    ie = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": True, "country": "IE"})  # noqa: E731
    gb = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": True, "country": "GB"})  # noqa: E731
    assert check_geoblock(ie)["country"] == "IE"
    with pytest.raises(LiveGateError, match="blocked"):
        check_geoblock(gb)


@pytest.mark.parametrize("live_path,twin_path", [("config/live_d.toml", "config/live_d_twin.toml"),
                                                 ("config/live_v3.toml", "config/v3.toml")])
def test_live_configs_load_and_twin_matches_live(live_path, twin_path):
    live, twin = load_config(live_path), load_config(twin_path)
    assert live.execution.mode == "live" and twin.execution.mode == "paper"
    assert live.strategy == twin.strategy and live.filters == twin.filters
    for k in ("max_net_shares_per_window", "max_cost_per_window", "max_open_worst_loss", "daily_loss_limit",
              "max_actions_per_sec", "max_losing_windows_in_row"):
        assert getattr(live.risk, k) == getattr(twin.risk, k)
    assert live.risk.state_file != twin.risk.state_file and not live.risk.resume_next_day
    assert live.risk.max_open_worst_loss <= 10 and live.risk.daily_loss_limit <= 5 and live.strategy.quote_size == 5
    with pytest.raises(ValueError, match="paper-only"):
        load_config(live_path, {"risk": {"resume_next_day": True}})


def test_paper_control_resumes_next_day_live_does_not(tmp_path):
    clock = [T0]
    for resume in (True, False):
        cfg = BotConfig()
        cfg.risk.state_file = str(tmp_path / f"r{resume}.json")
        cfg.risk.kill_file = ""
        cfg.risk.daily_loss_limit = 5
        cfg.risk.resume_next_day = resume
        clock[0] = T0
        r = RiskManager(cfg.risk, clock=lambda: clock[0])
        r.on_fill("a", "U", 0.5, 20, 0.0)
        r.on_settle("a", "Down", rebate_share=0.0)
        assert not r.can_quote()[0] and r.killed
        assert r.stays_alive_when_killed == resume
        clock[0] = T0 + 86400
        assert r.can_quote()[0] == resume


def test_live_balance_gate_uses_window_budget():
    cfg = load_config("config/live_d.toml")
    env = {"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0xabc"}
    ok_geo = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": False})  # noqa: E731
    with pytest.raises(LiveGateError, match="balance"):
        run(LiveBroker(cfg, client=FakeClient(4), geoblock_get=ok_geo, env=env).start())
    lb = LiveBroker(cfg, client=FakeClient(7), geoblock_get=ok_geo, env=env)

    async def scenario():
        lb._user_stream = lambda: asyncio.sleep(0)
        await lb.start()
        await lb.stop()
    run(scenario())
    assert lb.balance_usd == pytest.approx(7)


class Rejected(Exception):
    def __init__(self, status):
        super().__init__("forbidden")
        self.status = status


def test_live_broker_health_and_fatal_on_403():
    cfg = load_config("config/live_d.toml")
    lb = LiveBroker(cfg, client=FakeClient(100), env={})
    assert lb.health() == (False, "user_stream_down")
    lb.stream_ok = True
    assert lb.health()[0]
    lb.note_error("place", RuntimeError("timeout"))
    assert lb.health() == (False, "api_backoff") and not lb.fatal
    lb.note_error("place", Rejected(403))
    assert lb.fatal and lb.health() == (False, "broker_fatal")


def test_late_fill_after_cancel_is_still_counted():
    """cancel says 'not canceled' (matched) and the fill event arrives afterwards: the position must count it."""
    fills = []
    fc = FakeClient(100)

    async def nc(order_ids):
        return types.SimpleNamespace(canceled=(), not_canceled={o: "matched" for o in order_ids})
    fc.cancel_orders = nc
    lb = LiveBroker(load_config("config/live_d.toml"), client=fc, env={}, on_fill=fills.append)

    async def scenario():
        oid = await lb.place("U1", "k", "U", 0.45, 5, now_ms=0, expiration_s=1234)
        await lb.cancel([oid], now_ms=0)
        assert oid not in lb.orders and oid in lb.known
        mo = types.SimpleNamespace(order_id=oid, matched_amount="5", price="0.45")
        ev = types.SimpleNamespace(type="trade", payload=types.SimpleNamespace(
            id="t1", status="MATCHED", maker_orders=(mo,), matched_at=None))

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                lb._stopping = True

            def __aiter__(self):
                async def gen():
                    yield ev
                return gen()

        async def subscribe(specs):
            return Stream()
        fc.subscribe = subscribe
        await lb._user_stream()
    run(scenario())
    assert len(fills) == 1 and fills[0]["size"] == 5 and fills[0]["key"] == "k"


def test_engine_survives_order_errors_and_resets():
    from pmbot.bot.engine import Engine
    from pmbot.bot.journal import Journal
    import tempfile
    cfg = load_config(None, {"strategy": {"assets": ["btc"]}})

    class Flaky(PaperBroker):
        fail = True
        resets = 0

        async def place(self, *a, **kw):
            if self.fail:
                raise RuntimeError("network down")
            return await super().place(*a, **kw)

        async def cancel_all(self, now_ms=None):
            Flaky.resets += 1
            await super().cancel_all(now_ms)

    eng = Engine(cfg, lambda on_fill, journal: Flaky(on_fill=on_fill), Journal(tempfile.mkdtemp()))
    w = mk_window()
    eng.st.add_window(w)
    run(eng.set_quote(w, "U", (0.45, 10.0), T0 * 1000 + 10_000))
    assert eng._need_reset and eng.stats["order_errors"] == 1 and not eng.quotes
    run(eng._reset_orders(T0 * 1000 + 10_050))
    assert not eng._need_reset and Flaky.resets == 1


def test_loop_lag_filter_blocks_quoting():
    now = T0 * 1000 + 30_000
    st, w = mk_state(now)
    cfg = load_config(None, {"filters": {"max_loop_lag_ms": 200}})
    fv = fair_value(w, st.prices["btc"], now)
    assert "loop_lag" not in check(w, st, fv, now, cfg.filters, cfg.strategy)
    st.lag_block_until = now + 500
    assert "loop_lag" in check(w, st, fv, now, cfg.filters, cfg.strategy)


def test_post_only_cross_400_is_a_plain_rejection():
    fc = FakeClient(100)

    async def cross(**kw):
        raise Rejected(400)
    fc.place_limit_order = cross
    lb = LiveBroker(load_config("config/live_d.toml"), client=fc, env={})
    lb.stream_ok = True
    assert run(lb.place("U1", "k", "U", 0.45, 5, now_ms=0, expiration_s=1234)) is None
    assert lb.health()[0] and not lb.fatal          # no backoff, no reset


def test_matched_on_cancel_pauses_that_side_and_skips_rebid():
    from pmbot.bot.engine import Engine
    from pmbot.bot.journal import Journal
    import tempfile
    cfg = load_config(None, {"strategy": {"assets": ["btc"]}})
    placed = []

    class HitBroker(PaperBroker):
        async def place(self, *a, **kw):
            placed.append(a)
            return await super().place(*a, **kw)

        async def cancel(self, oids, now_ms):
            await super().cancel(oids, now_ms)
            return set(oids)                      # "already canceled or matched"

    eng = Engine(cfg, lambda on_fill, journal: HitBroker(on_fill=on_fill), Journal(tempfile.mkdtemp()))
    w = mk_window()
    eng.st.add_window(w)
    now = T0 * 1000 + 10_000
    run(eng.set_quote(w, "U", (0.45, 10.0), now))
    run(eng.set_quote(w, "U", (0.44, 10.0), now + 100))       # requote down -> cancel answers "matched"
    assert len(placed) == 1 and ("btc-%d" % T0, "U") not in eng.quotes
    assert eng.st.side_block_until[(w.key, "U")] >= now + 100 + cfg.execution.hit_pause_ms


def test_routine_cross_rejection_is_not_an_alert():
    from pmbot.bot.alerts import TelegramAlerts
    a = TelegramAlerts(token="t", chat="c", mode="live")
    a.enabled = False
    assert a.render("order_rejected", {"code": "post_only_would_cross"}) is None
    assert a.render("broker_fatal", {"op": "place", "status": 403, "error": "x"})


def test_restart_does_not_settle_a_window_twice(tmp_path):
    """Live 8 Nov: after a restart the wallet's losing tokens of an already settled window were loaded as an
    existing position and settled again (-2.10 phantom loss in the daily counter)."""
    cfg = BotConfig()
    cfg.risk.state_file = str(tmp_path / "r.json")
    cfg.risk.kill_file = ""
    r = RiskManager(cfg.risk)
    r.on_fill("btc-1", "D", 0.42, 5, 0.0)
    assert r.on_settle("btc-1", "Up", rebate_share=0.0)["pnl"] == pytest.approx(-2.1)
    r2 = RiskManager(cfg.risk)                               # restart
    assert r2.is_settled("btc-1")
    r2.on_fill("btc-1", "D", 0.42, 5, 0.0)                    # leftover tokens loaded again
    assert r2.on_settle("btc-1", "Up", rebate_share=0.0) is None
    assert r2.realized_by_day[r2.today()] == pytest.approx(-2.1) and not r2.unresolved


# ---------------------------------------------------------------- v3 microstructure (reports/40)
def test_v3_config_loads_and_quotes_at_the_best_bid():
    cfg = load_config("config/v3.toml")
    assert cfg.execution.mode == "paper" and cfg.execution.paper_queue and cfg.filters.min_queue_imbalance == 0.30
    w = mk_window()
    pos = Position()
    bu, bd = book(0.55, 0.56), book(0.44, 0.45)
    q = target_quotes(w, 0.555, bu, bd, pos, cfg.strategy, cfg.risk)
    assert q["U"][0] == 0.55 and q["D"][0] == 0.44               # both at the touch, both inside the 0.40-0.60 band
    live = load_config("config/live_d.toml")
    q = target_quotes(w, 0.555, bu, bd, pos, live.strategy, live.risk)
    assert q["U"][0] == 0.54 and q["D"][0] == 0.43               # the D/live config rests one tick below


def test_queue_imbalance_guard_with_hysteresis():
    from pmbot.bot.filters import imbalance_blocks, queue_imbalance
    cfg = load_config("config/v3.toml")
    st, w = mk_state(T0 * 1000 + 30_000)
    st.books["U1"] = Book(bids={0.55: 10}, asks={0.56: 190}, last_rx=1)     # thin bid: I = 0.05
    st.books["D1"] = Book(bids={0.44: 190}, asks={0.45: 10}, last_rx=1)     # thick bid: I = 0.95
    cfg.filters.min_queue_imbalance, cfg.filters.queue_imbalance_hysteresis = 0.35, 0.10
    assert queue_imbalance(st.books["U1"]) == pytest.approx(0.05)
    assert imbalance_blocks(w, st, cfg.filters, {}, {}) == {"U"}
    st.books["U1"] = Book(bids={0.55: 40}, asks={0.56: 60}, last_rx=1)      # I = 0.40
    assert "U" in imbalance_blocks(w, st, cfg.filters, {"U": False}, {})     # not quoting: needs 0.45
    assert "U" not in imbalance_blocks(w, st, cfg.filters, {"U": True}, {})  # quoting: keeps until 0.35
    assert "U" in imbalance_blocks(w, st, cfg.filters, {"U": True}, {"U": 10})  # own size excluded: 30/90


def test_paper_queue_model_fills_only_after_the_queue_ahead():
    fills = []
    pb = PaperBroker(0, 0, on_fill=fills.append, queue=True)
    run(pb.place("U1", "k", "U", 0.55, 5, now_ms=0, queue_ahead=20))
    comp = lambda t: "D1" if t == "U1" else "U1"   # noqa: E731
    pb.on_public_trade("U1", comp, "SELL", 0.55, 15, 10)     # eats 15 of the 20 ahead
    assert not fills
    pb.on_public_trade("D1", comp, "BUY", 0.45, 8, 20)       # complement buy at 0.45 = sell at 0.55: 5 ahead, 3 us
    assert len(fills) == 1 and fills[0]["size"] == 3
    pb.on_public_trade("U1", comp, "SELL", 0.54, 100, 30)    # trade-through: the rest
    assert sum(f["size"] for f in fills) == 5
    old = PaperBroker(0, 0, on_fill=fills.append)            # default model unchanged: at-price prints never fill
    run(old.place("U1", "k", "U", 0.55, 5, now_ms=0, queue_ahead=0))
    n = len(fills)
    old.on_public_trade("U1", comp, "SELL", 0.55, 50, 10)
    assert len(fills) == n


def test_one_unpaired_lot_and_partial_fills_cannot_exceed_net_limit():
    cfg = load_config("config/live_d.toml")
    assert cfg.risk.max_net_shares_per_window == 5
    w = mk_window()
    bu, bd = book(0.49, 0.50), book(0.50, 0.51)
    pos = Position(up=5.0, down=0.0, cost=2.45)
    q = target_quotes(w, 0.5, bu, bd, pos, cfg.strategy, cfg.risk)
    assert q["U"] is None and q["D"] is not None                 # long Up: only Down may be bid
    pos = Position(up=0.97 + 3.02, down=0.0, cost=0.64)          # partial fills: 3.99 Up
    q = target_quotes(w, 0.5, bu, bd, pos, cfg.strategy, cfg.risk)
    assert q["U"] is None                                       # room 1.01 < 5 shares minimum -> no new Up bid
    pos = Position(up=5.0, down=5.0, cost=4.9)                   # paired: inventory allows both sides again ...
    cfg.risk.max_cost_per_window = 20                            # (... the $5 window budget would stop it in live)
    q = target_quotes(w, 0.5, bu, bd, pos, cfg.strategy, cfg.risk)
    assert q["U"] is not None and q["D"] is not None


def test_calm_market_filter():
    import math
    now = T0 * 1000 + 30_000
    st, w = mk_state(now)
    cfg = load_config("config/v3.toml")
    assert cfg.filters.min_vol_5m_bp == 2.7
    px = st.prices["btc"]
    px.cl_by_sec.clear()
    s0 = now // 1000
    for i, s in enumerate(range(s0 - 600, s0)):                 # calm: alternating 0.02 bp moves
        px.cl_by_sec[s] = 100.0 * (1 + 0.000002 * (i % 2))
    fv = fair_value(w, px, now)
    assert "calm_market" in check(w, st, fv, now, cfg.filters, cfg.strategy)
    for i, s in enumerate(range(s0 - 600, s0)):                 # volatile: alternating 0.5 bp moves
        px.cl_by_sec[s] = 100.0 * (1 + 0.00005 * (i % 2))
    assert px.cl_vol_5m_bp(s0) > 2.7
    assert "calm_market" not in check(w, st, fv, now, cfg.filters, cfg.strategy)


def test_opening_legs_only_near_half_closing_leg_any_price():
    cfg = load_config("config/live_v3.toml")
    assert (cfg.strategy.min_open_price, cfg.strategy.max_open_price) == (0.40, 0.60)
    w = mk_window()
    bu, bd = book(0.55, 0.56), book(0.44, 0.45)          # near 0.5: both sides rest at once, a pair can form
    q = target_quotes(w, 0.555, bu, bd, Position(), cfg.strategy, cfg.risk)
    assert q["U"][0] == 0.55 and q["D"][0] == 0.44
    bu, bd = book(0.61, 0.62), book(0.38, 0.39)          # tails: favourite above 0.60, underdog below 0.40
    q = target_quotes(w, 0.615, bu, bd, Position(), cfg.strategy, cfg.risk)
    assert q["U"] is None and q["D"] is None                                  # flat: nothing opens
    pos = Position(up=0.0, down=5.0, cost=1.9)                               # long Down 5 from an earlier fill
    q = target_quotes(w, 0.615, bu, bd, pos, cfg.strategy, cfg.risk)
    assert q["U"] is not None and q["U"][0] > 0.60 and q["D"] is None        # closing leg at 0.61 allowed
