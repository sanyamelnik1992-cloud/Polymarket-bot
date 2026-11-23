"""Telegram message formatting (pmbot/notify.py) and the trading bots' alert filter (pmbot/bot/alerts.py)."""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot import notify, ops  # noqa: E402
from pmbot.bot.alerts import ERROR_EVERY_S, TelegramAlerts  # noqa: E402
from test_ops import make_repo  # noqa: E402


def balanced(text):
    """Every <b>/<code>/<pre> is closed and nothing else looks like a tag (Telegram rejects broken HTML)."""
    tags = re.findall(r"</?([a-z]+)>", text)
    assert set(tags) <= {"b", "i", "code", "pre"}, tags
    for t in ("b", "i", "code", "pre"):
        assert text.count(f"<{t}>") == text.count(f"</{t}>"), t
    return True


def alerts(mode="paper", per_window=False, clock=time.time):
    a = TelegramAlerts(token="", chat="", config_path="config/colo_v2.toml", mode=mode, clock=clock)
    a.per_window = per_window
    return a


def test_window_name_and_money():
    assert notify.window_name("btc-1793988600") == "BTC 18:10–18:15"
    assert notify.window_name("weird<key>") == "weird&lt;key&gt;"
    assert notify.money(-1.5) == "−1.50$" and notify.money(2, bold=True) == "<b>+2.00$</b>"
    assert notify.squares([1, -1, 0]) == "🟩🟥⬜"


def test_paper_windows_are_not_sent_one_by_one_but_live_ones_are():
    rec = {"key": "btc-1793988600", "winner": "Up", "pnl": 3.2, "up": 10, "down": 4, "cost": 6.8}
    assert alerts().render("settle", rec) is None
    live = alerts(mode="live").render("settle", rec)
    assert "BTC 18:10–18:15" in live and "+3.20$" in live and "Up 10 · Down 4" in live and balanced(live)
    assert alerts(per_window=True).render("settle", rec)


def test_live_interceptor_closed_windows_come_as_one_summary_an_hour():
    """reports/83 sections 10 and 13: the live Перехватчик closes dozens of windows a day; one message each would bury the alerts
    that matter, but with none at all the owner saw only the stop (22 Nov). So one summary an hour; start, stop and errors are
    sent at once; the other live bots keep their per-window messages."""
    clock = {"t": 1000.0}
    rec = {"key": "btc-1793988600", "winner": "Up", "pnl": -1.8, "side": "U", "shares": 5, "cost": 1.8}
    a = TelegramAlerts(token="", chat="", config_path="config/lock_fair_live.toml", mode="live", clock=lambda: clock["t"])
    assert a.unit == "pmbot-lock-fair-live" and a.unit in notify.QUIET_SETTLES
    assert a.render("settle", rec) is None
    clock["t"] += 1800
    assert a.render("settle", dict(rec, pnl=3.2)) is None
    clock["t"] += 1900                                                    # an hour after the first window of the period
    msg = a.render("settle", dict(rec, pnl=-0.5))
    assert "окон 3 (+1/−2)" in msg and "+0.90$" in msg and "с запуска: окон 3" in msg and balanced(msg)
    clock["t"] += 600
    assert a.render("settle", dict(rec, pnl=1.0)) is None                  # a new period starts
    clock["t"] += 3600
    msg = a.render("settle", dict(rec, pnl=1.0))
    assert "окон 2 (+2/−0)" in msg and "с запуска: окон 5 (+3/−2), итог +2.90$" in msg
    assert a.render("start", {"mode": "live"}) and a.render("kill", {"reason": "daily loss limit 20.0 reached"})
    other = TelegramAlerts(token="", chat="", config_path="config/lock_99_live.toml", mode="live")
    assert other.unit == "pmbot-lock-99-live" and other.render("settle", rec)


def test_paper_bots_are_silent_about_start_pause_and_final():
    a = alerts()
    for kind in ("start", "pause", "final"):
        assert a.render(kind, {"mode": "paper", "pnl": 1, "losing_streak": 8}) is None, kind
    assert a.render("kill", {"reason": "daily loss limit 50 reached"})      # the kill switch is always reported


def test_alert_texts_are_labelled_and_escaped():
    a = alerts(mode="live")
    assert a.unit == "pmbot-5m"
    for kind, rec in [("start", {"mode": "paper"}), ("pause", {"losing_streak": 8, "until": 1793988600}),
                      ("kill", {"reason": "daily loss limit 50 reached"}), ("final", {"pnl": -3, "unresolved": 1}),
                      ("order_rejected", {"key": "eth-1793988600", "error": "bad <html> & stuff"})]:
        msg = a.render(kind, rec)
        assert msg and "<b>A</b>" in msg and balanced(msg), kind
    live_stop = alerts(mode="live").render("kill", {"reason": "daily loss limit 50 reached"})
    assert "/restart pmbot-5m" in live_stop and "/resume" not in live_stop          # reports/83 section 15
    assert "00:00 UTC" in alerts().render("kill", {"reason": "daily loss limit 50 reached"})   # paper resumes by itself
    assert "/resume" in alerts().render("kill", {"reason": "kill file STOP present"})
    assert alerts().render("quote", {}) is None and alerts().render("fv", {}) is None


def test_errors_are_rate_limited_with_a_skipped_count():
    now = [1000.0]
    a = alerts(clock=lambda: now[0])
    assert a.render("order_rejected", {"error": "x"})
    assert a.render("order_rejected", {"error": "x"}) is None
    assert a.render("order_rejected", {"error": "x"}) is None
    assert a.render("heartbeat_error", {"error": "y"})          # another kind is not blocked
    now[0] += ERROR_EVERY_S + 1
    assert "ещё таких же" in a.render("order_rejected", {"error": "x"})


def test_fak_miss_is_routine_but_an_unknown_order_outcome_alerts():
    a = alerts(mode="live")
    assert a.render("order_rejected", {"key": "btc-1793988600", "code": "fak_not_filled", "message": "no orders"}) is None
    msg = a.render("order_failed", {"key": "btc-1793988600", "err": "TransportError", "error": "timed out"})
    assert msg and "исход неизвестен" in msg and balanced(msg)
    msg = a.render("order_pending", {"key": "btc-1793988600", "status": "delayed"})
    assert msg and "ещё не исполнена" in msg and balanced(msg)


def test_digest_daily_and_status_from_journals(tmp_path, monkeypatch):
    root = make_repo(tmp_path, monkeypatch)
    # a second, paired bot C next to A
    (root / "deploy" / "pmbot-5m-c.service").write_text(
        "[Service]\nExecStart=__REPO__/.venv/bin/python scripts/run_bot.py --config config/c.toml\n")
    (root / "config" / "c.toml").write_text('journal_dir = "data/jc"\n[risk]\nstate_file = "data/state/c.json"\n')
    (root / "data" / "state" / "c.json").write_text(json.dumps({"realized_by_day": {ops.today_utc(): 7.5}}))
    monkeypatch.setattr(ops, "unit_state", lambda u: ("active", time.time() - 60) if u in ("pmbot-5m", "pmbot-5m-c")
                        else ("inactive", None))
    d = notify.format_digest(time.time() - 3600)
    assert "A: 🟢" in d and "+5.50$" in d and "окон 1 (✅1 ❌0)" in d and balanced(d)
    assert "C: ⚪ сделок не было" in d                      # C had no closed windows this hour
    assert "C</b> vs" in d and "+2.00$" in d and "строгий лучше" in d   # 7.5 - 5.5
    assert "Тесты" in d and "Реальные деньги" not in d    # everything here is paper
    st = notify.format_status()
    assert "+5.50$" in st and "создан файл STOP" in st and "⛔ стоп на сегодня" in st and balanced(st)
    # real money and paper are separate blocks (reports/56, 88): everything running here is paper
    assert "ТЕСТЫ" in st and "сейчас ни один бот на деньгах не запущен" in st and "сегодня на реальных деньгах: <b>+0.00$</b>" in st
    assert notify.group_of("pmbot-lock-live", "live") == "live" and notify.group_of("pmbot-lock-99-live", "live") == "live"
    assert notify.group_of("pmbot-lock-tier", "paper") == "twin" and notify.group_of("pmbot-lock-99", "paper") == "twin"
    assert notify.group_of("pmbot-lock-tier-wide", "paper") == "lab"
    for u in ("pmbot-lock-live", "pmbot-lock-99-live", "pmbot-lock-tier", "pmbot-lock-99"):
        assert u in notify.BOTS and "·" in notify.BOTS[u][1]          # plain name first, the letter code second
    assert balanced(notify.format_trades())


def test_status_shows_running_bots_real_money_apart_and_the_shelf(tmp_path, monkeypatch):
    """reports/88 (owner 23 Nov): dropped strategies and switched-off tests are not listed; real money apart from the tests;
    shelved / unfinished strategies marked; a live bot stopped by its own day stop is shown with how to restart it, and a live
    bot stopped on purpose still counts in the day's real money."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "state").mkdir(parents=True)
    today = ops.today_utc()

    def unit(name, mode, pnl, extra="", killed=False):
        live = " --live" if mode == "live" else ""
        (tmp_path / "deploy" / f"{name}.service").write_text(
            f"[Service]\nExecStart=__REPO__/.venv/bin/python scripts/run_lock.py --config config/{name}.toml{live}\n")
        (tmp_path / "config" / f"{name}.toml").write_text(
            f'journal_dir = "data/j-{name}"\n[lock]\nshares = 10\n{extra}[risk]\nstate_file = "data/state/{name}.json"\n'
            f'daily_loss_limit = 9.5\n[execution]\nmode = "{mode}"\n')
        st = {"realized_by_day": {"2026-11-20": 1.0, today: pnl}}
        if killed:
            st.update(killed_day=today, kill_reason="daily loss limit 9.5 reached")
        (tmp_path / "data" / "state" / f"{name}.json").write_text(json.dumps(st))

    unit("pmbot-lock-99-live", "live", 0.19)
    unit("pmbot-lock-15-live", "live", -5.2, killed=True)
    unit("pmbot-lock-fair-live", "live", 0.37)
    unit("pmbot-lock-fair", "paper", -9.92)
    unit("pmbot-lock-15-fair", "paper", -1.39)
    running = {"pmbot-lock-99-live", "pmbot-lock-fair", "pmbot-feedrec"}
    monkeypatch.setattr(ops, "REPO", str(tmp_path))
    monkeypatch.setattr(ops, "KILL_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(ops, "unit_state", lambda u: ("active", time.time() - 600) if u in running else ("inactive", None))
    st = notify.format_status()
    assert balanced(st)
    live, rest = st.split("🧪 <b>ТЕСТЫ</b>")
    assert "Снайпер · 5 мин" in live and "10 акций · стоп −$9.5 в день" in live
    assert "дневной стоп" in live and "/restart pmbot-lock-15-live" in live           # stopped by its own day stop
    assert "Перехватчик" not in live and "⏹ остановленные сегодня: +0.37$" in live   # retired live: only its money
    assert "сегодня на реальных деньгах: <b>−4.64$</b>" in st                         # 0.19 - 5.2 + 0.37
    assert "Перехватчик · 5 мин" in rest and "−9.92$" in rest                           # a running test
    assert "−1.39$" not in st                                                           # a switched-off test is not listed
    assert "📡 <b>СБОР ДАННЫХ</b>" in rest and "Гонка источников цены" in rest
    assert "🗂 <b>ОТЛОЖЕНО И В РАБОТЕ</b>" in rest and all(n in rest for _, n, _ in notify.SHELF)
    assert notify.limits("pmbot-lock-99-live") == "10 акций · стоп −$9.5 в день"
    assert notify._shares_word(2) == "акции" and notify._shares_word(1) == "акция" and notify._shares_word(12) == "акций"
