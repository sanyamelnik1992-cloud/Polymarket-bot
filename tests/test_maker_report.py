"""The maker's periodic Telegram report (pmbot/bot/makerreport.py, FairMaker.report_*, notify.maker_report / format_maker,
reports/95 section 10; owner 26 Nov: "добавь какой то отчет по мейкеру в тг, например каждые 30 мин или час")."""
import json
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot import notify, ops  # noqa: E402
from pmbot.bot.alerts import TelegramAlerts  # noqa: E402
from pmbot.bot.fairmaker import load_maker_config, load_maker_live_config  # noqa: E402
from pmbot.bot.makerreport import GROUPS, Interval, cancel_group, next_boundary  # noqa: E402
from test_maker_live import LIVE, V2, broker, live_bot, run  # noqa: E402
from test_notify import balanced  # noqa: E402

BASE = 1_800_000_000.0          # a multiple of 1800 and of 3600 s


def sample(**kw):
    rec = {"mode": "live", "t0": BASE - 1800, "t1": BASE, "minutes": 30, "state": "ok", "windows_quoted": 6, "windows_filled": 2,
           "cancels": {"requote": 120, "pause": 8, "gate": 15, "end": 12, "limits": 3, "trouble": 0},
           "fills": 4, "shares": 20.0, "cost": 9.2, "fav": 3, "dog": 1, "up": 15.0, "dn": 5.0, "avg_price": 0.46, "edge_c": 1.4,
           "mo_c": {"5": -0.8, "30": -1.2}, "mo_n": {"5": 4, "30": 4}, "closed": 3, "won": 2, "lost": 1, "pnl": 0.42, "paired": 1,
           "quotes": 148, "requotes": 120, "pulls": 4, "guard": 6, "lag_over_100ms": 2, "skipped_risk": 0,
           "open": {"windows": 1, "up": 5.0, "dn": 0.0, "worst": 2.3, "bids": 2}, "day": -1.2, "day_limit": 5.0, "days": 2,
           "total": 3.55,
           "broker": {"placed": 148, "rejected": 1, "crossed": 6, "cancel_late": 1, "failed": 0, "adopted": 0, "reconciled": 0},
           "lat": {"place": {"n": 148, "med": 150.2, "p95": 640.0, "max": 900}, "cancel": {"n": 140, "med": 187.0, "p95": 780.0, "max": 1200}},
           "balance": 361.2}
    rec.update(kw)
    return rec


# ---------------------------------------------------------------- pure parts
def test_boundary_is_the_next_wall_clock_multiple():
    assert BASE % 3600 == 0
    assert next_boundary(BASE + 1, 1800) == BASE + 1800
    assert next_boundary(BASE + 1799.9, 1800) == BASE + 1800
    assert next_boundary(BASE, 1800) == BASE + 1800                  # exactly on it: the next one, never a zero wait
    assert next_boundary(BASE + 10, 3600) == BASE + 3600


def test_cancel_reasons_fall_into_report_groups_and_unknown_ones_are_trouble():
    want = {"requote": "requote", "pause": "pause", "gate": "gate", "time": "end", "window_gone": "end", "stop": "end",
            "price": "limits", "side_full": "limits", "unpaired": "limits", "too_low": "limits", "risk:max_open": "limits",
            "risk:killed": "limits", "broker:user_stream_down": "trouble", "bn_stale": "trouble", "book_stale": "trouble",
            "no_strike": "trouble", "something new": "trouble"}
    for why, group in want.items():
        assert cancel_group(why) == group, why
    assert set(want.values()) == set(GROUPS)


def test_interval_summary_weights_edge_and_markouts_by_shares():
    iv = Interval(1000.0)
    for key in ("btc-1", "btc-1", "btc-2"):
        iv.quote(key)
    for why in ("requote", "requote", "pause", "weird"):
        iv.cancel(why)
    iv.fill("btc-1", "U", 0.40, 5, True, 0.02)
    iv.fill("btc-1", "D", 0.50, 5, False, -0.01)
    iv.fill("btc-2", "U", 0.30, 2, None, None)                       # an adopted order: neither side nor edge known
    iv.markout(5, 0.01, 5)
    iv.markout(5, -0.03, 5)
    iv.markout(30, None, 5)                                           # the model had no value 30 s later
    iv.settle(2.5, 5, 0)
    iv.settle(-2.0, 5, 5)
    s = iv.summary()
    assert (s["windows_quoted"], s["windows_filled"]) == (2, 2)
    assert s["cancels"] == {"requote": 2, "pause": 1, "gate": 0, "end": 0, "limits": 0, "trouble": 1}
    assert (s["fills"], s["shares"], s["fav"], s["dog"], s["up"], s["dn"]) == (3, 12.0, 1, 1, 7.0, 5.0)
    assert s["avg_price"] == pytest.approx((2.0 + 2.5 + 0.6) / 12, abs=1e-4)
    assert s["edge_c"] == pytest.approx(0.5)                          # (0.02 x 5 - 0.01 x 5) / 10 shares = 0.5 c
    assert s["mo_c"] == {"5": -1.0} and s["mo_n"] == {"5": 2}
    assert (s["closed"], s["won"], s["lost"], s["pnl"], s["paired"]) == (2, 1, 1, 0.5, 1)
    json.dumps(s)
    empty = Interval(0).summary()
    assert empty["avg_price"] is None and empty["edge_c"] is None and empty["mo_c"] == {} and empty["fills"] == 0


# ---------------------------------------------------------------- the maker's record
def test_report_record_counts_what_the_maker_did_and_resets(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    assert len(bot.mm_bids) == 2
    up = bot.mm_bids[(w.key, "U")]
    bot._on_mm_fill({"key": w.key, "side": "U", "size": 5, "price": up["price"], "order_id": up["oid"], "ts": now})
    bot.st.prices["btc"].bn_hist.append((now + 100, 100.0 * math.exp(40e-4)))          # a Binance jump: the other bid is pulled
    run(bot.decide_once(now + 150))
    assert not bot.mm_bids
    bot._settle(w.key, "Up")
    br.stats = {"placed": 2, "rejected": 1, "cancel_late": 1}
    rec = bot.report_record(time.time())
    assert rec["mode"] == "live" and rec["state"] == "ok" and rec["t1"] >= rec["t0"]
    assert (rec["quotes"], rec["windows_quoted"], rec["windows_filled"]) == (2, 1, 1)
    assert (rec["fills"], rec["shares"], rec["up"], rec["dn"]) == (1, 5.0, 5.0, 0.0) and rec["fav"] + rec["dog"] == 1
    assert rec["avg_price"] == pytest.approx(up["price"], abs=1e-4)
    assert rec["cancels"] == {"requote": 0, "pause": 1, "gate": 0, "end": 0, "limits": 0, "trouble": 0}
    assert (rec["closed"], rec["won"], rec["lost"], rec["paired"]) == (1, 1, 0, 0)
    assert rec["pnl"] == pytest.approx(5 - 5 * up["price"], abs=1e-4)
    assert rec["total"] == pytest.approx(rec["pnl"], abs=1e-4) and rec["days"] == 1
    assert rec["open"] == {"windows": 0, "up": 0.0, "dn": 0.0, "worst": 0.0, "bids": 0}
    assert rec["broker"]["rejected"] == 1 and rec["broker"]["cancel_late"] == 1 and rec["day_limit"] == 5.0
    assert rec["lat"] == {} and rec["balance"] is None              # the fake broker keeps no latency or balance
    json.dumps(rec)
    again = bot.report_record(time.time() + 10)                         # the next interval starts at the report
    assert (again["quotes"], again["fills"], again["closed"], again["broker"]["rejected"]) == (0, 0, 0, 0)
    assert sum(again["cancels"].values()) == 0 and again["windows_quoted"] == 0
    assert again["total"] == rec["total"]                               # what the day and the test earned does not reset


def test_a_fault_in_the_report_never_disturbs_orders_fills_or_settlement(tmp_path):
    """The report is a view. Its accumulators are called from the order, fill and settle paths (real money): a bug in them is
    logged and dropped, the bookkeeping goes on."""
    bot, j, br, w, now = live_bot(tmp_path)

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("the report is broken")

    bot.iv = Broken()
    run(bot.decide_once(now))
    assert len(bot.mm_bids) == 2 and bot.stats["quotes"] == 2                       # quoting went on
    up = bot.mm_bids[(w.key, "U")]
    bot._on_mm_fill({"key": w.key, "side": "U", "size": 5, "price": up["price"], "order_id": up["oid"], "ts": now})
    assert bot.held(w.key) == (5.0, 0.0) and len(j.of("fill")) == 1 and len(j.of("mm_fill")) == 1
    bot.st.prices["btc"].bn_hist.append((now + 100, 100.0 * math.exp(40e-4)))
    run(bot.decide_once(now + 150))
    assert not bot.mm_bids and j.of("mm_cancel")                                    # the jump pull cancelled the other bid
    bot._settle(w.key, "Up")
    assert j.of("settle") and bot.risk["realized_by_day"] and not bot.entries


def test_open_position_is_part_of_the_record(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    up = bot.mm_bids[(w.key, "U")]
    bot._on_mm_fill({"key": w.key, "side": "U", "size": 5, "price": up["price"], "order_id": up["oid"], "ts": now})
    rec = bot.report_record(time.time())
    assert rec["open"]["windows"] == 1 and (rec["open"]["up"], rec["open"]["dn"]) == (5.0, 0.0)
    assert rec["open"]["worst"] == pytest.approx(5 * up["price"], abs=1e-4) and rec["open"]["bids"] == 1   # only the Down bid rests


def test_state_says_why_the_maker_is_not_quoting(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    assert bot.report_state() == "ok"
    br.ok = (False, "user_stream_down")
    assert bot.report_state() == "user_stream_down"
    br.ok = (True, "")
    bot.risk["killed_day"] = bot.today()
    assert bot.report_state() == "killed"
    bot.risk["killed_day"] = None
    open(bot.cfg.risk.kill_file, "w").write("x")
    assert bot.report_state() == "kill_file"
    assert bot.risk["killed_day"] is None                              # asking never stops the bot


def test_report_once_writes_the_journal_record(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    rec = bot.report_once(time.time())
    (logged,) = j.of("mm_report")
    assert logged == rec and logged["quotes"] == 2


# ---------------------------------------------------------------- the loop
def test_report_loop_waits_for_the_boundary_and_skips_a_short_first_interval(tmp_path):
    """Every deploy restarts the maker: a restart 100 s before :00 must not send a 100-second "report"; its counters go on
    into the next one (here 32 minutes)."""
    bot, j, br, w, now = live_bot(tmp_path)
    clock, sleeps = {"t": BASE + 1700}, []
    bot.iv = Interval(clock["t"])

    async def sleep(dt):
        sleeps.append(dt)
        clock["t"] += dt
        if len(sleeps) == 3:
            bot._stop = True

    run(bot.report_loop(clock=lambda: clock["t"], sleep=sleep))
    assert sleeps == [100.0, 1800.0, 1800.0]
    (rec,) = j.of("mm_report")
    assert rec["t0"] == BASE + 1700 and rec["t1"] == BASE + 3600 and rec["minutes"] == 32


def test_report_loop_waits_out_an_early_wake_up(tmp_path):
    """asyncio sleeps on the monotonic clock: a wake-up a few ms before the boundary must not stamp the report 29:59."""
    bot, j, br, w, now = live_bot(tmp_path)
    clock, sleeps = {"t": BASE - 1000}, []
    bot.iv = Interval(BASE - 1000)

    async def sleep(dt):
        sleeps.append(round(dt, 3))
        clock["t"] += dt - 0.03 if len(sleeps) == 1 else dt         # the first sleep ends 30 ms short
        if len(sleeps) == 3:
            bot._stop = True

    run(bot.report_loop(clock=lambda: clock["t"], sleep=sleep))
    assert sleeps[:2] == [1000.0, 0.04]
    (rec,) = j.of("mm_report")
    assert rec["t1"] == BASE and rec["minutes"] == 17


def test_report_loop_survives_a_failing_report(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    clock = {"t": BASE + 10}
    bot.iv = Interval(BASE - 1790)

    def boom(t):
        raise RuntimeError("boom")

    bot.report_once = boom

    async def sleep(dt):
        clock["t"] += dt
        bot._stop = True

    async def go():
        await bot.report_loop(clock=lambda: clock["t"], sleep=sleep)         # stopped right after the first sleep: no report at all
        bot._stop = False
        stops = []

        async def sleep2(dt):
            clock["t"] += dt
            stops.append(1)
            if len(stops) == 2:
                bot._stop = True
        await bot.report_loop(clock=lambda: clock["t"], sleep=sleep2)         # the first report raises, the loop goes on
    run(go())
    assert [kw["where"] for kw in j.of("error")] == ["mm_report"]


def test_report_every_s_switches_the_loop_off_and_is_validated(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    loops = bot.extra_loops()
    assert len(loops) == 1
    loops[0].close()
    bot.mm.report_every_s = 0
    assert bot.extra_loops() == []
    for bad in (60, 299, 7201, -1):
        with pytest.raises(ValueError):
            load_maker_live_config(LIVE, live_flag=True, overrides={"maker": {"report_every_s": bad}})
    for ok in (0, 300, 1800, 7200):
        assert load_maker_live_config(LIVE, live_flag=True, overrides={"maker": {"report_every_s": ok}})[1].report_every_s == ok
    assert load_maker_live_config(LIVE, live_flag=True)[1].report_every_s == 1800
    assert load_maker_config(V2)[1].report_every_s == 3600                  # paper: hourly, one Telegram message for all (reports/96)


def test_the_live_broker_reports_latency_per_interval():
    b = broker()
    end = int(time.time()) + 250

    async def go():
        assert await b.place("U", "btc-1", "U", 0.47, 5, 0, end_s=end)
        assert await b.place("D", "btc-1", "D", 0.52, 5, 0, end_s=end)
        await b.cancel(list(b.orders), 0)
    run(go())
    rep = b.latency_report()
    assert rep["place"]["n"] == 2 and rep["cancel"]["n"] == 1
    assert 0 <= rep["place"]["med"] <= rep["place"]["p95"] <= rep["place"]["max"]       # the fake exchange answers at once: ~0 ms
    assert b.latency_report() == {"place": {"n": 0}, "cancel": {"n": 0}}      # reading starts the next interval
    assert b.stats["place_ms"] is not None                                   # the status line's rolling median is separate


# ---------------------------------------------------------------- Telegram
def test_the_message_is_short_plain_russian_and_balanced():
    msg = notify.maker_report(sample())
    assert balanced(msg) and len(msg) < 1200
    for part in ("Мейкер · живой тест", "💰", "за 30 мин", "🟢 работает", "окон с позицией закрыто 3 (+2/−1)", "+0.42$",
                 "с парой Up+Down: 1", "сегодня <b>−1.20$</b> (стоп при −5.00$)", "всего +3.55$ за 2 дн.",
                 "окон с заявками 6 · поставлено заявок 148 · исполнений 4 (2.7%)", "куплено 20 акций по 0.46 (Up 15 · Down 5)",
                 "фаворит 3 · аутсайдер 1", "ниже модельной цены на 1.4¢", "за 5 с −0.8¢, за 30 с −1.2¢ (минус = против нас)",
                 "в позиции окон 1: Up 5 · Down 0 · худший случай −2.30$ · заявок на бирже 2",
                 "подвинуто 120 · пауза Binance 8 · фильтр стороны 15 · конец окна 12 · лимиты 3",
                 "поставить 150 мс (p95 640) · снять 187 мс (p95 780)", "не успели снять 1", "отказов биржи 1", "кошелёк 361.20$"):
        assert part in msg, part
    assert "сбои" not in msg and "позиций нет" not in msg


def test_a_quiet_interval_and_a_paused_maker_are_said_plainly():
    quiet = sample(state="user_stream_down", fills=0, quotes=0, closed=0, cancels={}, open={"windows": 0, "bids": 0}, lat={}, mo_c={},
                   broker={}, lag_over_100ms=0, shares=0, avg_price=None, edge_c=None)
    msg = notify.maker_report(quiet)
    assert balanced(msg) and "⏸ пауза: нет потока исполнений" in msg and "окон с позицией закрыто 0" in msg
    assert "исполнений 0" in msg and "куплено" not in msg and "позиций нет · заявок на бирже 0" in msg
    assert "Заявок снято" not in msg and "кошелёк 361.20$" in msg
    for state in ("kill_file", "killed", "broker_fatal", "order_outcome_unknown", "api_backoff", "heartbeat_down", "low_balance"):
        assert notify.MAKER_STATE_RU[state] in notify.maker_report(sample(state=state))
    assert "⚠️ new<b>x</b>" not in notify.maker_report(sample(state="new<b>x</b>"))           # an unknown state is escaped
    trouble = notify.maker_report(sample(cancels={"requote": 1, "trouble": 2}, broker={"failed": 1}))
    assert "⚠️ сбои 2" in trouble and "⚠️ сделок не прошло 1" in trouble and balanced(trouble)
    paper = notify.maker_report(sample(mode="paper", lat={}, broker={}, balance=None), "pmbot-maker-v2")
    assert "(paper)" in paper and "Связь" not in paper and "💰" not in paper and "Мейкер v2" in paper


def test_the_report_goes_to_telegram_in_live_only_and_replaces_the_window_messages():
    rec = sample()
    live = TelegramAlerts(token="", chat="", config_path="config/maker_live.toml", mode="live")
    assert live.unit == "pmbot-maker-live" and live.unit in notify.REPORTED_UNITS and live.unit not in notify.QUIET_SETTLES
    msg = live.render("mm_report", rec)
    assert msg and "окон с позицией закрыто 3" in msg and balanced(msg)
    assert live.render("mm_report", rec)                                  # never rate-limited: one every 30 minutes, all of them are wanted
    settle = {"key": "btc-1793988600", "winner": "Up", "pnl": 1.2, "up": 5, "down": 0, "cost": 2.3}
    assert live.render("settle", settle) is None                           # no message per window, no hourly summary: the report has them
    assert live.render("kill", {"reason": "daily loss limit 5.0 reached"}) and live.render("start", {"mode": "live"})
    debug = TelegramAlerts(token="", chat="", config_path="config/maker_live.toml", mode="live")
    debug.per_window = True                                               # PMBOT_TG_SETTLES=1 still shows every window
    assert debug.render("settle", settle)
    paper = TelegramAlerts(token="", chat="", config_path="config/maker_v2.toml", mode="paper")
    assert paper.unit == "pmbot-maker-v2" and paper.render("mm_report", dict(rec, mode="paper")) is None
    other = TelegramAlerts(token="", chat="", config_path="config/lock_99_live.toml", mode="live")
    assert other.render("settle", settle)                                  # the other live bots keep their messages


def test_event_message_needs_no_unit():
    assert "Мейкер · живой тест" in notify.event_message("mm_report", sample(), unit=None, mode="live")


def make_maker_repo(tmp_path, monkeypatch, reports, state="active"):
    """A repo with the maker's unit, config and journal runs; `reports` = [[run name, [events]], ...]."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "deploy" / "pmbot-maker-live.service").write_text(
        "[Service]\nExecStart=__REPO__/.venv/bin/python scripts/run_maker.py --config config/maker_live.toml --live\n")
    (tmp_path / "config" / "maker_live.toml").write_text(
        'journal_dir = "data/jm"\n[risk]\nstate_file = "data/state/m.json"\n[execution]\nmode = "live"\n')
    for name, events in reports:
        d = tmp_path / "data" / "jm" / name
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in events) + "\n")
    monkeypatch.setattr(ops, "REPO", str(tmp_path))
    monkeypatch.setattr(ops, "unit_state", lambda u: (state, time.time() - 3600))


def test_the_maker_command_shows_the_newest_report(tmp_path, monkeypatch):
    now = time.time()
    old = dict(sample(), t=now - 7200, type="mm_report", pnl=-9.99)
    new = dict(sample(), t=now - 600, type="mm_report")
    make_maker_repo(tmp_path, monkeypatch, [("20261126-100000", [old, {"t": now - 6000, "type": "fv"}]),
                                            ("20261126-120000", [{"t": now - 1500, "type": "fv"}, new, {"t": now - 5, "type": "fv"}])])
    text = notify.format_maker()
    assert balanced(text) and "−9.99$" not in text and "окон с позицией закрыто 3" in text
    assert "10 мин назад" in text and "приходят сами в :00 и :30 UTC" in text
    assert ops.last_event("data/jm", "mm_report")["t"] == new["t"] and ops.last_event("data/jm", "nothing") is None
    assert ops.last_event("data/none", "mm_report") is None


def test_the_maker_command_when_stopped_or_without_a_report(tmp_path, monkeypatch):
    now = time.time()
    make_maker_repo(tmp_path, monkeypatch, [("20261126-120000", [dict(sample(), t=now - 600, type="mm_report")])], state="inactive")
    text = notify.format_maker()
    assert "⚪ выключен\nЗапуск: /restart pmbot-maker-live" in text and "окон с позицией закрыто 3" in text and balanced(text)


def test_the_maker_command_before_the_first_report(tmp_path, monkeypatch):
    make_maker_repo(tmp_path, monkeypatch, [("20261126-120000", [{"t": time.time(), "type": "start"}])])
    text = notify.format_maker()
    assert "Отчётов в журнале ещё нет" in text and "🟢 работает" in text and balanced(text) and "Запуск" not in text


def test_the_command_is_wired_into_the_telegram_bot():
    src = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "tg_bot.py")).read()
    assert '"/maker": lambda a: notify.format_maker()' in src and '"/maker"}' in src      # a read-only command for viewers too
    assert "/maker —" in src and '("/maker",' in src                                        # in /help and in the command menu
    assert '"type":"mm_report"' in open(os.path.join(os.path.dirname(__file__), "..", "pmbot", "ops.py")).read()
