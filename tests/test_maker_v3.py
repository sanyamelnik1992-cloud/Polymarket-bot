"""Мейкер v3 (reports/96): bids at least k_time x the fair value's 1 s move under their reference (x the 10-min / 1-h sigma ratio), the
paper maker v2 back from PAUSED next to it, and ONE hourly Telegram message for the paper makers."""
import dataclasses as dc
import importlib.util
import json
import math
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot import notify, ops  # noqa: E402
from pmbot.bot.fairmaker import FairMaker, MakerCfg, bid_target, keep_bid, load_maker_config, load_maker_live_config  # noqa: E402
from pmbot.lockmodel import BlockSigma  # noqa: E402
from test_maker_live import J, LIVE, V2, FakeBroker, live_bot, run  # noqa: E402
from test_notify import balanced  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
V3 = os.path.join(ROOT, "config", "maker_v3.toml")


def phi(z):
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


# ---------------------------------------------------------------- the rule
def test_v3_is_v2_plus_the_time_scaled_spread_only():
    c2, m2 = load_maker_config(V2)
    c3, m3 = load_maker_config(V3)
    assert c3.lock == c2.lock and c3.risk.max_open_usd == c2.risk.max_open_usd and c3.execution.mode == "paper"
    d2, d3 = dc.asdict(m2), dc.asdict(m3)
    assert {k for k in d2 if d2[k] != d3[k]} == {"k_time", "vol_ratio"}
    assert (m3.k_time, m3.vol_ratio, m2.k_time, m2.vol_ratio) == (1.5, True, 0.0, False)
    assert c3.journal_dir == "data/journal-maker-v3" and c3.risk.state_file == "data/state/maker_v3.json"
    assert load_maker_live_config(LIVE, live_flag=True)[1].k_time == 0.0          # the live rule is not changed (owner: watch paper)
    for bad in (-0.1, 5.5):
        with pytest.raises(ValueError):
            load_maker_config(V3, overrides={"maker": {"k_time": bad}})


def test_the_floor_widens_a_bid_and_keeps_the_skew():
    m = MakerCfg(half_spread=0.015, dog_spread=0.10, shares=5, max_side_shares=10, max_unpaired=5, skew_c=0.01, p_hi=0.80)
    assert bid_target(m, 0.60, 0, 0, None, True, 0.0) == (0.58, "")                # v2: 1.5 c under
    assert bid_target(m, 0.60, 0, 0, None, True, 0.05) == (0.55, "")               # v3: 5 c under
    assert bid_target(m, 0.30, 0, 0, None, False, 0.05) == (0.20, "")              # the underdog keeps its wider 10 c
    assert bid_target(m, 0.60, 5, 5, None, True, 0.05)[1] == "unpaired"
    assert bid_target(m, 0.60, 0, -5, None, True, 0.05) == (0.56, "")              # the short side: 1 c skew closer, 4 c under
    assert keep_bid(m, 0.55, 0.55, 0.60, 0, True, 0.05) and not keep_bid(m, 0.58, 0.55, 0.60, 0, True, 0.05)


def test_spread_floor_is_k_times_the_fair_values_one_second_move(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    bot.mm.k_time = 1.5
    for z, tau0 in ((0.0, 138.0), (0.8, 40.0), (-1.5, 10.0)):
        assert bot.spread_floor("btc", z, tau0) == pytest.approx(1.5 * phi(z) / math.sqrt(tau0 + 19.5))
    assert bot.spread_floor("btc", 0.0, 138.0) == pytest.approx(0.0477, abs=5e-4)    # ~4.8 c at window second 100
    assert bot.spread_floor("btc", 0.0, 25.0) == pytest.approx(0.0897, abs=5e-4)     # ~9 c at second 213
    assert bot.spread_floor("btc", None, None) == 0.0
    bot.mm.k_time = 0.0
    assert bot.spread_floor("btc", 0.0, 138.0) == 0.0                                 # v2: off


def test_vol_ratio_widens_only_in_a_turbulent_spell():
    b = BlockSigma()
    for i in range(300):
        b.r2.append(1e-8)                   # 50 calm minutes
    for i in range(60):
        b.r2.append(4e-8)                   # 10 minutes with twice the moves
    assert b.sigma_recent(60) / b.sigma() == pytest.approx(math.sqrt(4e-8 / ((300 * 1e-8 + 60 * 4e-8) / 360)), rel=1e-9)
    assert b.sigma_recent(400) is None and b.sigma_recent(0) is None


def test_v3_quotes_wider_than_v2_on_the_same_market(tmp_path):
    quotes = {}
    for label, k, vr in (("v2", 0.0, False), ("v3", 1.5, True)):
        bot, j, br, w, now = live_bot(tmp_path / label)
        bot.mm.k_time, bot.mm.vol_ratio = k, vr
        run(bot.decide_once(now))
        quotes[label] = {q["side"]: q for q in j.of("mm_quote")}
    up2, up3 = quotes["v2"]["U"], quotes["v3"]["U"]
    assert up2["fl"] == 0.0 and up3["fl"] > 0.02
    assert up3["price"] <= round(up3["ref"] - up3["fl"], 4) + 1e-9 and up3["price"] < up2["price"]
    assert quotes["v3"]["D"]["price"] == quotes["v2"]["D"]["price"]                  # the underdog's 10 c is wider already


# ---------------------------------------------------------------- units
def test_paper_maker_v2_is_back_and_v3_starts_once():
    inst = open(os.path.join(ROOT, "deploy", "install_units.sh")).read()
    paused = re.search(r'(?m)^PAUSED="([^"]*)"', inst).group(1).split()
    resume = re.search(r'(?m)^RESUME_ONCE="([^"]*)"', inst).group(1).split()
    start_once = re.search(r'(?m)^START_ONCE="([^"]*)"', inst).group(1).split()
    assert "pmbot-maker-v2" not in paused and resume == ["pmbot-maker-v2"] and "pmbot-maker-v3" in start_once
    assert '.resumed-$u' in inst and 'rm -f "$REPO/data/state/.resumed-$u"' in inst          # once, and again after a later pause
    assert " pmbot-maker-v3 " in inst and " pmbot-maker-v3 " in open(os.path.join(ROOT, "deploy", "autoupdate.sh")).read()
    svc = open(os.path.join(ROOT, "deploy", "pmbot-maker-v3.service")).read()
    assert "scripts/run_maker.py --config config/maker_v3.toml" in svc and "--live" not in svc and "CPUWeight=20" in svc
    assert "pmbot-maker-v3" in ops.BOT_UNITS and "pmbot-maker-v3" in notify.BOTS and not ops.unit_is_live("pmbot-maker-v3")
    assert ops.bot_paths("pmbot-maker-v3") == ("data/journal-maker-v3", "data/state/maker_v3.json", "paper")


# ---------------------------------------------------------------- Telegram
def rep(t, **kw):
    r = {"t": t, "type": "mm_report", "mode": "paper", "t0": t - 3600, "t1": t, "minutes": 60, "state": "ok", "closed": 4, "won": 3,
         "lost": 1, "pnl": 1.25, "quotes": 300, "fills": 9, "mo_c": {"5": 0.4, "30": -0.6}, "day": 2.5}
    r.update(kw)
    return r


def paper_repo(tmp_path, monkeypatch, records, states=None):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "config").mkdir()
    for u, cfg, jd in (("pmbot-maker-v2", "maker_v2", "jm2"), ("pmbot-maker-v3", "maker_v3", "jm3")):
        (tmp_path / "deploy" / f"{u}.service").write_text(f"[Service]\nExecStart=x/python scripts/run_maker.py --config config/{cfg}.toml\n")
        (tmp_path / "config" / f"{cfg}.toml").write_text(f'journal_dir = "data/{jd}"\n[execution]\nmode = "paper"\n')
        if u in records:
            d = tmp_path / "data" / jd / "20261126-190000"
            d.mkdir(parents=True)
            (d / "events.jsonl").write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in records[u]) + "\n")   # the journal's format
    states = states or {}
    monkeypatch.setattr(ops, "REPO", str(tmp_path))
    monkeypatch.setattr(ops, "unit_state", lambda u: (states.get(u, "active"), time.time() - 3600))


def test_one_message_for_both_paper_makers(tmp_path, monkeypatch):
    now = time.time()
    paper_repo(tmp_path, monkeypatch, {"pmbot-maker-v2": [rep(now - 3700, pnl=-9.0), rep(now - 120)],
                                       "pmbot-maker-v3": [rep(now - 110, closed=0, fills=0, pnl=0.0, day=-0.4)]})
    msg = notify.format_maker_paper(now=now)
    assert balanced(msg) and msg.count("🏦") == 2 and "Мейкеры на бумаге" in msg and "без денег" in msg
    assert "Мейкер v2 · 5 мин</b>: окон с позицией 4 (+3/−1) <b>+1.25$</b> · исполнений 9 из 300 заявок · цена через 30 с −0.6¢" in msg
    assert "−9.00$" not in msg                                                        # the newest report only
    assert "Мейкер v3 · 5 мин</b>: окон с позицией 0 · исполнений 0 из 300 заявок · сегодня −0.40$" in msg
    assert notify.format_maker_paper(now=now + 3600) is None                           # nothing fresh: no message


def test_a_stopped_or_silent_paper_maker_is_left_out(tmp_path, monkeypatch):
    now = time.time()
    paper_repo(tmp_path, monkeypatch, {"pmbot-maker-v2": [rep(now - 60)], "pmbot-maker-v3": [rep(now - 60)]},
               states={"pmbot-maker-v3": "inactive"})
    msg = notify.format_maker_paper(now=now)
    assert "Мейкер v2" in msg and "Мейкер v3" not in msg
    assert "Мейкер v2" in notify.format_maker() and "Мейкеры на бумаге" in notify.format_maker()   # /maker shows them too


def test_the_control_bot_sends_the_paper_makers_once_an_hour(monkeypatch):
    spec = importlib.util.spec_from_file_location("tg", os.path.join(ROOT, "scripts", "tg_bot.py"))
    tg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tg)
    sent = []
    monkeypatch.setattr(tg, "send", sent.append)
    monkeypatch.setattr(tg.notify, "format_maker_paper", lambda now=None: f"MSG {int(now)}")
    w = tg.Watcher.__new__(tg.Watcher)
    h = 500_000
    w.mm_hour = h
    assert w.paper_makers(now=h * 3600 + 1000) is None                                 # the same hour as the start
    assert w.paper_makers(now=(h + 1) * 3600 + 60) is None                             # too early: the makers write at :00
    assert w.paper_makers(now=(h + 1) * 3600 + 130) == f"MSG {(h + 1) * 3600 + 130}"
    assert w.paper_makers(now=(h + 1) * 3600 + 900) is None                            # once an hour
    assert sent == [f"MSG {(h + 1) * 3600 + 130}"]
