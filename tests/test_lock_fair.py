"""lock.zone = "fair" (reports/83): the book lags Binance; buy the side whose ask is >= fair_edge below its Binance fair value."""
import asyncio
import math
import os

import pytest

from pmbot.bot.lock import LockBot, decide_fair, load_lock_config
from pmbot.lockmodel import BlockSigma

ROOT = os.path.join(os.path.dirname(__file__), "..")
FAIR5 = os.path.join(ROOT, "config", "lock_fair.toml")
FAIR15 = os.path.join(ROOT, "config", "lock15_fair.toml")
T = 1_795_000_000                     # an anchor (end - 300)


class _J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append((kind, kw))


class _Tab:
    def surv(self, *a):
        return 0.5


def test_block_sigma_uses_10_s_returns_and_forward_fills():
    o = BlockSigma(lookback=3600, block=10)
    v = 100.0
    for s in range(10_000, 10_000 + 1800):
        if s % 10 == 9:
            v *= math.exp(1e-4 * (1 if (s // 10) % 2 else -1))      # +-1 bp per 10 s block
        if s % 10 in (3, 4):
            continue                                                   # dropped seconds inside a block: forward-filled
        o.on_second(s, v)
    assert o.seconds() >= 1780 and o.sigma() * 1e4 == pytest.approx(1 / math.sqrt(10), rel=1e-6)
    assert o.sigma(min_s=3600) is None
    o.on_second(o.last_sec + 700, v)                                   # a long outage resets the history
    assert o.seconds() == 0 and o.sigma() is None


def test_fair_configs_are_paper_and_validated():
    from pmbot import notify, ops
    for path, dur, lo, shares in ((FAIR5, 300, 10.0, 5), (FAIR15, 900, -540.0, 10)):
        cfg = load_lock_config(path)
        c = cfg.lock
        assert cfg.execution.mode == "paper" and c.zone == "fair" and c.duration == dur and (c.rel_lo, c.rel_hi) == (lo, 238.0)
        assert c.fair_edge == 0.15 and c.shares == shares
        assert (c.max_entries_per_window, c.fair_gap_s) == ((3, 15.0) if dur == 300 else (1, 0.0))     # reports/83 section 12
        unit = "pmbot-lock-" + ("fair" if dur == 300 else "15-fair")
        assert unit in ops.BOT_UNITS and unit in notify.BOTS
        assert notify.TWINS.get(unit) == ("pmbot-lock-fair-live" if dur == 300 else None)     # the 5m bot is the live bot's twin
        text = open(os.path.join(ROOT, "deploy", unit + ".service")).read()
        assert f"--config config/{os.path.basename(path)}" in text and "--live" not in text and "CPUWeight=20" in text
        for f in ("autoupdate.sh", "install_units.sh"):
            assert f" {unit} " in open(os.path.join(ROOT, "deploy", f)).read(), f
    for bad in ({"lock": {"rel_lo": 240.0, "rel_hi": 299.0}}, {"lock": {"fair_edge": 0.0}}, {"lock": {"fair_pmin": 0.96}},
                {"lock": {"max_entries_per_window": 4}}, {"lock": {"hedge_p": 0.65}}):
        with pytest.raises(ValueError):
            load_lock_config(FAIR5, overrides=bad)
    with pytest.raises(ValueError):           # the 15m fair bot is paper only (reports/83 section 10)
        load_lock_config(FAIR15, overrides={"execution": {"mode": "live"}, "risk": {"resume_next_day": False}})
    for name in ("lock_live.toml", "lock_99_live.toml", "lock15_live.toml", "lock15_early_live.toml"):
        assert load_lock_config(os.path.join(ROOT, "config", name)).lock.zone != "fair", name


def test_decide_fair_buys_the_side_the_book_underprices():
    c = load_lock_config(FAIR5).lock
    now = (T + 150) * 1000                          # 150 s before the end: tau0 = 88 s
    K, sigma = 100.0, 1e-4                          # 1 bp per sqrt(s)
    S = K * math.exp(15e-4)                         # +15 bp: z = 15 / sqrt(107.5) = 1.447, fair Up = 0.926
    sig, ctx = decide_fair(c, T, K, now, S, sigma, (0.70, 40.0), (0.32, 40.0))
    assert sig["side"] == "U" and ctx["p_up"] == pytest.approx(0.926, abs=1e-3) and sig["edge"] == pytest.approx(0.226, abs=1e-3)
    assert sig["limit"] == pytest.approx(0.77) and sig["limit"] >= sig["ask"]          # never above fair - 0.15
    sig, ctx = decide_fair(c, T, K, now, S, sigma, (0.80, 40.0), (0.22, 40.0))          # Up 0.126 below fair: not enough
    assert sig is None and ctx["why"] == "edge"
    sig, ctx = decide_fair(c, T, K, now, K * math.exp(-15e-4), sigma, (0.10, 40.0), (0.70, 40.0))
    assert sig["side"] == "D"                                                          # the mirror case
    for kw, why in ((dict(K_bn=None), "no_strike"), (dict(sigma=None), "no_sigma"), (dict(now_ms_=(T + 5) * 1000), "time"),
                    (dict(now_ms_=(T + 239) * 1000), "time"), (dict(ask_up=None, ask_dn=None), "no_ask"),
                    (dict(ask_up=(0.03, 40.0), bn_now=K * math.exp(60e-4)), "price")):
        args = dict(T=T, K_bn=K, now_ms_=now, bn_now=S, sigma=sigma, ask_up=(0.70, 40.0), ask_dn=(0.32, 40.0))
        args.update(kw)
        sig, ctx = decide_fair(c, **args)
        assert sig is None and ctx["why"] == why, (kw, ctx)


def test_fair_bot_takes_a_stale_ask_once_per_window(tmp_path):
    import time as _t
    from pmbot.bot.state import Window

    class Br:
        mode = "paper"

        def __init__(self):
            self.calls = []

        async def buy(self, token, key, side, limit, shares):
            self.calls.append((key, side, limit, shares))
            return limit, shares, 0.0

    br = Br()
    cfg = load_lock_config(FAIR5, overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=br, tables=_Tab())
    now = int(_t.time() * 1000)
    start = now // 1000 - 150                                           # 150 s into the 5m window
    for a, lead_bp in (("btc", 20.0), ("eth", 1.0)):
        bot.st.add_window(Window(asset=a, start=start, slug=a, condition_id=a, token_up=a + "u", token_down=a + "d"))
        px = bot.st.prices[a]
        for s in range(start - 70, start):
            px.bn_sec_mid[s] = 100.0                                    # Binance strike: 100
        px.bn_hist.append((now - 200, 100.0 * math.exp(lead_bp * 1e-4)))
        bot.bsig[a].r2.extend([1e-8 * 10] * 360)                         # 1 bp per sqrt(s), an hour of history
        bot.bsig[a].last_sec = now // 1000 - 2
        bot.st.books[a + "u"] = type("B", (), {"asks": {0.62: 30.0}})()
        bot.st.books[a + "d"] = type("B", (), {"asks": {0.40: 30.0}})()
        bot.st.feed_alive["clob"][a] = now
    bot.st.feed_alive["binance"] = now

    async def go(t):
        await bot.decide_once(t)
        await asyncio.sleep(0.01)
    asyncio.run(go(now))
    asyncio.run(go(now + 1000))
    assert [c[0].split("-")[0] for c in br.calls] == ["btc"], br.calls      # eth: +1 bp, no edge; btc once per window
    assert br.calls[0][1] == "U" and br.calls[0][3] == 5 and br.calls[0][2] >= 0.62
    assert any(k == "bn_strike" for k, _ in bot.j.ev)


def test_fair_live_is_its_paper_twin_with_real_money_limits():
    """reports/83 section 10: the live Перехватчик trades exactly the rule of pmbot-lock-fair (paper since 19 Nov 17:40: 275 entries,
    +22.8 % of the money), 5 shares, one entry per coin of a window (4 at most, $20 open), a -$20 day stop that a human lifts.
    It is installed, not started: it trades only after the owner's word (/restart pmbot-lock-fair-live)."""
    from pmbot import notify, ops
    live_path = os.path.join(ROOT, "config", "lock_fair_live.toml")
    paper, live = load_lock_config(FAIR5), load_lock_config(live_path)
    assert live.execution.mode == "live" and paper.execution.mode == "paper"
    assert live.lock == paper.lock, "the live Перехватчик must stay identical to its paper twin pmbot-lock-fair"
    assert live.lock.fair_feed == "spot" and live.lock.duration == 300 and live.lock.shares == 5
    for cfg in (live, paper):
        assert (cfg.risk.max_usd_per_entry, cfg.risk.max_open_usd, cfg.risk.max_open_entries) == (5.0, 20.0, 4)
    assert live.risk.daily_loss_limit == 20.0 and live.risk.resume_next_day is False
    assert live.risk.state_file not in ("", paper.risk.state_file) and live.journal_dir != paper.journal_dir
    text = open(os.path.join(ROOT, "deploy", "pmbot-lock-fair-live.service")).read()
    assert "--config config/lock_fair_live.toml --live" in text and "CPUWeight=500" in text
    assert "pmbot-lock-fair-live" in ops.BOT_UNITS and ops.unit_is_live("pmbot-lock-fair-live")
    assert notify.TWINS["pmbot-lock-fair"] == "pmbot-lock-fair-live" and "pmbot-lock-fair-live" in notify.BOTS
    for f in ("autoupdate.sh", "install_units.sh"):
        assert " pmbot-lock-fair-live " in open(os.path.join(ROOT, "deploy", f)).read(), f
    # real money only with the tested rule: no futures signal, no looser edge, one entry per window, a cap on the size and on
    # the open windows, 5m only
    for bad in ({"lock": {"fair_feed": "futures"}}, {"lock": {"fair_edge": 0.10}}, {"lock": {"max_entries_per_window": 4}},
                {"lock": {"fair_gap_s": 2.0}}, {"lock": {"shares": 20}}, {"risk": {"max_open_entries": 0}},
                {"lock": {"duration": 900, "rel_lo": -540.0}}, {"risk": {"resume_next_day": True}}):
        with pytest.raises(ValueError):
            load_lock_config(live_path, overrides=bad)


def test_min_order_shares_lifts_a_cheap_entry_to_one_dollar(tmp_path):
    """The exchange refuses a marketable BUY under $1 (reports/69): at 5 shares any limit below 0.21 would be refused, so a cheap
    fair entry buys ceil(1.05 / limit) whole shares (paper and live alike)."""
    import time as _t
    from pmbot.bot.lock import MIN_BUY_USD, min_order_shares
    from pmbot.bot.state import Window
    assert min_order_shares(5, 0.30, 5.0) == (5, False)
    assert min_order_shares(5, 0.21, 5.0) == (5, False)                 # $1.05 exactly
    assert min_order_shares(5, 0.18, 5.0) == (6, True)                  # $1.08
    assert min_order_shares(5, 0.05, 5.0) == (21, True)                 # $1.05
    assert min_order_shares(5, 0.05, 1.0) == (5, False)                 # never above the per-entry limit
    assert min_order_shares(10, 0.12, 10.0) == (10, False)              # $1.20 is enough

    class Br:
        mode = "paper"

        def __init__(self):
            self.calls = []

        async def buy(self, token, key, side, limit, shares):
            self.calls.append((key, side, limit, shares))
            return limit, shares, 0.0

    br = Br()
    cfg = load_lock_config(FAIR5, overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=br, tables=_Tab())
    now = int(_t.time() * 1000)
    start = now // 1000 - 150
    bot.st.add_window(Window(asset="btc", start=start, slug="btc", condition_id="btc", token_up="bu", token_down="bd"))
    px = bot.st.prices["btc"]
    for s in range(start - 70, start):
        px.bn_sec_mid[s] = 100.0
    px.bn_hist.append((now - 200, 100.0 * math.exp(4.5e-4)))           # +4.5 bp: Up ~0.67, Down ~0.33
    bot.bsig["btc"].r2.extend([1e-8 * 10] * 360)
    bot.bsig["btc"].last_sec = now // 1000 - 2
    bot.st.books["bu"] = type("B", (), {"asks": {0.62: 30.0}})()        # Up: no edge
    bot.st.books["bd"] = type("B", (), {"asks": {0.08: 30.0}})()        # Down sells at 0.08, ~25 c below its fair value
    bot.st.feed_alive["clob"]["btc"] = now
    bot.st.feed_alive["binance"] = now

    async def go():
        await bot.decide_once(now)
        await asyncio.sleep(0.01)
    asyncio.run(go())
    (key, side, limit, shares), = br.calls
    assert side == "D" and limit < 0.21 and shares == math.ceil(MIN_BUY_USD / limit - 1e-9) and shares * limit >= 1.05
    assert any(k == "lock_entry" and kw.get("min_order") for k, kw in bot.j.ev)


def test_fair_variants_instant_reaction_and_futures_ab():
    """reports/83 section 8 (owner 19 Nov: "улучши его немного"): every fair bot decides at once on a 0.3 bp Binance move;
    pmbot-lock-fair-fut is pmbot-lock-fair with the signal from Binance USD-M perpetuals (an A/B, paper only)."""
    import dataclasses
    from pmbot import notify, ops
    spot = load_lock_config(FAIR5)
    fut = load_lock_config(os.path.join(ROOT, "config", "lock_fair_fut.toml"))
    assert [k for k in dataclasses.asdict(spot.lock) if getattr(spot.lock, k) != getattr(fut.lock, k)] == ["fair_feed"]
    assert spot.lock.fair_feed == "spot" and fut.lock.fair_feed == "futures" and fut.execution.mode == "paper"
    assert spot.lock.fair_react_bp == fut.lock.fair_react_bp == load_lock_config(FAIR15).lock.fair_react_bp == 0.3
    assert fut.journal_dir != spot.journal_dir and fut.risk.state_file != spot.risk.state_file
    assert spot.risk == dataclasses.replace(fut.risk, state_file=spot.risk.state_file)
    assert "pmbot-lock-fair-fut" in ops.BOT_UNITS and "pmbot-lock-fair-fut" in notify.BOTS
    text = open(os.path.join(ROOT, "deploy", "pmbot-lock-fair-fut.service")).read()
    assert "--config config/lock_fair_fut.toml" in text and "--live" not in text and "CPUWeight=20" in text
    for bad in ({"lock": {"fair_feed": "coinbase"}}, {"lock": {"fair_react_bp": -1.0}}):
        with pytest.raises(ValueError):
            load_lock_config(FAIR5, overrides=bad)


def test_fair_bot_wakes_on_a_binance_move_and_reads_the_futures_mids(tmp_path):
    cfg = load_lock_config(os.path.join(ROOT, "config", "lock_fair_fut.toml"),
                           overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=type("B", (), {"mode": "paper"})(), tables=_Tab())
    assert bot.fut and bot.st.on_tick == bot._on_tick and bot.sig_px("btc") is bot.st.fut_prices["btc"]
    px = bot.st.fut_prices["btc"]
    px.bn_hist.append((1000, 100.0))
    bot._react_mid["btc"] = 100.0
    bot._on_tick("btc")
    assert not bot._wake.is_set()                      # no move since the last decision
    px.bn_hist.append((1100, 100.0 * math.exp(0.2e-4)))
    bot._on_tick("btc")
    assert not bot._wake.is_set()                      # 0.2 bp < 0.3 bp
    px.bn_hist.append((1200, 100.0 * math.exp(0.5e-4)))
    bot._on_tick("btc")
    assert bot._wake.is_set()                          # 0.5 bp: decide now
    spot = load_lock_config(FAIR5, overrides={"risk": {"state_file": str(tmp_path / "t.json"), "kill_file": ""},
                                              "lock": {"fair_react_bp": 0.0}})
    b2 = LockBot(spot, _J(), broker=type("B", (), {"mode": "paper"})(), tables=_Tab())
    assert b2._wake is None and b2.st.on_tick is None and b2.sig_px("btc") is b2.st.prices["btc"]


def test_fair_sigma_and_strike_seconds_survive_a_restart(tmp_path, monkeypatch):
    """Each restart (every update of main restarts the bots) cost the fair bots 30 min without entries while the Binance
    sigma refilled (21 Nov: restart 21:13, first entry in the 21:40 window). The hour of 10 s returns and the last 130 s of
    Binance mids are saved in the state file (every 30 s and at the stop) and restored when they are < 5 min old."""
    import json as _json
    import time as _t
    o = BlockSigma(lookback=3600, block=10)
    v = 100.0
    for s in range(20_000, 20_000 + 3600):
        if s % 10 == 9:
            v *= math.exp(1e-4 * (1 if (s // 10) % 2 else -1))
        o.on_second(s, v)
    o2 = BlockSigma(lookback=3600, block=10)
    assert o2.load(_json.loads(_json.dumps(o.to_dict()))) and o2.sigma(min_s=1800) == pytest.approx(o.sigma(min_s=1800))
    assert (o2.last_sec, o2.last_v, o2.block_v) == (o.last_sec, o.last_v, o.block_v)
    assert not BlockSigma(lookback=3600, block=5).load(o.to_dict())          # another block length: not restored

    state = str(tmp_path / "s.json")
    cfg = load_lock_config(FAIR5, overrides={"risk": {"state_file": state, "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=type("B", (), {"mode": "paper"})(), tables=_Tab())
    now = int(_t.time())
    T = now - 30                                                         # a window that started 30 s ago
    for a in cfg.lock.assets:
        bot.bsig[a].load(o.to_dict())
        bot.bsig[a].last_sec = now - 2
        for s in range(now - 130, now):
            bot.st.prices[a].bn_sec_mid[s] = 100.0 + (s % 7) * 0.01
    K = bot.bn_strike("btc-x", "btc", T)
    assert K is not None
    bot._save_state()
    fresh = LockBot(cfg, _J(), broker=type("B", (), {"mode": "paper"})(), tables=_Tab())
    for a in cfg.lock.assets:
        assert fresh.bsig[a].sigma(cfg.lock.fair_min_sigma_s) == pytest.approx(o.sigma())       # no 30 min wait
    assert fresh.bn_strike("btc-x", "btc", T) == pytest.approx(K)          # the strike of the running window too
    d = _json.load(open(state))
    for a in d["bsigma"]:
        d["bsigma"][a]["saved"] -= 400                                       # too old: the market moved on
    _json.dump(d, open(state, "w"))
    old = LockBot(cfg, _J(), broker=type("B", (), {"mode": "paper"})(), tables=_Tab())
    assert old.bsig["btc"].sigma() is None and not old.st.prices["btc"].bn_sec_mid
    fut = load_lock_config(os.path.join(ROOT, "config", "lock_fair_fut.toml"),
                           overrides={"risk": {"state_file": state, "kill_file": ""}})
    bot._save_state()
    other = LockBot(fut, _J(), broker=type("B", (), {"mode": "paper"})(), tables=_Tab())
    assert other.bsig["btc"].sigma() is None                                  # spot history never feeds the futures signal


def test_fair_takes_up_to_three_entries_a_window_15_s_apart(tmp_path, monkeypatch):
    """reports/83 section 12: on the public trades of 18-21 Nov the 2nd and 3rd qualifying moments of a window, 15 s or more after
    the previous entry, earned like the first (+34 % / +43 % of the money, every day positive), so the 5m bots (live and its twin)
    take up to 3 entries a window 15 s apart. The open money stays capped by max_open_usd ($20 live)."""
    import time as _t
    from pmbot.bot.state import Window

    class Br:
        mode = "paper"

        def __init__(self):
            self.calls = []

        async def buy(self, token, key, side, limit, shares):
            self.calls.append((key, side, limit, shares))
            return limit, shares, 0.0

    import pmbot.bot.lock as lockmod
    clock = {"t": int(_t.time() * 1000)}
    monkeypatch.setattr(lockmod, "now_ms", lambda: clock["t"])           # one clock for the decision and the fill
    br = Br()
    cfg = load_lock_config(FAIR5, overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    bot = LockBot(cfg, _J(), broker=br, tables=_Tab())
    now = clock["t"]
    start = now // 1000 - 100
    bot.st.add_window(Window(asset="btc", start=start, slug="btc", condition_id="btc", token_up="bu", token_down="bd"))
    px = bot.st.prices["btc"]
    for s in range(start - 70, start):
        px.bn_sec_mid[s] = 100.0
    bot.bsig["btc"].r2.extend([1e-8 * 10] * 360)
    bot.bsig["btc"].last_sec = now // 1000 - 2
    bot.st.books["bu"] = type("B", (), {"asks": {0.62: 30.0}})()
    bot.st.books["bd"] = type("B", (), {"asks": {0.40: 30.0}})()

    got = []

    async def go(t):
        clock["t"] = t
        px.bn_hist.append((t - 200, 100.0 * math.exp(20e-4)))        # Binance +20 bp: Up is worth ~0.97, the book sells it at 0.62
        bot.st.feed_alive["binance"] = t
        bot.st.feed_alive["clob"]["btc"] = t
        n = len(br.calls)
        await bot.decide_once(t)
        await asyncio.sleep(0.01)
        if len(br.calls) > n:
            got.append(t - now)
    for dt in (0, 1000, 14000, 16000, 17000, 30000, 31500, 47000, 70000):
        asyncio.run(go(now + dt))
    assert got == [0, 16000, 31500]                                    # 15 s apart, at most 3 entries a window
    assert all(c[0] == "btc-%d" % start for c in br.calls) and len(bot.entries["btc-%d" % start]) == 3
