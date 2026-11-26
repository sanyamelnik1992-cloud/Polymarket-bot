"""Paper market maker (pmbot/bot/fairmaker.py, reports/89): bids on both sides around the Binance fair value."""
import asyncio
import math
import os
import time

import pytest

from pmbot.bot.fairmaker import FairMaker, MakerCfg, bid_target, load_maker_config
from pmbot.bot.state import Book, Window

ROOT = os.path.join(os.path.dirname(__file__), "..")
CFG = os.path.join(ROOT, "config", "maker_fair.toml")


class _J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append((kind, kw))

    def kinds(self, kind):
        return [kw for k, kw in self.ev if k == kind]


def test_config_is_paper_only_and_validated():
    cfg, mm = load_maker_config(CFG)
    assert cfg.execution.mode == "paper" and cfg.lock.zone == "fair" and cfg.lock.assets == ["btc"]
    assert (cfg.lock.rel_lo, cfg.lock.rel_hi) == (20.0, 230.0) and mm.half_spread == 0.01 and mm.shares == 5
    for bad in ({"execution": {"mode": "live"}, "risk": {"resume_next_day": False}}, {"maker": {"half_spread": 0.0}},
                {"maker": {"shares": 3}}, {"maker": {"max_unpaired": 40.0}}, {"maker": {"p_lo": 0.95}},
                {"maker": {"markout_s": [0]}}, {"lock": {"rel_hi": 260.0}}, {"lock": {"zone": "close", "rel_lo": 240.0,
                                                                                       "rel_hi": 299.0}},
                {"maker": {"typo": 1}}, {"risk": {"max_open_usd": 5.0, "max_usd_per_entry": 5.0}}):
        with pytest.raises(ValueError):
            load_maker_config(CFG, overrides=bad)


def test_unit_is_wired_as_a_paper_bot():
    from pmbot import notify, ops
    text = open(os.path.join(ROOT, "deploy", "pmbot-maker.service")).read()
    assert "scripts/run_maker.py --config config/maker_fair.toml" in text and "--live" not in text and "CPUWeight=20" in text
    assert "pmbot-maker" in ops.BOT_UNITS and "pmbot-maker" in notify.BOTS and not ops.unit_is_live("pmbot-maker")
    assert ops.bot_paths("pmbot-maker") == ("data/journal-maker", "data/state/maker.json", "paper")
    for f in ("autoupdate.sh", "install_units.sh"):
        assert " pmbot-maker " in open(os.path.join(ROOT, "deploy", f)).read(), f
    inst = open(os.path.join(ROOT, "deploy", "install_units.sh")).read()
    # 26 Nov (reports/94): retired to make room for the live maker, whose rule is the cautious twin's ("min")
    assert "pmbot-maker" in inst.split("RETIRED=")[1].splitlines()[0].replace('"', " ").split()
    assert "pmbot-maker" not in inst.split("START_ONCE=")[1].splitlines()[0].replace('"', " ").split()


def test_the_cautious_twin_differs_only_in_the_reference():
    from dataclasses import asdict
    from pmbot import notify, ops
    a, am = load_maker_config(CFG)
    b, bm = load_maker_config(os.path.join(ROOT, "config", "maker_safe.toml"))
    assert (am.ref, bm.ref) == ("fair", "min")
    assert {k: v for k, v in asdict(am).items() if k != "ref"} == {k: v for k, v in asdict(bm).items() if k != "ref"}
    assert a.lock == b.lock and a.execution == b.execution
    assert asdict(a.risk) | {"state_file": ""} == asdict(b.risk) | {"state_file": ""} and a.risk.state_file != b.risk.state_file
    assert a.journal_dir != b.journal_dir
    text = open(os.path.join(ROOT, "deploy", "pmbot-maker-safe.service")).read()
    assert "scripts/run_maker.py --config config/maker_safe.toml" in text and "--live" not in text and "CPUWeight=20" in text
    assert "pmbot-maker-safe" in ops.BOT_UNITS and "pmbot-maker-safe" in notify.BOTS
    for f in ("autoupdate.sh", "install_units.sh"):
        assert " pmbot-maker-safe " in open(os.path.join(ROOT, "deploy", f)).read() or \
            " pmbot-maker-safe\"" in open(os.path.join(ROOT, "deploy", f)).read(), f
    inst = open(os.path.join(ROOT, "deploy", "install_units.sh")).read()
    # 26 Nov (reports/95): retired for the v2 paper twin of the live test (two paper makers do not fit the 2 vCPU)
    assert "pmbot-maker-safe" in inst.split("RETIRED=")[1].splitlines()[0].replace('"', " ").split()
    assert "pmbot-maker-safe" not in inst.split("START_ONCE=")[1].splitlines()[0].replace('"', " ").split()


def test_bid_target():
    mm = MakerCfg(half_spread=0.01, skew_c=0.01, shares=5, max_unpaired=10, max_side_shares=25)
    assert bid_target(mm, 0.604, 0, 0, None) == (0.59, "")              # 0.594 -> the 0.01 grid
    assert bid_target(mm, 0.604, 5, 5, None) == (0.58, "")              # 5 shares long: 1 c lower
    assert bid_target(mm, 0.604, 10, 10, None) == (None, "unpaired")
    assert bid_target(mm, 0.604, 25, 0, None) == (None, "side_full")
    assert bid_target(mm, 0.95, 0, 0, None) == (None, "price")
    assert bid_target(mm, 0.604, 0, 0, 0.58) == (0.57, "")              # post-only: one cent under the ask
    assert bid_target(mm, 0.604, 0, -10, None) == (0.60, "")            # 10 short: bid at the reference (completes the pair)
    assert bid_target(mm, 0.604, 0, -2, None) == (0.59, "")             # 2 short: 0.4 c higher, 0.598 -> 0.59


def test_ref_min_caps_the_bid_at_the_book_mid(tmp_path):
    bot, j, w, now = _bot(tmp_path, cfg_over={"maker": {"ref": "min"}})
    bot.st.books["U"] = Book(bids={0.70: 100.0}, asks={0.72: 50.0})       # the book values Up at 0.71, the model at ~0.79
    run(bot.decide_once(now))
    q = {x["side"]: x for x in j.kinds("mm_quote")}
    assert q["U"]["ref"] == pytest.approx(0.71) and q["U"]["price"] == 0.70 and q["U"]["p"] > 0.75
    bot2, j2, w2, now2 = _bot(tmp_path)                                    # ref = "fair": capped only by the ask
    bot2.st.books["U"] = Book(bids={0.70: 100.0}, asks={0.72: 50.0})
    run(bot2.decide_once(now2))
    assert {x["side"]: x for x in j2.kinds("mm_quote")}["U"]["price"] == 0.71


def test_keep_bid_hysteresis():
    from pmbot.bot.fairmaker import keep_bid
    mm = MakerCfg(half_spread=0.01, requote_c=0.01)
    assert keep_bid(mm, 0.55, 0.55, 0.567, 0)                # same price
    assert not keep_bid(mm, 0.55, 0.54, 0.555, 0)            # fair fell: lowered at once
    assert keep_bid(mm, 0.55, 0.56, 0.5701, 0)               # 0.5601 raw: just past the cent, stays
    assert not keep_bid(mm, 0.55, 0.56, 0.5735, 0)           # 0.5635 raw: raised


def _bot(tmp_path, lead_bp=10.0, rel=100, cfg_over=None):
    over = {"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": str(tmp_path / "STOP")}}
    for k, v in (cfg_over or {}).items():
        over[k] = {**over.get(k, {}), **v}
    cfg, mm = load_maker_config(CFG, overrides=over)
    j = _J()
    bot = FairMaker(cfg, mm, j)
    now = int(time.time() * 1000)
    start = now // 1000 - rel
    w = bot.st.add_window(Window(asset="btc", start=start, slug="s", condition_id="c", token_up="U", token_down="D"))
    px = bot.st.prices["btc"]
    for s in range(start - 70, start):
        px.bn_sec_mid[s] = 100.0                                        # Binance strike 100
    px.bn_hist.append((now - 5000, 100.0 * math.exp(lead_bp * 1e-4)))
    bot.bsig["btc"].r2.extend([1e-8 * 10] * 360)                         # 1 bp per sqrt(s), an hour of history
    bot.bsig["btc"].last_sec = now // 1000 - 2
    bot.st.books["U"] = Book(bids={0.55: 100.0}, asks={0.90: 50.0})      # wide: the post-only cap stays out of the way
    bot.st.books["D"] = Book(bids={0.09: 100.0}, asks={0.45: 50.0})
    bot.st.feed_alive["clob"]["btc"] = now
    bot.st.feed_alive["binance"] = now
    return bot, j, w, now


def _fair_up(lead_bp, rel):
    tau0 = 238 - rel
    return 0.5 * math.erfc(-(lead_bp / math.sqrt(tau0 + 19.5)) / math.sqrt(2))


def run(coro):
    return asyncio.run(coro)


def test_post_only_cap_under_the_ask(tmp_path):
    bot, j, w, now = _bot(tmp_path)
    bot.st.books["U"].asks = {0.70: 50.0}                                   # fair Up ~0.79: the bid stops one cent under the ask
    run(bot.decide_once(now))
    assert bot.mm_bids[(w.key, "U")]["price"] == 0.69


def test_quotes_both_sides_at_fair_minus_half_spread(tmp_path):
    bot, j, w, now = _bot(tmp_path)
    run(bot.decide_once(now))
    pu = _fair_up(10.0, now / 1000 - w.start)
    q = {x["side"]: x for x in j.kinds("mm_quote")}
    assert q["U"]["price"] == pytest.approx(math.floor((pu - 0.01) * 100 + 1e-9) / 100)
    assert q["D"]["price"] == pytest.approx(math.floor((1 - pu - 0.01) * 100 + 1e-9) / 100)
    assert q["U"]["price"] + q["D"]["price"] <= 0.98 + 1e-9                # a pair always costs less than $1
    assert {o.token for o in bot.broker.orders.values()} == {"U", "D"} and all(o.size == 5 for o in bot.broker.orders.values())
    assert j.kinds("fv")[0]["mid"] == pytest.approx((0.55 + 0.90) / 2)    # the book mid for the hourly markout-60
    run(bot.decide_once(now + 200))                                         # nothing moved: no requote
    assert len(j.kinds("mm_quote")) == 2
    bot.st.prices["btc"].bn_hist.append((now + 300, 100.0 * math.exp(11e-4)))   # +1 bp (no jump): fair Up +2.3 c
    run(bot.decide_once(now + 400))
    re = [x for x in j.kinds("mm_quote") if x["re"]]
    assert {x["side"] for x in re} == {"U", "D"} and len(bot.mm_bids) == 2
    assert [o for o in bot.broker.orders.values() if o.cancel_at is not None]  # the old bids are being cancelled


def test_no_bids_outside_the_quote_window_or_without_a_model(tmp_path):
    for rel in (10, 235):
        bot, j, w, now = _bot(tmp_path, rel=rel)
        run(bot.decide_once(now))
        assert not j.kinds("mm_quote") and not bot.mm_bids
    bot, j, w, now = _bot(tmp_path)
    bot.bsig["btc"].r2.clear()                                              # no sigma yet (cold start)
    run(bot.decide_once(now))
    assert not bot.mm_bids and bot.last_why[w.key] == "no_sigma"


def test_a_binance_jump_pulls_both_bids_for_pause_s(tmp_path):
    bot, j, w, now = _bot(tmp_path)
    run(bot.decide_once(now))
    assert len(bot.mm_bids) == 2
    assert bot.pull_limit_bp("btc") == pytest.approx(2.5 * 1.0 * math.sqrt(2.0), rel=1e-6)   # sigma 1 bp: 3.54 bp
    bot.st.prices["btc"].bn_hist.append((now + 900, 100.0 * math.exp(14e-4)))    # +4 bp within 2 s
    run(bot.decide_once(now + 1000))
    assert not bot.mm_bids and j.kinds("mm_pull") and bot.last_why[w.key] == "pause"
    assert {x["why"] for x in j.kinds("mm_cancel")} == {"pause"}
    run(bot.decide_once(now + 2000))                                        # still inside the pause
    assert not bot.mm_bids
    bot.st.prices["btc"].bn_hist.append((now + 3500, 100.0 * math.exp(14e-4)))
    bot.st.feed_alive["binance"] = bot.st.feed_alive["clob"]["btc"] = now + 3900
    run(bot.decide_once(now + 4000))                                        # calm again and the pause is over
    assert len(bot.mm_bids) == 2


def test_paper_fills_queue_and_settlement(tmp_path):
    bot, j, w, now = _bot(tmp_path)
    bot.st.books["U"].bids[0.0] = 0.0
    run(bot.decide_once(now))
    bu, bd = bot.mm_bids[(w.key, "U")]["price"], bot.mm_bids[(w.key, "D")]["price"]
    # before the bid is live (60 ms): no fill
    bot._on_public_trade("U", "SELL", bu - 0.05, 10.0, now + 10, now + 30)
    assert not bot.entries
    # a taker sells Up below our bid: we were the better bid -> 5 shares at our price
    bot._on_public_trade("U", "SELL", bu - 0.02, 10.0, now + 100, now + 120)
    assert bot.held(w.key) == (5.0, 0.0) and (w.key, "U") not in bot.mm_bids
    f = j.kinds("mm_fill")[0]
    assert f["side"] == "U" and f["price"] == bu and f["edge"] == pytest.approx(f["p"] - bu, abs=1e-4) and f["up"] == 5.0
    # a taker BUYS Up at 1 - bd: that is a mint against our Down bid (the books are mirrors)
    bot._on_public_trade("U", "BUY", round(1 - bd + 0.01, 2), 3.0, now + 150, now + 170)
    assert bot.held(w.key) == (5.0, 3.0)
    assert bot.worst(w.key) == pytest.approx(5 * bu + 3 * bd - 3.0)
    bot._settle(w.key, "Up")
    s = j.kinds("settle")[0]
    assert s["pnl"] == pytest.approx(5.0 - 5 * bu - 3 * bd, abs=1e-4) and not bot.entries


def test_a_trade_at_our_price_eats_the_queue_first(tmp_path):
    bot, j, w, now = _bot(tmp_path)
    run(bot.decide_once(now))
    o = next(o for o in bot.broker.orders.values() if o.token == "U")
    o.q_ahead = 8.0                                                         # 8 shares rested at our price before us
    bot._on_public_trade("U", "SELL", o.price, 6.0, now + 100, now + 120)
    assert not bot.entries and o.q_ahead == pytest.approx(2.0)
    bot._on_public_trade("U", "SELL", o.price, 4.0, now + 130, now + 150)  # 2 for the queue, 2 for us
    assert bot.held(w.key) == (2.0, 0.0)


def test_inventory_limits_and_the_open_risk_cap(tmp_path):
    bot, j, w, now = _bot(tmp_path)
    bot.entries[w.key] = [{"side": "U", "shares": 10.0, "price": 0.6, "cost": 6.0, "fee": 0.0}]
    run(bot.decide_once(now))
    assert (w.key, "U") not in bot.mm_bids and (w.key, "D") in bot.mm_bids   # 10 unpaired Up: only the Down bid rests ...
    pd_ = 1 - _fair_up(10.0, now / 1000 - w.start)
    assert bot.mm_bids[(w.key, "D")]["price"] == pytest.approx(math.floor(pd_ * 100 + 1e-9) / 100)   # ... at its fair value
    bot2, j2, w2, now2 = _bot(tmp_path, cfg_over={"risk": {"max_open_usd": 10.0}})
    bot2.entries[w2.key] = [{"side": "U", "shares": 9.0, "price": 0.6, "cost": 5.4, "fee": 0.0}]
    run(bot2.decide_once(now2))                                             # worst 5.4 + a bid: only one side fits
    assert len(bot2.mm_bids) == 1 and bot2.stats["skipped_risk"] >= 1
    assert bot2.exposure() <= 10.0 + 1e-9


def test_markouts_and_kill_file(tmp_path):
    bot, j, w, now = _bot(tmp_path)
    run(bot.decide_once(now))
    bu = bot.mm_bids[(w.key, "U")]["price"]
    bot._on_public_trade("U", "SELL", bu - 0.02, 5.0, now + 100, now + 120)
    real = int(time.time() * 1000)
    bot._markouts(real + 6000)
    m = j.kinds("mm_markout")
    assert [x["h"] for x in m] == [5] and abs(m[0]["d"]) < 0.01            # Binance did not move: only time passed
    open(str(tmp_path / "STOP"), "w").close()
    run(bot.decide_once(now + 200))
    assert not bot.mm_bids and bot.last_why[w.key] == "risk:kill_file"


# ---------------------------------------------------------------- reports/95: favourite skew, Binance gate
V2OVER = {"maker": {"half_spread": 0.015, "dog_spread": 0.10, "gate_bp": 0.5, "ref": "min"}, "lock": {"rel_lo": 100.0}}


def test_the_underdog_has_its_own_wide_spread_and_the_skew_keeps_it():
    mm = MakerCfg(half_spread=0.015, dog_spread=0.10, skew_c=0.01, shares=5, max_unpaired=10, max_side_shares=25)
    assert bid_target(mm, 0.653, 0, 0, None, True) == (0.63, "")             # 0.638 -> the 0.01 grid
    assert bid_target(mm, 0.353, 0, 0, None, False) == (0.25, "")            # 10 c under: 0.253 -> 0.25
    assert bid_target(mm, 0.353, 5, 5, None, False) == (0.24, "")            # long 5: 1 c lower still
    assert bid_target(mm, 0.353, 0, -5, None, False) == (0.26, "")           # short 5: 1 c higher, never above its reference
    assert bid_target(mm, 0.353, 0, 0, None) == (0.33, "")                   # fav defaults to True: the old call keeps working
    assert bid_target(mm, 0.13, 0, 0, None, False) == (None, "too_low")      # 0.13 - 10 c = 0.03: under the broker's 0.05 floor
    assert bid_target(mm, 0.153, 0, 0, None, False) == (0.05, "")
    off = MakerCfg(half_spread=0.015, dog_spread=0.0)                       # dog_spread 0 = the same spread on both sides
    assert bid_target(off, 0.353, 0, 0, None, False) == bid_target(off, 0.353, 0, 0, None, True)
    for bad in ({"maker": {"dog_spread": 0.6}}, {"maker": {"fav_p": 0.9}}, {"maker": {"gate_bp": 20.0}}, {"maker": {"gate_s": 0.5}}):
        with pytest.raises(ValueError):
            load_maker_config(CFG, overrides=bad)


def test_the_favourite_is_bid_close_and_the_underdog_deep(tmp_path):
    bot, j, w, now = _bot(tmp_path, rel=140, cfg_over=V2OVER)
    run(bot.decide_once(now))
    pu = _fair_up(10.0, now / 1000 - w.start)
    q = {x["side"]: x for x in j.kinds("mm_quote")}
    up_ref = min(pu, (0.55 + 0.90) / 2)                                      # the book mid caps the favourite (ref = "min")
    dn_ref = min(1 - pu, (0.09 + 0.45) / 2)
    assert pu > 0.5 and q["U"]["fav"] is True and q["D"]["fav"] is False
    assert q["U"]["price"] == pytest.approx(math.floor((up_ref - 0.015) * 100 + 1e-9) / 100)
    assert q["D"]["price"] == pytest.approx(math.floor((dn_ref - 0.10) * 100 + 1e-9) / 100)
    assert q["U"]["price"] - q["D"]["price"] > 0.5                           # the underdog sits far below, not at fair - 1 c
    assert not bot.gate_blocks("btc", "U") and not bot.gate_blocks("btc", "D")


def test_the_gate_pulls_the_side_binance_moved_against_and_keeps_the_other(tmp_path):
    bot, j, w, now = _bot(tmp_path, rel=140, cfg_over=V2OVER)
    run(bot.decide_once(now))
    assert len(bot.mm_bids) == 2
    # Binance -1.5 bp within the last 3 s: against Up, toward Down; below the 3.5 bp jump limit, so no both-sides pull
    bot.st.prices["btc"].bn_hist.append((now + 100, 100.0 * math.exp(10e-4) * math.exp(-1.5e-4)))
    assert bot.gate_blocks("btc", "U") and not bot.gate_blocks("btc", "D")
    run(bot.decide_once(now + 200))
    assert (w.key, "U") not in bot.mm_bids and (w.key, "D") in bot.mm_bids
    assert [x["why"] for x in j.kinds("mm_cancel") if x["side"] == "U"] == ["gate"] and not j.kinds("mm_pull")
    # a move back the other way (toward Up): the gate lifts, the Up bid returns
    bot.st.prices["btc"].bn_hist.append((now + 250, 100.0 * math.exp(10e-4) * math.exp(+2.0e-4)))
    bot.st.feed_alive["binance"] = bot.st.feed_alive["clob"]["btc"] = now + 400
    assert not bot.gate_blocks("btc", "U")
    run(bot.decide_once(now + 300))
    assert (w.key, "U") in bot.mm_bids


def test_the_gate_is_off_in_the_old_configs(tmp_path):
    bot, j, w, now = _bot(tmp_path)                                         # maker_fair.toml: gate_bp 0, no dog spread
    run(bot.decide_once(now))
    bot.st.prices["btc"].bn_hist.append((now + 100, 100.0 * math.exp(10e-4) * math.exp(-1.5e-4)))
    assert not bot.gate_blocks("btc", "U")
    assert bot.mm.dog_spread == 0.0 and bot.mm.gate_bp == 0.0


def test_quotes_only_from_second_100_with_the_v2_window(tmp_path):
    for rel, quoted in ((99, False), (101, True), (229, True), (232, False)):
        bot, j, w, now = _bot(tmp_path, rel=rel, cfg_over=V2OVER)
        run(bot.decide_once(now))
        assert bool(bot.mm_bids) == quoted, rel
