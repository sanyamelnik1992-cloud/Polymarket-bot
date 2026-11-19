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
    for path, dur, lo in ((FAIR5, 300, 10.0), (FAIR15, 900, -540.0)):
        cfg = load_lock_config(path)
        c = cfg.lock
        assert cfg.execution.mode == "paper" and c.zone == "fair" and c.duration == dur and (c.rel_lo, c.rel_hi) == (lo, 238.0)
        assert c.fair_edge == 0.15 and c.max_entries_per_window == 1 and c.shares == 10
        unit = "pmbot-lock-" + ("fair" if dur == 300 else "15-fair")
        assert unit in ops.BOT_UNITS and unit in notify.BOTS and unit not in notify.TWINS
        text = open(os.path.join(ROOT, "deploy", unit + ".service")).read()
        assert f"--config config/{os.path.basename(path)}" in text and "--live" not in text and "CPUWeight=20" in text
        for f in ("autoupdate.sh", "install_units.sh"):
            assert f" {unit} " in open(os.path.join(ROOT, "deploy", f)).read(), f
    for bad in ({"execution": {"mode": "live"}, "risk": {"resume_next_day": False}},           # paper only (reports/83)
                {"lock": {"rel_lo": 240.0, "rel_hi": 299.0}}, {"lock": {"fair_edge": 0.0}}, {"lock": {"fair_pmin": 0.96}},
                {"lock": {"max_entries_per_window": 4}}, {"lock": {"hedge_p": 0.65}}):
        with pytest.raises(ValueError):
            load_lock_config(FAIR5, overrides=bad)
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
    assert br.calls[0][1] == "U" and br.calls[0][3] == 10 and br.calls[0][2] >= 0.62
    assert any(k == "bn_strike" for k, _ in bot.j.ev)


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
