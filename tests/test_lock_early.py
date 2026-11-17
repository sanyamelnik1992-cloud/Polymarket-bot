"""lock.zone = "early" (reports/80-81): the leader 1-5 minutes before the end, one-hour sigma, asks 0.985-0.99 only."""
import asyncio
import math
import os

import pytest

from pmbot.bot.lock import LockBot, PaperTaker, decide_early, load_lock_config
from pmbot.bot.state import State, Window
from pmbot.lockmodel import EARLY_AVG_VAR, HourSigma, early_z

ROOT = os.path.join(os.path.dirname(__file__), "..")
T = 1_794_000_000          # a window start (multiple of 900)
EARLY5 = os.path.join(ROOT, "config", "lock_early.toml")
EARLY15 = os.path.join(ROOT, "config", "lock15_early.toml")


class _J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append((kind, kw))


def test_hour_sigma_is_the_rms_of_the_last_hour_and_survives_a_restart():
    h = HourSigma(3600)
    h.on_print(1000, 100.0)
    assert h.sigma(min_n=1) is None
    v = 100.0
    for s in range(1001, 1001 + 1800):                   # +-1 bp every second
        v *= math.exp(1e-4 if s % 2 else -1e-4)
        h.on_print(s, v)
    assert h.sigma(min_n=1800) == pytest.approx(1e-4, rel=1e-6)
    assert h.sigma(min_n=3600) is None                   # not trusted before min_n seconds of returns
    # an hour of a calm market afterwards pushes the old returns out of the window
    for s in range(2801, 2801 + 4000):
        h.on_print(s, v)
    assert h.sigma(min_n=1) == pytest.approx(0.0, abs=1e-12)
    assert h.n <= 3600 + 60
    # missing seconds are zero returns (forward fill), at most 120 of them
    h2 = HourSigma(3600)
    h2.on_print(0, 100.0)
    h2.on_print(5, 100.0 * math.exp(2e-4))
    assert h2.n == 5 and h2.sigma(min_n=1) == pytest.approx(math.sqrt(4e-8 / 5))
    # state file round trip
    h3 = HourSigma(3600)
    h3.load(h.to_dict())
    assert h3.n == h.n and h3.last_sec == h.last_sec and h3.sigma(min_n=1) == pytest.approx(h.sigma(min_n=1), abs=1e-15)


def test_early_z_counts_the_wait_and_the_closing_average():
    assert EARLY_AVG_VAR == pytest.approx(sum(min(i, j) for i in range(60) for j in range(60)) / 3600, abs=0.01)
    # 30 bp ahead, sigma 1 bp / s, 100 s to the first closing print: 30 / sqrt(119.5)
    assert early_z(100.3, 100.0, 1e-4, 100.0) == pytest.approx(math.log(1.003) / (1e-4 * math.sqrt(119.5)))
    assert early_z(99.7, 100.0, 1e-4, 100.0) < -2.7
    assert early_z(100.3, 100.0, None, 100.0) == 0.0


def _inputs(lead_bp=30.0, sigma=1e-4, rel=200.0, Tc=T, ask_up=(0.99, 500.0), ask_dn=(0.02, 500.0)):
    K = 100.0
    now = int((Tc + rel) * 1000)
    S = K * math.exp(lead_bp * 1e-4)
    cl = (S, now - 1000, now - 400)                      # (value, print ts, received)
    return dict(T=Tc, K=K, now_ms_=now, cl_last=cl, sigma=sigma, bn_now=75000.0, bn_at_last=75000.0, bn_1s=75000.0,
                bn_3s=75000.0, ask_up=ask_up, ask_dn=ask_dn)


def test_configs_are_paper_and_early():
    for path, dur, lo, hi in ((EARLY5, 300, 180.0, 238.0), (EARLY15, 900, 0.0, 180.0)):
        cfg = load_lock_config(path)
        c = cfg.lock
        assert cfg.execution.mode == "paper" and c.zone == "early" and c.duration == dur
        assert (c.rel_lo, c.rel_hi) == (lo, hi)               # 5m: 120-62 s before the end; 15m: 300-120 s (reports/80)
        assert c.z_min == 3.0 and c.ask_lo == 0.985 and c.a_max == 0.99 and c.sigma_window_s == 3600
        assert not c.tiers and not c.hedge_p and not c.big_shares
    a, b = load_lock_config(EARLY5), load_lock_config(EARLY15)
    assert a.journal_dir != b.journal_dir and a.risk.state_file != b.risk.state_file


@pytest.mark.parametrize("bad", [
    {"rel_lo": 240.0, "rel_hi": 299.5},          # the early rule ends before the closing TWAP
    {"z_min": 1.0},
    {"ask_lo": 0.995},                           # above a_max
    {"tiers": [[0.99, 0.98]], "theta": 0.99},
    {"hedge_p": 0.65},
])
def test_early_validation(bad):
    with pytest.raises(ValueError):
        load_lock_config(EARLY5, overrides={"lock": bad})
    with pytest.raises(ValueError):
        load_lock_config(overrides={"lock": {"zone": "middle"}})


def test_decide_early_buys_the_leader_at_099():
    c = load_lock_config(EARLY5).lock
    sig, ctx = decide_early(c, **_inputs())
    assert sig and sig["side"] == "U" and sig["limit"] == 0.99 and sig["ask"] == 0.99, ctx
    assert ctx["tau0"] == pytest.approx(38.0) and ctx["z"] == pytest.approx(30 / math.sqrt(57.5), rel=1e-3)
    sig, ctx = decide_early(c, **_inputs(lead_bp=-30.0, ask_up=(0.02, 500.0), ask_dn=(0.988, 50.0)))
    assert sig and sig["side"] == "D" and sig["ask"] == 0.988, ctx


@pytest.mark.parametrize("change,why", [
    ({"lead_bp": 15.0}, "z"),                         # 15 / sqrt(57.5) = 2.0 < 3
    ({"ask_up": (0.98, 500.0)}, "ask_too_low"),       # a cheaper seller knows more (reports/80)
    ({"ask_up": (0.995, 500.0)}, "ask_too_high"),
    ({"ask_up": None}, "no_ask"),
    ({"rel": 239.0}, "time"),                         # the closing TWAP has started: the closing-minute bots decide there
    ({"rel": 170.0}, "time"),
    ({"sigma": None}, "no_sigma"),
    ({"bn_1s": 75000 * (1 + 2e-4)}, "binance_against"),
    ({"stale": True}, "cl_stale"),
    ({"agree": True}, "agree"),
])
def test_decide_early_filters(change, why):
    c = load_lock_config(EARLY5).lock
    kw = _inputs(**{k: v for k, v in change.items() if k in ("lead_bp", "ask_up", "rel", "sigma")})
    if "bn_1s" in change:
        kw["bn_1s"] = change["bn_1s"]
    if change.get("stale"):
        kw["cl_last"] = (kw["cl_last"][0], kw["cl_last"][1], kw["now_ms_"] - 5000)
    if change.get("agree"):          # Binance says +30 bp, the received prints only +10 bp: wait for Chainlink
        kw["cl_last"] = (100.0 * math.exp(10e-4), kw["cl_last"][1], kw["cl_last"][2])
        kw["bn_now"] = 75000.0 * math.exp(20e-4)
        kw["bn_1s"] = kw["bn_3s"] = kw["bn_now"]
    sig, ctx = decide_early(c, **kw)
    assert sig is None and ctx["why"] == why, ctx


def test_15m_early_zone_is_measured_from_the_anchor():
    c = load_lock_config(EARLY15).lock
    Tc = T + 600                                       # 15m anchor = end - 300
    sig, ctx = decide_early(c, **_inputs(rel=100.0, Tc=Tc, lead_bp=60.0))    # 200 s before the end
    assert sig and ctx["tau0"] == pytest.approx(138.0), ctx
    sig, ctx = decide_early(c, **_inputs(rel=200.0, Tc=Tc, lead_bp=60.0))    # 100 s before the end: too late for this bot
    assert sig is None and ctx["why"] == "time"


def test_early_bot_subscribes_books_early_and_enters_once(tmp_path):
    import time as _t
    cfg = load_lock_config(EARLY5, overrides={"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": ""}})
    j = _J()
    st = State(["btc"])
    bot = LockBot(cfg, j, broker=PaperTaker(st, exec_ms=0))
    bot.st = st
    now_s = int(_t.time())
    start = now_s - 200                                # window second 200: inside the early zone (180-238)
    w = Window(asset="btc", start=start, slug="s", condition_id="c", token_up="tu", token_down="td")
    st.add_window(w)
    key = w.key
    K = 100.0
    for s in range(start - 70, start):
        st.prices["btc"].cl_by_sec[s] = K
    bot.strikes[key] = K
    v = K * math.exp(40e-4)
    px = st.prices["btc"]
    px.cl_by_sec[now_s - 1] = v
    px.cl_last = (v, (now_s - 1) * 1000, now_s * 1000 - 300)
    for k in range(40):
        px.bn_hist.append((now_s * 1000 - 4000 + k * 100, 75000.0))
    st.feed_alive["binance"] = now_s * 1000
    h = bot.hsig["btc"]
    h.last_sec, h.last_v = now_s - 1, v
    h.buckets = {(now_s - 1) // 60: [3600 * 1e-8, 3600]}     # sigma 1 bp / s over the last hour
    from pmbot.bot.state import Book
    st.books["tu"] = Book()
    st.books["tu"].asks = {0.99: 300.0}
    st.books["td"] = Book()
    st.books["td"].asks = {0.02: 300.0}
    st.feed_alive["clob"]["btc"] = now_s * 1000

    async def go():
        await bot.decide_once(now_s * 1000)
        await asyncio.sleep(0.02)
        await bot.decide_once(now_s * 1000 + 50)
        await asyncio.sleep(0.02)
    asyncio.run(go())
    es = bot.entries.get(key)
    assert es and len(es) == 1 and es[0]["side"] == "U" and es[0]["price"] == 0.99 and es[0]["shares"] == cfg.lock.shares
    assert any(k == "lock_entry" for k, _ in j.ev)
    # the hour of returns is written to the state file and read back
    bot._save_state()
    bot2 = LockBot(cfg, _J(), broker=PaperTaker(State(["btc"])))
    assert bot2.hsig["btc"].n == 3600 and bot2.hsig["btc"].sigma(cfg.lock.min_sigma_s) == pytest.approx(1e-4)
