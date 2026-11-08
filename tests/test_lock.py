"""TWAP-lock bot (reports/44_twap_lock.md): model, decision rule, configs, accounting."""
import asyncio
import math
import os
import tomllib

import pytest

from pmbot.bot.lock import LockBot, PaperTaker, decide, load_lock_config, tick_floor
from pmbot.bot.state import Book, State
from pmbot.lockmodel import ClSigma, Tables, expected_dist_bp, lock_state, prob_up, strike_from_prints, vfac

ROOT = os.path.join(os.path.dirname(__file__), "..")
T = 1_794_000_000          # a window start (multiple of 300)


@pytest.fixture(scope="module")
def tables():
    return Tables(os.path.join(ROOT, "config", "lock_tables.npz"))


def prints(level_by_sec):
    return dict(level_by_sec)


def test_tables_are_survival_functions(tables):
    assert set(tables.assets) == {"btc", "eth", "sol", "xrp"}
    for a in tables.assets:
        s = tables.s[a]
        assert s.shape[0] == 60
        # P(Y >= y) falls with y and is ~1 far left, ~0 far right
        assert (s[:, :-1] >= s[:, 1:] - 1e-6).all()
        assert s[:, 0].min() > 0.999 and s[:, -1].max() < 1e-3
    # fat tails: P(Y <= -4) is far above the Gaussian 3e-5
    assert 1 - tables.surv("btc", 30, -4.0) > 1e-3


def test_vfac_and_sigma():
    assert vfac(1) == 1.0
    assert abs(vfac(60) - 61 * 121 / 360) < 1e-12
    s = ClSigma(halflife=900, min_n=3)
    s.on_print(100, 100.0)
    assert s.sigma() is None
    for i, v in enumerate([100.01, 100.0, 100.01, 100.0], start=1):
        s.on_print(100 + i, v)
    assert 0.5e-4 < s.sigma() < 1.5e-4
    n = s.n
    s.on_print(110, 100.0)            # 5 missing seconds are zero returns (forward fill)
    assert s.n == n + 6


def test_strike_from_prints_forward_fills_up_to_three_gaps():
    cl = {T + s: 100.0 + (s % 7) * 0.001 for s in range(-75, 0)}
    exact = sum(cl[T + s] for s in range(-62, -2)) / 60
    assert abs(strike_from_prints(cl, T) - exact) < 1e-12
    cl2 = dict(cl)
    for s in (-50, -40, -30):
        cl2.pop(T + s)
    k = strike_from_prints(cl2, T)
    assert k is not None and abs(k / exact - 1) < 1e-6
    cl2.pop(T - 20)
    assert strike_from_prints(cl2, T) is None


def test_prob_up_limits(tables):
    K = 100.0
    # 50 of 60 prints known at +3 bp, spot +3 bp: Up nearly sure; mirror for Down
    A = 50 * K * (1 + 3e-4)
    assert prob_up(tables, "btc", A, 10, K * (1 + 3e-4), K, 1.5e-5) > 0.995
    A = 50 * K * (1 - 3e-4)
    assert prob_up(tables, "btc", A, 10, K * (1 - 3e-4), K, 1.5e-5) < 0.005
    # all prints known: deterministic but never more than 1 - 1e-4
    assert prob_up(tables, "btc", 60 * K * (1 + 1e-6), 0, K, K, 1.5e-5) == pytest.approx(1 - 1e-4)
    assert prob_up(tables, "btc", 60 * K * (1 - 1e-6), 0, K, K, 1.5e-5) == pytest.approx(1e-4)
    assert expected_dist_bp(50 * K, 10, K * 1.0006, K) == pytest.approx(1.0, rel=1e-9)


def scenario(bp=2.0, rel=270.0, last_lag=1.4):
    """Prints at +bp over the strike since the closing window opened; returns decide() inputs."""
    K = 100.0
    now_s = T + rel
    last_sec = int(now_s - last_lag)
    cl = {T + s: K for s in range(-80, 238)}
    for s in range(T + 238, last_sec + 1):
        cl[s] = K * (1 + bp * 1e-4)
    last = (cl[last_sec], last_sec * 1000, int((now_s - 0.3) * 1000))
    return K, now_s, cl, last


def test_decide_buys_the_locked_side(tables):
    c = load_lock_config(os.path.join(ROOT, "config", "lock.toml")).lock
    K, now_s, cl, last = scenario(bp=2.0, rel=280.0)
    bn = 75000.0
    sig, ctx = decide(c, tables, "btc", T, K, int(now_s * 1000), cl, last, 1.5e-5, bn, bn, bn, bn,
                      (0.95, 100.0), (0.06, 100.0))
    assert sig and sig["side"] == "U", ctx
    assert sig["limit"] <= c.a_max and sig["ask"] == 0.95
    # the same with Down favoured
    K2, now2, cl2, last2 = scenario(bp=-2.0, rel=280.0)
    sig, ctx = decide(c, tables, "btc", T, K2, int(now2 * 1000), cl2, last2, 1.5e-5, bn, bn, bn, bn,
                      (0.06, 100.0), (0.95, 100.0))
    assert sig and sig["side"] == "D", ctx


@pytest.mark.parametrize("change,why", [
    ({"ask_up": (0.99, 100.0)}, "ask_too_high"),          # above a_max 0.98
    ({"ask_up": (0.85, 100.0)}, "ask_too_low"),           # '99% mode' floor 0.90
    ({"ask_up": None}, "no_ask"),
    ({"bn_1s": 75000 * (1 + 2e-4)}, "binance_against"),  # Binance fell 2 bp in the last second
    ({"bn_1s": 75000 * (1 - 6e-4), "bn_3s": 75000 * (1 - 6e-4)}, "binance_jump"),  # +6 bp in 1 s: glitch guard
    ({"K": None}, "no_strike"),
    ({"sigma": None}, "no_sigma"),
    ({"rel": 230.0}, "time"),                             # before the closing TWAP window
    ({"stale": True}, "cl_stale"),
])
def test_decide_filters(tables, change, why):
    c = load_lock_config(os.path.join(ROOT, "config", "lock.toml")).lock
    rel = change.get("rel", 280.0)
    K, now_s, cl, last = scenario(bp=2.0, rel=rel)
    if change.get("stale"):
        last = (last[0], last[1], int((now_s - 3.0) * 1000))
    bn = 75000.0
    args = dict(K=K, sigma=1.5e-5, bn_1s=bn, bn_3s=bn, ask_up=(0.95, 100.0))
    args.update({k: v for k, v in change.items() if k in args})
    sig, ctx = decide(c, tables, "btc", T, args["K"], int(now_s * 1000), cl, last, args["sigma"], bn,
                      bn, args["bn_1s"], args["bn_3s"], args["ask_up"], (0.06, 100.0))
    assert sig is None and ctx["why"] == why, ctx


def test_decide_needs_distance_and_spot_on_our_side(tables):
    c = load_lock_config(os.path.join(ROOT, "config", "lock.toml")).lock
    K, now_s, cl, last = scenario(bp=0.3, rel=285.0)          # expected TWAP only 0.3 bp past the strike
    bn = 75000.0
    sig, ctx = decide(c, tables, "btc", T, K, int(now_s * 1000), cl, last, 1.0e-6, bn, bn, bn, bn,
                      (0.95, 100.0), (0.06, 100.0))
    assert sig is None and ctx["why"] in ("dist", "p"), ctx
    # TWAP still well above, but the spot already crossed below the strike (8 Nov BTC crash pattern)
    K, now_s, cl, last = scenario(bp=3.0, rel=285.0)
    crossed = (K * (1 - 0.5e-4), last[1], last[2])
    sig, ctx = decide(c, tables, "btc", T, K, int(now_s * 1000), cl, crossed, 1.0e-6, bn, bn, bn, bn,
                      (0.95, 100.0), (0.06, 100.0))
    assert sig is None and ctx["why"] in ("spot_side", "p", "agree"), ctx


def test_lock_state_nowcast_counts_unreceived_past_prints():
    K, now_s, cl, last = scenario(bp=2.0, rel=280.0, last_lag=1.4)
    st = lock_state(cl, last[1] // 1000, last[0], T, K, now_s, 75000 * 1.0001, 75000)
    assert st["n"] == int(now_s - 1.4) - (T + 238) + 1
    assert st["m_nc"] < st["m_cl"]                      # nowcast fills the past second(s) not received yet
    assert st["S_nc"] == pytest.approx(last[0] * 1.0001)


def test_configs_load_and_live_is_the_twin():
    paper = load_lock_config(os.path.join(ROOT, "config", "lock.toml"))
    live = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    wide = load_lock_config(os.path.join(ROOT, "config", "lock_wide.toml"))
    assert paper.execution.mode == "paper" and wide.execution.mode == "paper" and live.execution.mode == "live"
    assert live.risk.resume_next_day is False and live.risk.state_file
    assert paper.lock == live.lock, "the live rule must stay identical to its paper twin pmbot-lock"
    assert paper.risk.max_usd_per_entry == live.risk.max_usd_per_entry
    assert paper.risk.max_open_usd == live.risk.max_open_usd
    assert wide.lock.a_min < paper.lock.a_min
    raw = tomllib.load(open(os.path.join(ROOT, "config", "lock_live.toml"), "rb"))
    assert raw["execution"]["mode"] == "live"
    with pytest.raises(ValueError):
        load_lock_config(overrides={"execution": {"mode": "live"}, "risk": {"state_file": "x", "resume_next_day": True}})
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"rel_lo": 200.0}})
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"typo": 1}})


def test_units_wire_the_configs():
    for unit, cfg, live in (("pmbot-lock", "config/lock.toml", False), ("pmbot-lock-wide", "config/lock_wide.toml", False),
                            ("pmbot-lock-live", "config/lock_live.toml", True)):
        text = open(os.path.join(ROOT, "deploy", unit + ".service")).read()
        assert f"--config {cfg}" in text
        assert ("--live" in text) == live


def test_tick_floor_and_price_string():
    from pmbot.bot.live import price_str
    assert tick_floor(0.9749, 0.01) == pytest.approx(0.97)
    assert tick_floor(0.9749, 0.001) == pytest.approx(0.974)
    assert tick_floor(0.98, 0.01) == pytest.approx(0.98)          # no float drift below the grid
    assert price_str(0.98) == "0.98" and price_str(0.975) == "0.975" and price_str(0.9) == "0.90"
    assert price_str(round(tick_floor(0.9749, 0.01), 2)) == "0.97"


def test_paper_taker_walks_the_asks():
    st = State(["btc"])
    st.books["tok"] = Book(bids={0.9: 10}, asks={0.95: 3.0, 0.96: 10.0, 0.99: 50.0})
    pt = PaperTaker(st, exec_ms=0)
    px, got, fee = asyncio.run(pt.buy("tok", "btc-1", "U", 0.96, 5.0))
    assert got == pytest.approx(5.0)
    assert px == pytest.approx((3 * 0.95 + 2 * 0.96) / 5)
    assert fee == pytest.approx(3 * 0.07 * 0.95 * 0.05 + 2 * 0.07 * 0.96 * 0.04)
    px, got, fee = asyncio.run(pt.buy("tok", "btc-1", "U", 0.94, 5.0))
    assert got == 0


class _J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append((kind, kw))


class _Tab:
    def surv(self, *a):
        return 0.5


def test_settle_and_daily_stop(tmp_path):
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "daily_loss_limit": 4.0,
                                               "kill_file": str(tmp_path / "STOP")}})
    j = _J()
    bot = LockBot(cfg, j, broker=PaperTaker(State(["btc"])), tables=_Tab())
    import time as _t
    T0 = int(_t.time()) // 300 * 300 - 600
    win, lose = f"btc-{T0}", f"eth-{T0}"
    bot.entries[win] = [{"side": "U", "shares": 5.0, "price": 0.95, "cost": 4.75, "fee": 0.0166, "t": 0, "limit": 0.96,
                         "p": 0.995}]
    bot.entries[lose] = [{"side": "D", "shares": 5.0, "price": 0.96, "cost": 4.8, "fee": 0.0134, "t": 0, "limit": 0.97,
                          "p": 0.995}]
    assert bot.open_cost() == pytest.approx(4.75 + 0.0166 + 4.8 + 0.0134)
    bot._settle(win, "Up")
    s = [kw for k, kw in j.ev if k == "settle"][-1]
    assert s["pnl"] == pytest.approx(5 - 4.75 - 0.0166, abs=1e-4) and s["up"] == 5.0 and s["down"] == 0
    ok, _ = bot.can_enter()
    assert ok
    bot._settle(lose, "Up")
    assert bot.day_pnl() == pytest.approx(0.2334 - 4.8134, abs=1e-3)
    ok, why = bot.can_enter()
    assert not ok and why == "killed"
    assert any(k == "kill" for k, _ in j.ev)
    # state survives a restart (realized PnL, kill flag)
    bot2 = LockBot(cfg, _J(), broker=PaperTaker(State(["btc"])), tables=_Tab())
    assert bot2.day_pnl() == pytest.approx(bot.day_pnl())
    assert not bot2.can_enter()[0]
    # kill file blocks new entries
    open(cfg.risk.kill_file, "w").close()
    assert bot2.can_enter() == (False, "kill_file") or bot2.can_enter()[1] == "killed"
