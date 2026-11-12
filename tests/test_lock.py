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


# the L3 tiers until reports/59 (live and its twins dropped the last one on 10 Nov; L4, L3-15 and LM3 keep it on paper)
FOUR_TIERS = {"lock": {"theta": 0.90, "tiers": [[0.99, 0.98], [0.97, 0.95], [0.95, 0.93], [0.90, 0.91]]}}


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
    tier = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"))
    live = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    wide = load_lock_config(os.path.join(ROOT, "config", "lock_wide.toml"))
    assert paper.execution.mode == "paper" and tier.execution.mode == "paper" and wide.execution.mode == "paper"
    assert live.execution.mode == "live"
    assert live.risk.resume_next_day is False and live.risk.state_file
    # live runs L3 (price tiers) since reports/50: its paper twin is pmbot-lock-tier = config/lock_tier.toml
    assert tier.lock == live.lock, "the live rule must stay identical to its paper twin pmbot-lock-tier (L3)"
    assert tier.lock.tiers and live.lock.theta < paper.lock.theta
    assert tier.risk.max_usd_per_entry == live.risk.max_usd_per_entry
    assert tier.risk.max_open_usd == live.risk.max_open_usd
    # L -> L3 changed the rule, not the money. reports/67, the owner's word on 11 Nov ("давай 10"): 10 shares from model p 0.97, so the
    # per-entry / open limits are $10 and one entry at a time is kept by max_open_entries; any further raise is the owner's call
    assert live.lock.shares <= 5 and live.lock.big_shares <= 10 and live.risk.max_usd_per_entry <= 10.0
    assert live.risk.max_open_usd <= 10.0 and live.risk.max_open_entries == 1 and live.risk.daily_loss_limit <= 10.0
    assert tier.risk.max_open_entries == live.risk.max_open_entries
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
                            ("pmbot-lock-15", "config/lock15.toml", False),
                            ("pmbot-lock-maker", "config/lock_maker.toml", False),
                            ("pmbot-lock-tier", "config/lock_tier.toml", False),
                            ("pmbot-lock-15-tier", "config/lock15_tier.toml", False),
                            ("pmbot-lock-maker3", "config/lock_maker_tier.toml", False),
                            ("pmbot-lock-tier-pb", "config/lock_tier_pb.toml", False),
                            ("pmbot-lock-tier-late", "config/lock_tier_late.toml", False),
                            ("pmbot-lock-99", "config/lock_99.toml", False),
                            ("pmbot-lock-99-live", "config/lock_99_live.toml", True),
                            ("pmbot-lock-live", "config/lock_live.toml", True),
                            ("pmbot-lock-15-live", "config/lock15_live.toml", True)):
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


def test_open_cost_frees_an_entry_once_its_result_is_known(tmp_path):
    """max_open_usd = 5 (live L3, reports/51) means one entry at a time. The entry must stop blocking the next window once
    the window has ended and its provisional result is in day_pnl, not only when Gamma settles it minutes later."""
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": str(tmp_path / "STOP")}})
    assert cfg.risk.max_open_entries == 1                               # one entry at a time (reports/51; by count since reports/67)
    bot = LockBot(cfg, _J(), broker=PaperTaker(State(["btc"])), tables=_Tab())
    import time as _t
    T0 = int(_t.time()) // 300 * 300 - 300
    key = f"btc-{T0}"
    bot.entries[key] = [{"side": "U", "shares": 5.0, "price": 0.91, "cost": 4.55, "fee": 0.02, "t": 0, "limit": 0.91,
                         "p": 0.92}]
    assert bot.open_cost() == pytest.approx(4.57)
    assert bot.open_windows() >= cfg.risk.max_open_entries              # a second entry is refused while the result is open
    bot.risk["provisional"][key] = -4.57                                 # what _provisional writes at the window end
    assert bot.open_cost() == 0.0 and bot.open_windows() == 0 and bot.day_pnl() == pytest.approx(-4.57)
    bot._settle(key, "Down")
    assert bot.open_cost() == 0.0 and bot.day_pnl() == pytest.approx(-4.57)


class _ErrBroker:
    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    async def buy(self, *a):
        self.calls += 1
        raise TimeoutError("no reply")


@pytest.mark.parametrize("mode", ["live", "paper"])
def test_order_error_blocks_the_window_in_live_only(tmp_path, mode):
    """A live order that errors (timeout) may still have been matched: no second order in that window."""
    from pmbot.bot.state import Window
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    j = _J()
    br = _ErrBroker(mode)
    bot = LockBot(cfg, j, broker=br, tables=_Tab())
    w = Window(asset="btc", start=T, slug="s", condition_id="c", token_up="u", token_down="d")
    sig = {"side": "U", "limit": 0.97, "ask": 0.95, "p": 0.995}
    bot.busy.add(w.key)
    asyncio.run(bot._execute(w, sig, 5, {"rel": 250.0}))
    assert br.calls == 1 and w.key not in bot.busy and not bot.entries.get(w.key)
    blocked = any(k == "window_blocked" for k, _ in j.ev)
    if mode == "live":
        assert blocked and bot.next_try[w.key] == float("inf")
    else:
        assert not blocked and bot.next_try[w.key] < float("inf")


def test_l15_is_l_on_15m_windows(tmp_path):
    """L15 = the L rule with duration 900: the closing TWAP is [end-62, end-3], decisions in the last minute."""
    l5 = load_lock_config(os.path.join(ROOT, "config", "lock.toml"))
    l15 = load_lock_config(os.path.join(ROOT, "config", "lock15.toml"))
    assert l15.lock.duration == 900 and l5.lock.duration == 300 and l15.execution.mode == "paper"
    a, b = dict(vars(l5.lock)), dict(vars(l15.lock))
    a.pop("duration"), b.pop("duration")
    assert a == b, "L15 must keep the L filters"
    assert l15.risk.state_file != l5.risk.state_file and l15.journal_dir != l5.journal_dir
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"duration": 600}})
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock15.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=PaperTaker(State(["btc"])), tables=_Tab())
    assert bot.anchor(T) == T + 600 and bot.end_of(f"btc-{T}") == T + 900 and bot.tag == "15m"
    # a 15m window locked in its last minute: same decision as a 5m window whose start is the anchor
    from pmbot.gamma import slug_for
    assert slug_for("btc", T, bot.tag) == f"btc-updown-15m-{T}"


def test_l15_decides_on_the_closing_minute(tables):
    """decide() with the 15m anchor Tc = start + 600 uses the prints of [start+838, start+897] and the start strike."""
    c = load_lock_config(os.path.join(ROOT, "config", "lock15.toml")).lock
    S = T                       # 15m window start
    Tc = S + 600
    K = 100.0
    cl = {s: 100.0 for s in range(S - 70, S)}
    cl.update({s: 100.05 for s in range(S, Tc + 250)})      # +5 bp all window long, 12 closing prints in
    now = (Tc + 250) * 1000 + 300
    last = (100.05, (Tc + 249) * 1000, now - 1200)
    args = (cl, last, 0.00002, 100.05, 100.05, 100.05, 100.05, (0.95, 50.0), (0.06, 50.0))
    sig, ctx = decide(c, tables, "btc", Tc, K, now, *args)
    assert ctx["rel"] == pytest.approx(250.3, abs=0.01)
    assert sig and sig["side"] == "U" and ctx["n"] == 12 and sig["p"] >= 0.99
    sig, ctx = decide(c, tables, "btc", S, K, now, *args)       # the window start is not the anchor
    assert sig is None and ctx["why"] == "time"


def test_lm_config_is_l_with_a_maker_entry():
    l5 = load_lock_config(os.path.join(ROOT, "config", "lock.toml"))
    lm = load_lock_config(os.path.join(ROOT, "config", "lock_maker.toml"))
    assert lm.lock.entry == "maker" and l5.lock.entry == "taker" and lm.execution.mode == "paper"
    a, b = dict(vars(l5.lock)), dict(vars(lm.lock))
    a.pop("entry"), b.pop("entry")
    assert a == b, "LM must keep the L rule"
    assert lm.risk.state_file not in (l5.risk.state_file, "") and lm.journal_dir != l5.journal_dir
    with pytest.raises(ValueError, match="paper-only"):
        load_lock_config(os.path.join(ROOT, "config", "lock_maker.toml"),
                         overrides={"execution": {"mode": "live"}, "risk": {"resume_next_day": False}})


def test_maker_bid_prices():
    from pmbot.bot.lock import maker_bid
    c = load_lock_config(os.path.join(ROOT, "config", "lock_maker.toml")).lock
    assert maker_bid(c, 0.999, None) == 0.98                 # capped at a_max
    assert maker_bid(c, 0.99, None) == 0.98                  # p - 0.005 = 0.985 -> 0.98
    assert maker_bid(c, 0.999, 0.985) == 0.97                # one cent under the ask (post-only), 0.01 grid
    assert maker_bid(c, 0.999, 0.95) == 0.94
    assert maker_bid(c, 0.999, 0.905) is None                # would be below a_min 0.90


def test_maker_fills_through_the_queue_and_settles(tmp_path):
    """A resting LM bid fills only after the queue ahead of it is used up (or on a trade-through), never above the
    lot, and the lot is settled like a taker entry."""
    from pmbot.bot.state import Window
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock_maker.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    j = _J()
    bot = LockBot(cfg, j, tables=_Tab())
    assert bot.maker and bot.broker.queue
    w = Window(asset="btc", start=T, slug="s", condition_id="c", token_up="u", token_down="d")
    bot.st.add_window(w)
    oid = asyncio.run(bot.broker.place("u", w.key, "U", 0.98, 5.0, now_ms=0, queue_ahead=10.0))
    bot.bids[w.key] = {"oid": oid, "side": "U", "price": 0.98, "size": 5.0, "token": "u", "p": 0.995}
    bot._on_public_trade("u", "SELL", 0.98, 8.0, 1000, 1000)     # eats 8 of the 10 ahead of us
    assert bot.filled(w.key) == 0
    bot._on_public_trade("u", "SELL", 0.98, 5.0, 1100, 1100)     # 2 more ahead, then 3 for us
    assert bot.filled(w.key) == pytest.approx(3.0)
    bot._on_public_trade("d", "BUY", 0.02, 1.0, 1150, 1150)      # Down bought at 0.02 = Up sold at 0.98: queue
    bot._on_public_trade("u", "SELL", 0.96, 10.0, 1200, 1200)    # trade-through fills the rest, not more
    assert bot.filled(w.key) == pytest.approx(5.0) and w.key not in bot.bids
    assert all(e["price"] == 0.98 and e["fee"] == 0.0 for e in bot.entries[w.key])
    bot._settle(w.key, "Up")
    s = [kw for k, kw in j.ev if k == "settle"][-1]
    assert s["pnl"] == pytest.approx(5 - 4.9, abs=1e-6)


def test_maker_cancel_pulls_the_bid(tmp_path):
    from pmbot.bot.state import Window
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock_maker.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), tables=_Tab())
    w = Window(asset="btc", start=T, slug="s", condition_id="c", token_up="u", token_down="d")
    bot.st.add_window(w)
    oid = asyncio.run(bot.broker.place("u", w.key, "U", 0.98, 5.0, now_ms=0, queue_ahead=0.0))
    bot.bids[w.key] = {"oid": oid, "side": "U", "price": 0.98, "size": 5.0, "token": "u", "p": 0.995}
    asyncio.run(bot._cancel_bid(w.key, 1000, "binance_against"))   # effective 50 ms later
    bot._on_public_trade("u", "SELL", 0.97, 5.0, 1020, 1020)          # inside the cancel latency: still fills
    assert bot.filled(w.key) == pytest.approx(5.0)
    cfg2 = load_lock_config(os.path.join(ROOT, "config", "lock_maker.toml"),
                            overrides={"risk": {"state_file": str(tmp_path / "s2.json"), "kill_file": ""}})
    bot2 = LockBot(cfg2, _J(), tables=_Tab())
    bot2.st.add_window(w)
    oid = asyncio.run(bot2.broker.place("u", w.key, "U", 0.98, 5.0, now_ms=0, queue_ahead=0.0))
    bot2.bids[w.key] = {"oid": oid, "side": "U", "price": 0.98, "size": 5.0, "token": "u", "p": 0.995}
    asyncio.run(bot2._cancel_bid(w.key, 1000, "binance_against"))
    bot2._on_public_trade("u", "SELL", 0.97, 5.0, 1100, 1100)         # after the cancel: no fill
    assert bot2.filled(w.key) == 0 and w.key not in bot2.bids


def test_lock_maker2_bids_lower_and_guards_binance_tighter():
    from pmbot.bot.lock import maker_bid
    lm = load_lock_config(os.path.join(ROOT, "config", "lock_maker.toml"))
    lm2 = load_lock_config(os.path.join(ROOT, "config", "lock_maker2.toml"))
    assert lm2.execution.mode == "paper" and lm2.lock.entry == "maker"
    assert (lm2.lock.a_max, lm2.lock.adv3_bp) == (0.96, 1.0)
    assert lm2.risk.state_file != lm.risk.state_file and lm2.journal_dir != lm.journal_dir
    assert maker_bid(lm.lock, 0.995, 0.99) == 0.98 and maker_bid(lm2.lock, 0.995, 0.99) == 0.96
    for k in ("theta", "agree", "dmin_bp", "adv1_bp", "jump_bp", "a_min", "min_edge", "rel_lo", "rel_hi", "shares"):
        assert getattr(lm2.lock, k) == getattr(lm.lock, k), k          # only a_max and adv3_bp differ


def test_lock_tiers_lower_probability_needs_cheaper_ask():
    from pmbot.bot.lock import tier_limit
    from pmbot.fees import fee_per_share
    paper = load_lock_config(os.path.join(ROOT, "config", "lock.toml"))
    tier = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"))
    assert tier.execution.mode == "paper" and tier.lock.entry == "taker" and tier.lock.tiers
    # everything except theta, tiers and the files is L's
    assert tier.risk.state_file != paper.risk.state_file and tier.journal_dir != paper.journal_dir
    for k in ("agree", "dmin_bp", "adv1_bp", "adv3_bp", "jump_bp", "a_min", "a_max", "min_edge", "rel_lo", "rel_hi",
              "assets", "max_entries_per_window"):
        assert getattr(tier.lock, k) == getattr(paper.lock, k), k
    # reports/70: below big_shares_p (the p 0.95-0.97 tier) L3 buys 2 shares, not L's 5; still above the $1 exchange minimum
    assert tier.lock.shares == 2 and tier.lock.shares * min(t[1] for t in tier.lock.tiers) >= 1.05
    live = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    for k in ("max_usd_per_entry", "daily_loss_limit", "max_open_usd", "max_open_entries"):   # the twin has live's money limits (reports/67)
        assert getattr(tier.risk, k) == getattr(live.risk, k), k
    assert tier.risk.max_open_usd <= paper.risk.max_open_usd      # L3 = live twin: one entry at a time since reports/51
    c = tier.lock
    assert tier_limit(c, 0.995) == 0.98 and tier_limit(c, 0.98) == 0.95 and tier_limit(c, 0.96) == 0.93
    # reports/59: the live rule and its twin have no p 0.90-0.95 tier any more (every loss of the filtered rule was there)
    assert tier_limit(c, 0.92) is None and tier_limit(c, 0.89) is None and c.theta == 0.95
    old = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides=FOUR_TIERS).lock
    assert tier_limit(old, 0.92) == 0.91 and tier_limit(old, 0.89) is None
    # the top tier is L's own rule: p >= 0.99 -> ask up to 0.98
    for p in (0.99, 0.999):
        assert min(c.a_max, tier_limit(c, p)) == min(paper.lock.a_max, p - paper.lock.min_edge - fee_per_share(0.98))


def test_lock_tiers_validation():
    import pytest
    base = {"lock": {"theta": 0.90, "agree": 0.90, "tiers": [[0.99, 0.98], [0.95, 0.93]]}}
    assert load_lock_config(overrides=base).lock.tiers
    for bad in ({"tiers": [[0.99, 0.98], [0.95, 0.99]]},           # ask above a_max
                {"tiers": [[0.99, 0.93], [0.95, 0.98]]},           # a higher p must not be cheaper
                {"tiers": [[0.80, 0.93]]}):                         # below theta
        with pytest.raises(ValueError):
            load_lock_config(overrides={"lock": {"theta": 0.90, "agree": 0.90, **bad}})


def test_decide_with_tiers_end_to_end(tables):
    """Same situation, probability between 0.95 and 0.97: L (theta 0.99) says no, L3 buys at <= 0.93 and not at 0.94."""
    from pmbot.bot.lock import decide_state
    paper = load_lock_config(os.path.join(ROOT, "config", "lock.toml")).lock
    tier = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides={"lock": {"sigma_floor_bp": 0.0}}).lock
    K, now_s, cl, last = scenario(bp=1.0, rel=270.0)             # the tier logic itself: sigma floor off (reports/52)
    bn = 75000.0
    now = int(now_s * 1000)
    sigma = None
    for sg in [x * 1e-5 for x in range(2, 80)]:             # find a sigma that puts the model in the 0.95-0.97 tier
        side, p, _ = decide_state(tier, tables, "btc", T, K, now, cl, last, sg, bn, bn, bn, bn)
        if side == "U" and 0.95 <= p < 0.97:
            sigma = sg
            break
    assert sigma is not None, "no sigma gives p in [0.95, 0.97)"
    asks = lambda a: (a, 100.0)  # noqa: E731
    args = (tables, "btc", T, K, now, cl, last, sigma, bn, bn, bn, bn)
    sig, ctx = decide(paper, *args, asks(0.92), (0.06, 100.0))
    assert sig is None and ctx["why"] == "p"                  # L: p < 0.99
    sig, ctx = decide(tier, *args, asks(0.92), (0.06, 100.0))
    assert sig and sig["side"] == "U" and sig["limit"] == pytest.approx(0.93), ctx
    sig, ctx = decide(tier, *args, asks(0.93), (0.06, 100.0))
    assert sig, ctx
    sig, ctx = decide(tier, *args, asks(0.94), (0.06, 100.0))
    assert sig is None and ctx["why"] == "ask_too_high"       # a 0.97 probability must come cheaper than 0.94
    sig, ctx = decide(tier, *args, asks(0.85), (0.06, 100.0))
    assert sig is None and ctx["why"] == "ask_too_low"        # a_min 0.90 still holds


def test_lock_tier_wide_is_l3_with_a_lower_price_floor(tables):
    tier = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"))
    wide = load_lock_config(os.path.join(ROOT, "config", "lock_tier_wide.toml"))
    assert wide.execution.mode == "paper" and wide.lock.a_min == 0.50 and tier.lock.a_min == 0.90
    # L4 stays the paper reference WITHOUT the sigma floor of reports/52 (what L3 was until 10 Nov, plus cheap asks)
    assert wide.lock.sigma_floor_bp == 0.0 and tier.lock.sigma_floor_bp == 0.5
    tier = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"),      # L4 keeps the p 0.90-0.95 tier (reports/59)
                            overrides={"lock": {"sigma_floor_bp": 0.0, **FOUR_TIERS["lock"]}})
    assert wide.risk.state_file != tier.risk.state_file and wide.journal_dir != tier.journal_dir
    for k in ("theta", "agree", "dmin_bp", "adv1_bp", "adv3_bp", "jump_bp", "a_max", "min_edge", "rel_lo", "rel_hi",
              "assets", "tiers", "max_entries_per_window"):     # shares: L4 keeps 5, L3 buys 2 below p 0.97 (reports/70)
        assert getattr(wide.lock, k) == getattr(tier.lock, k), k
    from pmbot.bot.lock import decide_state
    K, now_s, cl, last = scenario(bp=1.0, rel=270.0)
    bn = 75000.0
    now = int(now_s * 1000)
    sigma = next(sg for sg in [x * 1e-5 for x in range(2, 80)]
                 if (lambda r: r[0] == "U" and 0.95 <= r[1] < 0.97)(decide_state(tier.lock, tables, "btc", T, K, now, cl, last, sg, bn, bn, bn, bn)))
    args = (tables, "btc", T, K, now, cl, last, sigma, bn, bn, bn, bn)
    sig, ctx = decide(tier.lock, *args, (0.70, 100.0), (0.30, 100.0))
    assert sig is None and ctx["why"] == "ask_too_low"          # L3: the market doubting this much is a reason to stay out
    sig, ctx = decide(wide.lock, *args, (0.70, 100.0), (0.30, 100.0))
    assert sig and sig["side"] == "U" and sig["ask"] == 0.70, ctx


def test_lock_bot_reports_event_loop_lag(tmp_path):
    """Lag and decide time are measured per status interval (the hourly report shows 'лаг цикла до X мс')."""
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=PaperTaker(State(["btc"])), tables=_Tab())
    assert {"lag_max_ms", "lag_over_100ms", "decide_max_ms"} <= set(bot.stats)

    async def go():
        import time as _t
        task = asyncio.create_task(bot.lag_loop(period_s=0.01))
        await asyncio.sleep(0.03)
        _t.sleep(0.15)                      # block the loop: a starved / busy process
        await asyncio.sleep(0.03)
        bot._stop = True
        await task
    asyncio.run(go())
    assert bot.stats["lag_max_ms"] >= 100 and bot.stats["lag_over_100ms"] >= 1


# ------------------------------------------------------------------------------------------------ live execution path
def _full_ctx():
    """The context decide() hands to _execute(): it carries "side" and "limit" itself."""
    return {"rel": 250.0, "side": "U", "p": 0.96, "p_cl": 0.95, "dist_bp": 2.0, "spot_bp": 2.0, "mv1": 0.0, "mv3": 0.0,
            "n": 10, "m": 48, "sigma_bp": 0.3, "ask": 0.92, "ask_sz": 50.0, "limit": 0.9349}


def _bot(tmp_path, broker, config="lock_tier.toml", **risk):
    from pmbot.bot.state import Window
    cfg = load_lock_config(os.path.join(ROOT, "config", config),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": "", **risk}})
    j = _J()
    bot = LockBot(cfg, j, broker=broker, tables=_Tab())
    w = Window(asset="btc", start=T, slug="s", condition_id="c", token_up="u", token_down="d")
    bot.st.add_window(w)
    return bot, j, w


def test_execute_journals_the_entry_and_the_miss_with_the_decision_context(tmp_path):
    """lock_entry / lock_miss were never written: ctx already holds "side" and "limit" and passing them again as
    keywords raised TypeError inside the order task (9 Nov: no latency and no miss rate in any journal)."""
    sig = {"side": "U", "limit": 0.9349, "ask": 0.92, "p": 0.96}
    st = State(["btc"])
    bot, j, w = _bot(tmp_path, PaperTaker(st, exec_ms=0))
    st.books["u"] = Book(bids={0.9: 10}, asks={0.92: 50.0})
    asyncio.run(bot._execute(w, sig, 5, _full_ctx()))
    kinds = [k for k, _ in j.ev]
    assert kinds == ["fill", "lock_entry"]
    e = dict(j.ev)["lock_entry"]
    assert e["key"] == w.key and e["side"] == "U" and e["price"] == 0.92 and e["shares"] == 5.0
    assert e["limit"] == 0.93                      # the cap that was sent (0.01 grid), not the raw 0.9349
    assert e["p"] == 0.96 and e["ask"] == 0.92 and e["ms"] >= 0
    # a miss: the ask is gone when the order arrives
    st2 = State(["btc"])
    bot, j, w = _bot(tmp_path / "b", PaperTaker(st2, exec_ms=0))
    st2.books["u"] = Book(bids={0.9: 10}, asks={0.97: 50.0})
    asyncio.run(bot._execute(w, sig, 5, _full_ctx()))
    assert [k for k, _ in j.ev] == ["lock_miss"]
    m = dict(j.ev)["lock_miss"]
    assert m["limit"] == 0.93 and m["error"] is False and not bot.entries.get(w.key)
    assert bot.next_try[w.key] < float("inf")       # a miss may be retried


class _Rej(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status = status


class _FakClient:
    """polymarket AsyncSecureClient stand-in: `script` is a list of responses / exceptions for place_market_order."""
    def __init__(self, script):
        self.script, self.placed, self.created = list(script), [], []

    async def place_market_order(self, **kw):
        self.placed.append(kw)
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def create_market_order(self, **kw):
        self.created.append(kw)
        return object()


NO_MATCH = "no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found."


def _ok(spent, shares, status="matched"):
    import types
    return types.SimpleNamespace(ok=True, making_amount=spent, taking_amount=shares, order_id="0xabc", status=status)


def test_taker_live_broker_tells_a_clean_miss_from_an_unknown_outcome():
    """The CLOB answers a FAK that matched nothing with HTTP 400 and the SDK raises: that is a miss, not an error
    (live 9-10 Nov: 26 of 42 orders ended as order_error and each one blocked its window)."""
    import types
    from pmbot.bot.live import TakerLiveBroker
    j = _J()
    c = _FakClient([_Rej(NO_MATCH, 400), _ok("4.52", "4.967"), _Rej("invalid order payload", 400), TimeoutError("no reply"),
                    types.SimpleNamespace(ok=False, code="not_enough_balance", message="not enough balance / allowance"),
                    _ok("0", "0", status="delayed")])
    b = TakerLiveBroker(5.0, journal=j, client=c)
    assert asyncio.run(b.place_fak("tok", "btc-1", "U", 0.91, 4.55)) is None
    assert b.last_reject == "fak_not_filled"
    k, rec = j.ev[-1]
    assert k == "order_rejected" and rec["code"] == "fak_not_filled" and rec["latency_ms"] >= 0
    r = asyncio.run(b.place_fak("tok", "btc-1", "U", 0.91, 4.55))
    assert r["size"] == pytest.approx(4.967) and r["price"] == pytest.approx(4.52 / 4.967) and b.last_reject == ""
    assert j.ev[-1][0] == "order_filled" and j.ev[-1][1]["latency_ms"] >= 0
    assert c.placed[0]["max_price"] == "0.91" and c.placed[0]["order_type"] == "FAK"
    for exc in (_Rej, TimeoutError):                       # another 400, a timeout: outcome unknown -> raise
        with pytest.raises(exc):
            asyncio.run(b.place_fak("tok", "btc-1", "U", 0.91, 4.55))
        assert j.ev[-1][0] == "order_failed"
    assert asyncio.run(b.place_fak("tok", "btc-1", "U", 0.91, 4.55)) is None and b.last_reject == "not_enough_balance"
    assert asyncio.run(b.place_fak("tok", "btc-1", "U", 0.91, 4.55)) is None and b.last_reject == "pending:delayed"
    assert j.ev[-1][0] == "order_pending"                  # accepted, not matched yet: not a fill and not a miss
    # the private key / signed order never reach the journal
    assert "0x1" not in str(j.ev)


def _live_bot(tmp_path, script):
    from pmbot.bot.lock import LiveTaker
    from pmbot.bot.live import TakerLiveBroker
    lt = LiveTaker.__new__(LiveTaker)
    j = _J()
    lt.b = TakerLiveBroker(5.0, journal=j, client=_FakClient(script))
    bot, _, w = _bot(tmp_path, lt)
    bot.j = j
    return bot, j, w, lt


def test_live_retries_after_a_clean_miss_and_blocks_the_window_otherwise(tmp_path):
    import types
    sig = {"side": "U", "limit": 0.91, "ask": 0.91, "p": 0.93}
    # 1. nothing matched: no order_error, the window stays open for the next signal (like the paper twin)
    bot, j, w, lt = _live_bot(tmp_path / "a", [_Rej(NO_MATCH, 400), _ok("4.52", "4.967")])
    asyncio.run(bot._execute(w, sig, 5, _full_ctx()))
    kinds = [k for k, _ in j.ev]
    assert "order_error" not in kinds and "window_blocked" not in kinds and "lock_miss" in kinds
    assert bot.next_try[w.key] < float("inf") and not bot.entries.get(w.key)
    asyncio.run(bot._execute(w, sig, 5, _full_ctx()))                       # the retry fills
    assert bot.entries[w.key][0]["shares"] == pytest.approx(4.967) and len(lt.b.client.placed) == 2
    # 2. refused for another reason, still pending, or no answer: never a second order in this window
    for i, script in enumerate(([types.SimpleNamespace(ok=False, code="not_enough_balance", message="x")],
                                [_ok("0", "0", status="delayed")], [TimeoutError("no reply")])):
        bot, j, w, lt = _live_bot(tmp_path / f"c{i}", script)
        asyncio.run(bot._execute(w, sig, 5, _full_ctx()))
        kinds = [k for k, _ in j.ev]
        assert "order_error" in kinds and "window_blocked" in kinds, kinds
        assert bot.next_try[w.key] == float("inf")


def test_prewarm_once_per_window_and_only_with_a_live_broker(tmp_path):
    """The SDK looks up a market (2 HTTP round trips) before the first order for a token: do it ~40 s ahead."""
    bot, j, w, lt = _live_bot(tmp_path / "a", [])

    async def go(b):
        b._prewarm(w.key, w.token_up)
        b._prewarm(w.key, w.token_up)                    # the discover loop comes back every 2 s
        await asyncio.sleep(0.01)
    asyncio.run(go(bot))
    c = lt.b.client
    assert len(c.created) == 1 and c.created[0]["asset_id"] == "u" and not c.placed     # signed, never posted
    assert [k for k, _ in j.ev] == ["prewarm"] and j.ev[0][1]["ms"] >= 0
    bot2, j2, w2 = _bot(tmp_path / "b", PaperTaker(State(["btc"])))
    asyncio.run(go(bot2))
    assert not j2.ev and not bot2.warmed                 # paper: nothing to warm


def test_open_risk_counts_orders_in_flight(tmp_path, monkeypatch):
    """All coins become eligible in the same tick at rel_lo and an order takes ~0.3 s: with max_open_usd = 5 (one
    entry at a time) only one order may be out, the one with the widest margin."""
    import time as _t
    from pmbot.bot import lock as lockmod
    from pmbot.bot.state import Window

    class Slow:
        mode = "paper"

        def __init__(self):
            self.calls, self.gate = [], None

        async def buy(self, token, key, side, limit, shares):
            self.calls.append(key)
            await self.gate.wait()
            return 0.91, 5.0, 0.03

    br = Slow()
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""},
                                      "lock": {"shares": 5.0}})          # the 5-share numbers below (L3 has 2 since reports/70)
    assert cfg.risk.max_open_entries == 1                 # one entry at a time (by count since reports/67, $10 fits a 10-share entry)
    j = _J()
    bot = LockBot(cfg, j, broker=br, tables=_Tab())
    now = int(_t.time() * 1000)
    start = now // 1000 - 250                           # every window is at rel = 250 s
    margin = {"btc": 0.02, "eth": 0.05, "sol": 0.03}
    for a in margin:
        bot.st.add_window(Window(asset=a, start=start, slug=a, condition_id=a, token_up=a + "u", token_down=a + "d"))
    bot.st.feed_alive["binance"] = now

    def fake_decide(c, tables, asset, *args, **kw):
        return {"side": "U", "limit": 0.91, "ask": 0.91, "p": 0.91 + margin[asset]}, {"rel": 250.0, "side": "U"}
    monkeypatch.setattr(lockmod, "decide", fake_decide)

    async def go():
        br.gate = asyncio.Event()
        await bot.decide_once(now)
        await asyncio.sleep(0)                           # the order task starts and waits for the exchange
        assert br.calls == [f"eth-{start}"]              # one order out, the widest margin first
        assert bot.pending == {f"eth-{start}": pytest.approx(4.55)}
        await bot.decide_once(now + 50)                  # next tick, the order is still in flight
        await asyncio.sleep(0)
        assert len(br.calls) == 1
        br.gate.set()
        await asyncio.sleep(0.01)
        assert not bot.pending and bot.open_cost() == pytest.approx(4.58)
        await bot.decide_once(now + 100)                 # filled: the entry itself now holds the limit
        await asyncio.sleep(0.01)
        assert len(br.calls) == 1
    asyncio.run(go())
    assert bot.stats["skipped_risk"] >= 2
    assert any(k == "lock_state" and kw.get("why") == "risk:max_open" for k, kw in j.ev)


def test_provisional_result_forward_fills_dropped_prints(tmp_path):
    """RTDS drops ~1% of the seconds: a third of the windows miss a closing print. The provisional result (day stop,
    open-risk slot) must not wait for Gamma because of one missing second, unless the result is too close to call."""
    from pmbot.lockmodel import close_from_prints
    K = 100.0

    def bot_with(cl, tmp):
        bot, j, w = _bot(tmp, PaperTaker(State(["btc"])))
        bot.strikes[w.key] = K
        bot.st.prices["btc"].cl_by_sec.update(cl)
        bot.entries[w.key] = [{"side": "U", "shares": 5.0, "price": 0.91, "cost": 4.55, "fee": 0.03, "t": 0,
                               "limit": 0.91, "p": 0.92}]
        return bot, j, w

    full = {T + s: K * (1 + 3e-4) for s in range(228, 298)}            # +3 bp: Up wins clearly
    c, miss = close_from_prints(full, T)
    assert miss == 0 and c == pytest.approx(K * (1 + 3e-4))
    gap = dict(full)
    gap.pop(T + 260)
    bot, j, w = bot_with(gap, tmp_path / "a")
    bot._provisional(w.key)
    assert bot.risk["provisional"][w.key] == pytest.approx(5 - 4.58) and bot.open_cost() == 0.0
    assert dict(j.ev)["provisional"]["missing"] == 1
    # too close to call with a filled print: wait for Gamma
    close = {T + s: K * (1 + 2e-5) for s in range(228, 298)}           # +0.2 bp
    close.pop(T + 260)
    bot, j, w = bot_with(close, tmp_path / "b")
    bot._provisional(w.key)
    assert w.key not in bot.risk["provisional"] and bot.open_cost() == pytest.approx(4.58)
    # the same margin with every print present is a result
    bot, j, w = bot_with({T + s: K * (1 + 2e-5) for s in range(228, 298)}, tmp_path / "c")
    bot._provisional(w.key)
    assert bot.risk["provisional"][w.key] > 0
    # more than 3 missing: no result
    many = {s: v for s, v in full.items() if s not in (T + 250, T + 251, T + 252, T + 253)}
    bot, j, w = bot_with(many, tmp_path / "d")
    bot._provisional(w.key)
    assert w.key not in bot.risk["provisional"]


def test_l3_on_15m_is_the_live_rule_with_another_duration():
    """pmbot-lock-15-tier (reports/52): the L3 rule and its one-entry-at-a-time limit on 15m windows. It keeps the four
    tiers live had until reports/59 (paper: the place where the p 0.90-0.95 tier is still measured with the filters on)."""
    l3 = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides=FOUR_TIERS)
    l15 = load_lock_config(os.path.join(ROOT, "config", "lock15_tier.toml"))
    assert l15.execution.mode == "paper" and l15.lock.duration == 900 and l15.lock.entry == "taker"
    a, b = dict(vars(l3.lock)), dict(vars(l15.lock))
    for k in ("duration", "hedge_p", "hedge_margin", "hedge_slip",   # the insurance (reports/64) is 5m-only: measured on 5m books
              "big_shares", "big_shares_p", "shares"):              # 10 shares (reports/67), 2 below p 0.97 (reports/70): 15m keeps 5
        a.pop(k), b.pop(k)
    assert a == b, "L3-15 must keep the L3 rule"
    assert l15.risk.max_open_usd == 5.0 and l15.lock.big_shares == 0 and l3.risk.max_open_entries == 1   # one entry at a time
    assert l15.risk.state_file not in ("", l3.risk.state_file) and l15.journal_dir != l3.journal_dir


def test_lm3_bids_the_tier_price_of_the_model_probability():
    """LM3 (reports/52): the L3 tiers as a resting bid. Paper only, like every maker entry of the lock bot."""
    from pmbot.bot.lock import maker_bid
    l3 = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides=FOUR_TIERS)   # LM3 keeps four tiers
    lm2 = load_lock_config(os.path.join(ROOT, "config", "lock_maker2.toml"))
    lm3 = load_lock_config(os.path.join(ROOT, "config", "lock_maker_tier.toml"))
    c = lm3.lock
    assert lm3.execution.mode == "paper" and c.entry == "maker" and c.tiers == l3.lock.tiers and c.theta == l3.lock.theta
    assert c.adv3_bp == lm2.lock.adv3_bp == 1.0                     # the maker's tighter Binance guard
    for k in ("agree", "dmin_bp", "adv1_bp", "jump_bp", "a_min", "a_max", "rel_lo", "rel_hi", "assets"):    # shares: LM3 keeps 5 (reports/70)
        assert getattr(c, k) == getattr(l3.lock, k), k
    assert lm3.risk.state_file not in ("", l3.risk.state_file, lm2.risk.state_file)
    assert len({lm3.journal_dir, l3.journal_dir, lm2.journal_dir}) == 3
    # the bid is the tier limit of p, one cent under the ask at most, nothing below the lowest tier
    assert maker_bid(c, 0.995, None) == 0.98 and maker_bid(c, 0.98, None) == 0.95
    assert maker_bid(c, 0.96, None) == 0.93 and maker_bid(c, 0.92, None) == 0.91
    assert maker_bid(c, 0.89, None) is None
    assert maker_bid(c, 0.92, 0.91) == 0.90 and maker_bid(c, 0.92, 0.905) is None    # post-only: one cent under the ask


def test_sigma_floor_is_in_live_and_its_twin():
    """reports/52: live L3 and its paper twins (5m and 15m) run with sigma never below 0.5 bp. It only makes the rule
    stricter (a floored sigma can only lower the model probability), so it is not a risk increase."""
    for name in ("lock_live.toml", "lock_tier.toml", "lock15_tier.toml"):
        c = load_lock_config(os.path.join(ROOT, "config", name)).lock
        assert c.sigma_floor_bp == 0.5, name
        assert c.floor_free_p == 0.99 and c.shock_bp == 2.5 and c.shock_s == 10.0, name      # reports/55
    for name in ("lock.toml", "lock_wide.toml", "lock_tier_wide.toml"):
        assert load_lock_config(os.path.join(ROOT, "config", name)).lock.sigma_floor_bp == 0.0, name
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"sigma_floor_bp": -0.1}})
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"sigma_floor_bp": 50.0}})


def test_sigma_floor_stops_a_small_lead_in_a_calm_market(tables):
    """Calm market: EWMA sigma 0.15 bp, the price 1.5 bp past the strike with 46 prints to go. Plain L3 calls it
    decided; with the 0.5 bp floor the model is no longer sure and stays out. A 6 bp lead is still bought."""
    from pmbot.bot.lock import decide_state
    off = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides={"lock": {"sigma_floor_bp": 0.0}}).lock
    on = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml")).lock
    bn = 75000.0
    K, now_s, cl, last = scenario(bp=1.5, rel=252.0)
    args = (tables, "btc", T, K, int(now_s * 1000), cl, last, 0.15e-4, bn, bn, bn, bn)
    side0, p0, ctx0 = decide_state(off, *args)
    side1, p1, ctx1 = decide_state(on, *args)
    assert side0 == "U" and p0 >= 0.97, ctx0
    assert side1 is None and ctx1["why"] in ("p", "agree") and ctx1["p"] < p0, ctx1
    assert ctx1["sigma_bp"] == 0.5 and ctx1["sigma_raw_bp"] == 0.15 and ctx0["sigma_bp"] == 0.15
    sig, _ = decide(off, *args, (0.91, 100.0), (0.10, 100.0))
    assert sig and sig["side"] == "U"
    sig, _ = decide(on, *args, (0.91, 100.0), (0.10, 100.0))
    assert sig is None
    K, now_s, cl, last = scenario(bp=6.0, rel=252.0)
    sig, ctx = decide(on, tables, "btc", T, K, int(now_s * 1000), cl, last, 0.15e-4, bn, bn, bn, bn, (0.91, 100.0), (0.10, 100.0))
    assert sig and sig["side"] == "U", ctx


def test_sigma_floor_changes_nothing_above_the_floor(tables):
    from pmbot.bot.lock import decide_state
    off = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides={"lock": {"sigma_floor_bp": 0.0}}).lock
    on = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml")).lock
    bn = 75000.0
    K, now_s, cl, last = scenario(bp=5.0, rel=262.0)
    for sg in (0.5e-4, 0.8e-4, 1.5e-4):
        a = decide_state(off, tables, "btc", T, K, int(now_s * 1000), cl, last, sg, bn, bn, bn, bn)
        b = decide_state(on, tables, "btc", T, K, int(now_s * 1000), cl, last, sg, bn, bn, bn, bn)
        assert a[0] == b[0] and a[1] == b[1] and a[2]["p_cl"] == b[2]["p_cl"]


def test_live_fak_asks_for_whole_shares(tmp_path):
    """The budget handed to the SDK must cover the notional plus the taker fee, or the SDK cuts the notional and the order is
    for 4.97 shares (5 decimals on a 0.001-tick market -> "invalid amounts", live 9-10 Nov). Per-entry cost stays under $5."""
    from pmbot.bot.lock import fak_budget
    from pmbot.fees import fee_per_share
    for cap in (0.91, 0.93, 0.95, 0.98):
        usd = round(5 * cap, 2)
        b = fak_budget(5, cap)
        assert b > usd + 5 * fee_per_share(cap), cap             # strictly above notional + fee: the SDK keeps the notional
        assert b <= usd + 5 * fee_per_share(cap) + 0.02 and b <= 5.0, cap
        assert round(b * 100) == pytest.approx(b * 100)          # whole cents
        assert (usd / cap) == pytest.approx(5.0)                  # notional / cap = exactly the configured shares
    sig = {"side": "U", "limit": 0.95, "ask": 0.95, "p": 0.98}
    bot, j, w, lt = _live_bot(tmp_path, [_ok("4.75", "5")])
    asyncio.run(bot._execute(w, sig, 5, _full_ctx()))
    sent = lt.b.client.placed[0]
    assert sent["amount"] == "4.75" and sent["max_spend"] == f"{fak_budget(5, 0.95):.2f}" == "4.78" and sent["max_price"] == "0.95"
    assert bot.entries[w.key][0]["shares"] == 5.0


def test_ping_uses_the_sdk_client_and_an_error_is_not_fatal(tmp_path):
    bot, j, w, lt = _live_bot(tmp_path, [])

    class C:
        n = 0

        async def get_midpoint(self, token_id=None):
            C.n += 1
            if C.n == 2:
                raise TimeoutError("x")
            return 0.5
    lt.b.client = C()
    assert asyncio.run(lt.b.ping("tok")) >= 0 and asyncio.run(lt.b.ping("tok")) is None


def test_warm_loop_pings_only_in_the_closing_minute_of_a_live_bot(tmp_path):
    import time as _t
    from pmbot.bot.state import Window
    bot, j, w, lt = _live_bot(tmp_path / "a", [])
    calls = []

    async def ping(token):
        calls.append(token)
        bot._stop = True
        return 27.0
    lt.ping = ping
    start = int(_t.time()) - 250                         # rel 250: inside [rel_lo - 15, rel_hi]
    bot.st.windows = {"btc-x": Window(asset="btc", start=start, slug="s", condition_id="c", token_up="U1", token_down="D1")}
    asyncio.run(bot.warm_loop(period_s=0.01))
    assert calls == ["U1"] and bot.stats["ping_ms"] == 27.0
    bot._stop, calls[:] = False, []
    bot.st.windows = {"btc-x": Window(asset="btc", start=int(_t.time()) - 100, slug="s", condition_id="c", token_up="U1",
                                      token_down="D1")}

    async def go():
        t = asyncio.create_task(bot.warm_loop(period_s=0.01))
        await asyncio.sleep(0.05)
        bot._stop = True
        await t
    asyncio.run(go())
    assert calls == []                                   # mid-window: no requests
    bot2, j2, w2 = _bot(tmp_path / "b", PaperTaker(State(["btc"])))
    asyncio.run(bot2.warm_loop(period_s=0.01))           # paper: returns at once


def test_top_tier_is_judged_by_the_unfloored_model(tables):
    """reports/55: the sigma floor must not take the original L rule away. Calm market (sigma 0.12 bp), 1.6 bp lead late in
    the window: the unfloored model is >= 0.99 sure (L buys up to 0.98); the floored one alone would ask for a cheaper tier."""
    import dataclasses
    from pmbot.bot.lock import decide_state
    on = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml")).lock
    off = dataclasses.replace(on, floor_free_p=0.0)
    bn = 75000.0
    found = False
    for bp in (1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.4):
        K, now_s, cl, last = scenario(bp=bp, rel=285.0)
        args = (tables, "btc", T, K, int(now_s * 1000), cl, last, 0.12e-4, bn, bn, bn, bn)
        s1, p1, c1 = decide_state(on, *args)
        s0, p0, c0 = decide_state(off, *args)
        if s1 == "U" and p1 >= 0.99 and (s0 is None or p0 < 0.99):
            found = True
            assert c1["sigma_bp"] == 0.12                      # judged with the raw sigma
            sig, _ = decide(on, *args, (0.98, 50.0), (0.03, 50.0))
            assert sig and sig["limit"] == 0.98
            sig, _ = decide(off, *args, (0.98, 50.0), (0.03, 50.0))
            assert sig is None
            break
    assert found, "no scenario where the raw model is >= 0.99 and the floored one is not"
    # a weak state is not rescued: below 0.99 raw the floor rules as before
    K, now_s, cl, last = scenario(bp=1.5, rel=252.0)
    args = (tables, "btc", T, K, int(now_s * 1000), cl, last, 0.15e-4, bn, bn, bn, bn)
    a, b = decide_state(on, *args), decide_state(off, *args)
    if a[2]["p"] < 0.99:
        assert a[0] == b[0] and a[2]["p"] == b[2]["p"]


def test_shock_blocks_an_entry_after_a_binance_jump(tables):
    """reports/55 (live loss 10 Nov 10:25): a 3 s Binance move > 2.5 bp within the last 10 s = no entry, either direction."""
    import dataclasses
    from pmbot.bot.lock import bn_shock_bp, hist_samples, shock_from_samples
    c = dataclasses.replace(load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml")).lock, shock_sigmas=0.0)   # the fixed threshold
    K, now_s, cl, last = scenario(bp=6.0, rel=262.0)
    now = int(now_s * 1000)
    bn = 75000.0
    args = (c, tables, "btc", T, K, now, cl, last, 0.6e-4, bn, bn, bn, bn, (0.91, 100.0), (0.10, 100.0))
    sig, ctx = decide(*args)                                   # no shock information (old callers): unchanged
    assert sig and "shock_bp" not in ctx
    sig, ctx = decide(*args, shock=1.0)
    assert sig and ctx["shock_bp"] == 1.0
    calls = []
    sig, ctx = decide(*args, shock=lambda: calls.append(1) or 2.96)
    assert sig is None and ctx["why"] == "shock" and calls == [1]
    # lazy: a state that fails earlier never asks for the shock
    K2, now2, cl2, last2 = scenario(bp=0.2, rel=262.0)
    decide(c, tables, "btc", T, K2, int(now2 * 1000), cl2, last2, 0.6e-4, bn, bn, bn, bn, (0.91, 100.0), (0.10, 100.0),
           shock=lambda: calls.append(2) or 9.0)
    assert calls == [1]
    # the measure: ticks (rx, mid); a -3 bp move over 3 s that ended 6 s ago, flat since
    hist = [(now - 20000 + i * 100, 100.0) for i in range(110)]                      # flat until now - 9 s
    hist += [(now - 9000 + i * 100, 100.0 * (1 - 3e-4 * (i + 1) / 30)) for i in range(30)]   # falls 3 bp until now - 6 s
    hist += [(now - 6000 + i * 100, 100.0 * (1 - 3e-4)) for i in range(61)]
    v = shock_from_samples(hist_samples(hist, now, 27), 10.0)
    assert 2.9 < v < 3.05
    assert shock_from_samples(hist_samples(hist, now, 11), 2.0) == 0.0               # every 3 s window of the last 2 s is flat

    def bn_at(t):
        best = None
        for rx, mid in hist:
            if rx <= t:
                best = mid
        return best if best is not None else hist[0][1]
    assert bn_shock_bp(bn_at, now, 10.0) == pytest.approx(v)                         # replay and bot measure the same
    assert shock_from_samples(hist_samples([], now, 27), 10.0) == 0.0


def test_lock_bot_passes_the_cached_shock(tmp_path):
    bot, _, w = _bot(tmp_path, PaperTaker(State(["btc"])))
    px = bot.st.prices["btc"]
    now = 1_000_000_000
    for i in range(200):
        px.bn_hist.append((now - 20000 + i * 100, 100.0 if i < 150 else 100.04))     # +4 bp jump 5 s ago
    assert bot._shock("btc", now) == pytest.approx(4.0, abs=0.05)
    px.bn_hist.clear()
    assert bot._shock("btc", now + 100) == pytest.approx(4.0, abs=0.05)             # cached for 200 ms
    assert bot._shock("btc", now + 300) == 0.0


def test_l99_is_paper_and_buys_the_sure_side_up_to_099(tables):
    """reports/55: the competitor zone. Paper only; 0.99 needs p >= 0.9947, and the unfloored model decides."""
    c = load_lock_config(os.path.join(ROOT, "config", "lock_99.toml"))
    assert c.execution.mode == "paper" and c.lock.sigma_floor_bp == 0.0 and not c.lock.tiers
    assert c.lock.a_min == 0.96 and c.lock.a_max == 0.99 and c.lock.theta == 0.99 and c.lock.shock_bp == 2.5
    K, now_s, cl, last = scenario(bp=3.0, rel=292.0)
    bn = 75000.0
    args = (c.lock, tables, "btc", T, K, int(now_s * 1000), cl, last, 0.15e-4, bn, bn, bn, bn)
    sig, ctx = decide(*args, (0.99, 500.0), (0.02, 500.0))
    assert sig and sig["side"] == "U" and ctx["p"] >= 0.9947 and round(tick_floor(sig["limit"], 0.01), 2) == 0.99, ctx
    sig, ctx = decide(*args, (0.95, 500.0), (0.06, 500.0))
    assert sig is None and ctx["why"] == "ask_too_low"


def test_l99_live_is_the_paper_rule_with_small_real_money_limits():
    """reports/56: the competitor zone on real money at the smallest stake, next to live L3. One loss stops it for the day."""
    paper = load_lock_config(os.path.join(ROOT, "config", "lock_99.toml"))
    live = load_lock_config(os.path.join(ROOT, "config", "lock_99_live.toml"))
    l3 = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    assert live.execution.mode == "live" and paper.execution.mode == "paper"
    assert live.lock == paper.lock, "L99 live must stay identical to its paper twin pmbot-lock-99"
    assert live.lock.shares <= 5 and live.risk.max_usd_per_entry <= 5.0 and live.risk.max_open_usd <= 5.0
    assert live.risk.resume_next_day is False
    assert live.risk.daily_loss_limit < 5 * live.lock.a_min       # below the smallest possible loss: one loss = stop
    assert paper.risk.max_open_usd == live.risk.max_open_usd and paper.risk.daily_loss_limit == live.risk.daily_loss_limit
    # separate books: the two live bots must never share a state file or a journal
    assert live.risk.state_file not in ("", l3.risk.state_file, paper.risk.state_file)
    assert len({live.journal_dir, l3.journal_dir, paper.journal_dir}) == 3


def test_polybolt_paper_bot_differs_from_the_twin_only_by_the_feed():
    """reports/57: pmbot-lock-tier-pb is the A/B of the new price socket: the same rule and limits as the live twin."""
    tier = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"))
    pb = load_lock_config(os.path.join(ROOT, "config", "lock_tier_pb.toml"))
    live = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    assert pb.execution.mode == "paper" and pb.lock == tier.lock
    for k in ("max_usd_per_entry", "max_open_usd", "daily_loss_limit", "resume_next_day"):
        assert getattr(pb.risk, k) == getattr(tier.risk, k), k
    # reports/66: live moved to PolyBolt after the A/B (more entries, fewer misses); pmbot-lock-tier stays the RTDS reference
    assert pb.execution.cl_feed == "both" and tier.execution.cl_feed == "rtds" and live.execution.cl_feed == "both"
    assert pb.execution.paper_exec_ms == tier.execution.paper_exec_ms
    assert pb.risk.state_file not in ("", tier.risk.state_file) and pb.journal_dir != tier.journal_dir
    with pytest.raises(ValueError):
        load_lock_config(overrides={"execution": {"cl_feed": "fast"}})


def test_polybolt_events_and_first_arrival_wins():
    import datetime as dt
    import types
    from pmbot.bot.feeds import pb_event_print
    from pmbot.bot.state import AssetPrices
    ts = dt.datetime(2026, 11, 10, 12, 0, 5, tzinfo=dt.timezone.utc)
    ev = types.SimpleNamespace(type="update", topic="prices.crypto", payload=types.SimpleNamespace(symbol="btcusd", value="74000.5", timestamp=ts))
    assert pb_event_print(ev, ["btc", "eth"]) == ("btc", 74000.5, int(ts.timestamp() * 1000))
    assert pb_event_print(types.SimpleNamespace(type="subscribe", topic="prices.crypto", payload=ev.payload), ["btc"]) is None
    assert pb_event_print(types.SimpleNamespace(type="update", topic="prices.crypto.twap", payload=ev.payload), ["btc"]) is None
    assert pb_event_print(ev, ["eth"]) is None                                       # not our asset
    assert pb_event_print(types.SimpleNamespace(type="update", topic="prices.crypto", payload=None), ["btc"]) is None
    # the same print from the second (slower) feed must not move the arrival time of the first
    px = AssetPrices()
    px.on_chainlink(100.0, 5000, 6070)
    px.on_chainlink(100.0, 5000, 6340)
    assert px.cl_last == (100.0, 5000, 6070)
    px.on_chainlink(100.2, 6000, 7400)
    assert px.cl_last == (100.2, 6000, 7400) and px.cl_by_sec == {5: 100.0, 6: 100.2}
    px.on_chainlink(99.9, 4000, 7500)                                                # a late older print fills its second only
    assert px.cl_last[1] == 6000 and px.cl_by_sec[4] == 99.9


def test_shock_threshold_grows_with_volatility(tables):
    """reports/58: a fixed 2.5 bp pause is a shock filter at sigma 0.5 bp and an off switch at sigma 2 bp (10 Nov from 13:00
    UTC: on 50-95 % of the time, no live entry for hours). With shock_sigmas the threshold is max(shock_bp, k sigma sqrt(3))."""
    c = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml")).lock
    assert c.shock_bp == 2.5 and c.shock_sigmas == 3.0
    bn = 75000.0

    def run(sigma, shock, bp):
        K, now_s, cl, last = scenario(bp=bp, rel=262.0)
        return decide(c, tables, "btc", T, K, int(now_s * 1000), cl, last, sigma, bn, bn, bn, bn, (0.91, 100.0), (0.10, 100.0),
                      shock=shock)
    # calm market (sigma at the 0.5 bp floor): the threshold stays ~2.6 bp, the live loss of 10 Nov (2.96 bp) is still blocked
    sig, ctx = run(0.45e-4, 2.96, 6.0)
    assert sig is None and ctx["why"] == "shock" and 2.5 <= ctx["shock_lim"] <= 2.6
    sig, ctx = run(0.45e-4, 2.4, 6.0)
    assert sig and 2.5 <= ctx["shock_lim"] <= 2.6
    # volatile market (sigma 2 bp): a 4 bp move in 3 s is ordinary (1.15 sigma), a 12 bp move is a shock
    sig, ctx = run(2.0e-4, 4.0, 40.0)
    assert sig and ctx["shock_lim"] == pytest.approx(3 * 2.0 * math.sqrt(3), abs=1e-3)
    sig, ctx = run(2.0e-4, 12.0, 40.0)
    assert sig is None and ctx["why"] == "shock"
    # off by default: the fixed threshold
    import dataclasses
    c0 = dataclasses.replace(c, shock_sigmas=0.0)
    K, now_s, cl, last = scenario(bp=40.0, rel=262.0)
    sig, ctx = decide(c0, tables, "btc", T, K, int(now_s * 1000), cl, last, 2.0e-4, bn, bn, bn, bn, (0.91, 100.0), (0.10, 100.0),
                      shock=4.0)
    assert sig is None and ctx["why"] == "shock" and "shock_lim" not in ctx
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"shock_sigmas": 1.0}})
    # the sniper (L99, a loss there costs 107 wins) keeps the fixed threshold
    assert load_lock_config(os.path.join(ROOT, "config", "lock_99_live.toml")).lock.shock_sigmas == 0.0


def test_bottom_tier_only_late_in_the_window(tables):
    """reports/60: with 40-57 closing prints still unknown the bottom tier (p 0.90-0.95 at an ask <= 0.91) lost 7 % on the tapes of
    1-9 Nov and 4 of the 5 live losses of 9-10 Nov; from second 260 on it lost 2 of 75. lock.late_tier_rel keeps that tier for
    the late seconds only. It is OFF in every config: live and its twins have no bottom tier at all since reports/59."""
    import dataclasses
    import glob
    from pmbot.bot.lock import tier_limit
    off = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides=FOUR_TIERS).lock
    assert off.late_tier_rel == 0.0 and off.late_tier_p == 0.95
    c = dataclasses.replace(off, late_tier_rel=260.0)
    assert tier_limit(off, 0.92, 244.0) == 0.91                  # off: the second does not matter
    assert tier_limit(c, 0.92) == 0.91 and tier_limit(c, 0.92, 262.0) == 0.91 and tier_limit(c, 0.92, 244.0) is None
    assert tier_limit(c, 0.96, 244.0) == 0.93 and tier_limit(c, 0.98, 244.0) == 0.95 and tier_limit(c, 0.995, 244.0) == 0.98
    bn = 75000.0

    def run(cfg, rel, bp, ask):
        K, now_s, cl, last = scenario(bp=bp, rel=rel)
        return decide(cfg, tables, "btc", T, K, int(now_s * 1000), cl, last, 0.6e-4, bn, bn, bn, bn, (ask, 100.0), (0.10, 100.0),
                      shock=0.0)
    bp = next(b / 10 for b in range(10, 200) if 0.91 <= run(off, 244.0, b / 10, 0.90)[1].get("p", 0) < 0.95)   # a bottom-tier state
    sig, ctx = run(off, 244.0, bp, 0.90)
    assert sig and sig["limit"] == 0.91                       # four tiers: the live loss of 10 Nov 18:04 (second 244)
    sig, ctx = run(c, 244.0, bp, 0.90)
    assert sig is None and ctx["why"] == "tier_early" and 0.90 <= ctx["p"] < 0.95
    late = next(b / 10 for b in range(10, 200) if 0.91 <= run(off, 262.0, b / 10, 0.90)[1].get("p", 0) < 0.95)
    sig, ctx = run(c, 262.0, late, 0.90)
    assert sig and sig["limit"] == 0.91                       # the same tier later in the window
    big = next(b / 10 for b in range(10, 400) if run(off, 244.0, b / 10, 0.93)[1].get("p", 0) >= 0.955)
    sig, ctx = run(c, 244.0, big, 0.93)
    assert sig and sig["limit"] >= 0.93                       # the upper tiers enter early as before
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"late_tier_rel": 200.0}})
    for path in glob.glob(os.path.join(ROOT, "config", "lock*.toml")):      # on in one paper experiment only: live is an owner's decision
        assert load_lock_config(path).lock.late_tier_rel == (260.0 if path.endswith("lock_tier_late.toml") else 0.0), path
    # the experiment = the paper twin of live + the fourth tier from second 260; paper, and it does not stop for the day
    import dataclasses as dc
    twin = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"))
    exp = load_lock_config(os.path.join(ROOT, "config", "lock_tier_late.toml"))
    assert exp.execution.mode == "paper" and exp.risk.resume_next_day
    assert dc.replace(exp.lock, theta=twin.lock.theta, tiers=twin.lock.tiers, late_tier_rel=0.0) == twin.lock
    assert exp.lock.theta == 0.90 and sorted(map(tuple, exp.lock.tiers)) == sorted(map(tuple, FOUR_TIERS["lock"]["tiers"]))
    assert dc.replace(exp.risk, daily_loss_limit=twin.risk.daily_loss_limit, state_file=twin.risk.state_file) == twin.risk
    assert exp.journal_dir != twin.journal_dir and exp.risk.state_file != twin.risk.state_file
    live = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml")).lock
    assert live.theta == 0.95 and min(t[0] for t in live.tiers) == 0.95    # reports/59: live has no tier below 0.95


def test_insurance_configs_and_validation():
    """reports/64: the insurance on a reversal runs in live L3 and every paper twin of its rule; the references stay without it."""
    import glob
    on = {"lock_live.toml", "lock_tier.toml", "lock_tier_pb.toml", "lock_tier_late.toml"}   # 15m: measured without it (reports/65)
    for path in glob.glob(os.path.join(ROOT, "config", "lock*.toml")):
        c = load_lock_config(path).lock
        if os.path.basename(path) in on:
            assert (c.hedge_p, c.hedge_margin, c.hedge_slip) == (0.65, 0.20, 0.02), path
        else:
            assert c.hedge_p == 0.0, path                           # L, L2, L4, L99, makers: no insurance
    live = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    assert live.risk.max_usd_per_entry <= 10.0 and live.risk.max_open_usd <= 10.0 and live.risk.daily_loss_limit <= 10.0   # reports/67
    assert live.risk.max_open_entries == 1
    for bad in ({"hedge_p": 0.4}, {"hedge_p": 1.0}, {"hedge_p": 0.65, "hedge_margin": 0.6}, {"hedge_p": 0.65, "hedge_slip": 0.2},
                {"hedge_p": 0.65, "max_entries_per_window": 2}):
        with pytest.raises(ValueError):
            load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"), overrides={"lock": bad})
    with pytest.raises(ValueError):
        load_lock_config(os.path.join(ROOT, "config", "lock_maker_tier.toml"), overrides={"lock": {"hedge_p": 0.65}})


def test_insurance_buys_the_other_side_once_on_a_reversal(tmp_path, monkeypatch):
    """reports/64: holding Up, the model turns to Down at 0.80 and Down sells at 0.55 <= 0.80 - 0.20: buy 5 Down capped at the seen
    ask + 2 c (0.57). Both sides held pay 5 at the end, so the window's result is known. Only once per window; not while the model is
    below hedge_p, our side is still favoured or the ask is above model - margin."""
    import time as _t
    import pmbot.bot.lock as lockmod
    from pmbot.bot.state import Window

    class Br:
        mode = "paper"

        def __init__(self):
            self.calls = []

        async def buy(self, token, key, side, limit, shares):
            self.calls.append((token, side, limit, shares))
            return limit - 0.02, shares, shares * 0.017

    br = Br()
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    j = _J()
    bot = LockBot(cfg, j, broker=br, tables=_Tab())
    now = int(_t.time() * 1000)
    start = now // 1000 - 270
    w = bot.st.add_window(Window(asset="btc", start=start, slug="s", condition_id="c", token_up="u", token_down="d"))
    bot.entries[w.key] = [{"side": "U", "shares": 5.0, "price": 0.93, "cost": 4.65, "fee": 0.0228, "t": now / 1000, "limit": 0.93,
                           "p": 0.955}]
    bot.st.feed_alive["binance"] = now
    bot.st.feed_alive["clob"]["btc"] = int(_t.time() * 1000) + 60_000
    model = {"side": "D", "p": 0.80}

    def fake_state(c, tables, asset, T, K, now_ms_, *a, **kw):
        if model["p"] < c.theta:
            return None, 0.0, {"rel": 270.0, "why": "p"}
        return model["side"], model["p"], {"rel": 270.0, "side": model["side"], "p": model["p"]}
    monkeypatch.setattr(lockmod, "decide_state", fake_state)

    async def tick():
        await bot.decide_once(now)
        await asyncio.sleep(0.01)
    bot.st.books["d"] = Book(bids={0.50: 10}, asks={0.65: 50.0})       # 0.65 > 0.80 - 0.20: too dear, wait
    asyncio.run(tick())
    assert br.calls == [] and any(k == "lock_state" and kw.get("why") == "hedge_wait" for k, kw in j.ev)
    model.update(side="U", p=0.97)                                     # our side still favoured: nothing
    bot.st.books["d"] = Book(bids={0.50: 10}, asks={0.55: 50.0})
    asyncio.run(tick())
    model.update(side="D", p=0.62)                                     # below hedge_p 0.65: nothing
    asyncio.run(tick())
    assert br.calls == []
    model.update(side="D", p=0.80)
    asyncio.run(tick())
    assert br.calls == [("d", "D", 0.57, 5)]                           # min(0.80 - 0.20, 0.55 + 0.02), whole shares
    es = bot.entries[w.key]
    assert len(es) == 2 and es[1]["hedge"] is True and es[1]["side"] == "D"
    h = dict(j.ev)["lock_hedge"]
    cost = 4.65 + 0.0228 + 5 * 0.55 + 5 * 0.017
    assert h["worst"] == pytest.approx(5 - cost, abs=1e-3) and h["best"] == pytest.approx(5 - cost, abs=1e-3)
    assert "lock_entry" not in [k for k, _ in j.ev]                    # not counted as a new entry in the reports
    asyncio.run(tick())
    assert len(br.calls) == 1                                          # once per window
    bot._settle(w.key, "Down")
    assert dict(j.ev)["settle"]["pnl"] == pytest.approx(5 - cost, abs=1e-3)


def test_cheap_insurance_is_still_a_valid_order(tmp_path, monkeypatch):
    """reports/69: on 12 Nov 07:24 the insurance of a SOL Down entry (5 shares) found Up at 0.09, sent 5 shares capped at 0.11 =
    $0.55 and the exchange refused it ("invalid amount for a marketable BUY order ($0.55), min size: 1"); the window then lost
    -$4.68. A cheap insurance buys enough whole shares for $1.05."""
    import time as _t
    import pmbot.bot.lock as lockmod
    from pmbot.bot.state import Window

    class Br:
        mode = "paper"

        def __init__(self):
            self.calls = []

        async def buy(self, token, key, side, limit, shares):
            self.calls.append((token, side, limit, shares))
            assert shares * limit >= 1.0                               # what the exchange demands of a marketable BUY
            return 0.09, shares, 0.0
    br = Br()
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=br, tables=_Tab())
    now = int(_t.time() * 1000)
    w = bot.st.add_window(Window(asset="sol", start=now // 1000 - 258, slug="s", condition_id="c", token_up="u", token_down="d"))
    bot.entries[w.key] = [{"side": "D", "shares": 5.0538, "price": 0.9201, "cost": 4.65, "fee": 0.026, "t": now / 1000,
                           "limit": 0.93, "p": 0.954}]
    bot.st.feed_alive["binance"] = now
    bot.st.feed_alive["clob"]["sol"] = int(_t.time() * 1000) + 60_000
    bot.st.books["u"] = Book(bids={0.08: 10}, asks={0.09: 50.0})
    monkeypatch.setattr(lockmod, "decide_state", lambda c, *a, **kw: ("U", 0.7357, {"rel": 258.3, "side": "U", "p": 0.7357}))

    async def tick():
        await bot.decide_once(now)
        await asyncio.sleep(0.01)
    asyncio.run(tick())
    assert br.calls == [("u", "U", 0.11, 10)]                         # min(0.7357 - 0.20, 0.09 + 0.02) = 0.11; ceil(1.05 / 0.11) = 10
    es = bot.entries[w.key]
    assert es[1]["hedge"] and es[1]["shares"] == 10
    bot._settle(w.key, "Up")                                           # what happened: Up won
    assert bot.risk["realized_by_day"][bot.today(bot.end_of(w.key))] == pytest.approx(10 - 0.9 - 4.676, abs=1e-3)


def test_l3_15_live_is_its_paper_twin_with_live_limits():
    """reports/65: the 15-minute rule on real money is exactly its paper twin pmbot-lock-15-tier (the rule measured in the replay
    and on paper), with the live limits of the other live bots and its own books."""
    import dataclasses as dc
    paper = load_lock_config(os.path.join(ROOT, "config", "lock15_tier.toml"))
    live = load_lock_config(os.path.join(ROOT, "config", "lock15_live.toml"))
    l3 = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    l99 = load_lock_config(os.path.join(ROOT, "config", "lock_99_live.toml"))
    assert live.execution.mode == "live" and paper.execution.mode == "paper"
    assert live.lock == paper.lock, "the 15m live rule must stay identical to its paper twin pmbot-lock-15-tier"
    assert live.lock.duration == 900 and live.lock.hedge_p == 0.0
    assert live.lock.shares <= 5 and live.risk.max_usd_per_entry <= 5.0 and live.risk.max_open_usd <= 5.0
    assert live.risk.daily_loss_limit <= 5.0 and live.risk.resume_next_day is False
    assert dc.replace(paper.risk, state_file=live.risk.state_file, resume_next_day=False) == live.risk
    assert len({live.risk.state_file, paper.risk.state_file, l3.risk.state_file, l99.risk.state_file}) == 4
    assert len({live.journal_dir, paper.journal_dir, l3.journal_dir, l99.journal_dir}) == 4
    from pmbot import notify, ops
    assert "pmbot-lock-15-live" in ops.BOT_UNITS and notify.TWINS["pmbot-lock-15-tier"] == "pmbot-lock-15-live"
    for f in ("autoupdate.sh", "install_units.sh"):
        assert " pmbot-lock-15-live " in open(os.path.join(ROOT, "deploy", f)).read(), f


def test_ten_shares_on_the_upper_tiers_one_entry_at_a_time(tmp_path, monkeypatch):
    """reports/67 (owner 11 Nov: "давай 10"): live 5m buys 10 shares when the model is >= 0.97 and 5 below, still one window at a
    time (max_open_entries = 1 even though $10 would fit two 5-share entries). Off in the 15m bot and the sniper."""
    import time as _t
    import pmbot.bot.lock as lockmod
    from pmbot.bot.state import Window
    live = load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"))
    assert (live.lock.shares, live.lock.big_shares, live.lock.big_shares_p) == (2.0, 10.0, 0.97)   # 2 below 0.97: reports/70
    assert live.risk.max_usd_per_entry >= 10 * live.lock.a_max and live.risk.max_open_entries == 1
    for name in ("lock15_live.toml", "lock15_tier.toml", "lock_99_live.toml", "lock_99.toml", "lock_tier_wide.toml"):
        assert load_lock_config(os.path.join(ROOT, "config", name)).lock.big_shares == 0, name
    for bad in ({"lock": {"big_shares": 1.0}}, {"lock": {"big_shares": 10.5}}, {"lock": {"big_shares_p": 1.0}},
                {"risk": {"max_usd_per_entry": 9.0}}, {"risk": {"max_open_entries": -1}}):
        with pytest.raises(ValueError):
            load_lock_config(os.path.join(ROOT, "config", "lock_live.toml"), overrides=bad)

    class Br:
        mode = "paper"

        def __init__(self):
            self.calls = []

        async def buy(self, token, key, side, limit, shares):
            self.calls.append((key, shares, limit))
            return limit, shares, 0.0

    for p_model, ask, want in ((0.985, 0.98, 10), (0.975, 0.95, 10), (0.96, 0.93, 2)):
        br = Br()
        cfg = load_lock_config(os.path.join(ROOT, "config", "lock_tier.toml"),
                               overrides={"risk": {"state_file": str(tmp_path / f"s{want}{ask}.json"), "kill_file": ""}})
        bot = LockBot(cfg, _J(), broker=br, tables=_Tab())
        now = int(_t.time() * 1000)
        start = now // 1000 - 250
        for a in ("btc", "eth"):
            bot.st.add_window(Window(asset=a, start=start, slug=a, condition_id=a, token_up=a + "u", token_down=a + "d"))
        bot.st.feed_alive["binance"] = now

        def fake_decide(c, tables, asset, *args, p_model=p_model, ask=ask, **kw):
            lim = lockmod.tier_limit(c, p_model)
            return {"side": "U", "limit": lim, "ask": ask, "p": p_model}, {"rel": 250.0, "side": "U"}
        monkeypatch.setattr(lockmod, "decide", fake_decide)

        async def go():
            await bot.decide_once(now)
            await asyncio.sleep(0.01)
            await bot.decide_once(now + 50)
            await asyncio.sleep(0.01)
        asyncio.run(go())
        assert len(br.calls) == 1, br.calls                        # two coins qualify, one entry at a time
        assert br.calls[0][1] == want, (p_model, br.calls)
