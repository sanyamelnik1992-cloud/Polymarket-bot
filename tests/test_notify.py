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
    assert "00:00 UTC" in alerts(mode="live").render("kill", {"reason": "daily loss limit 50 reached"})
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
    assert "<b>A</b>" in d and "+5.50$" in d and "окон 1 (✅1 ❌0)" in d and balanced(d)
    assert "сделок не было" in d                           # C had no closed windows this hour
    assert "C</b> vs" in d and "+2.00$" in d and "строгий лучше" in d   # 7.5 - 5.5
    st = notify.format_status()
    assert "+5.50$" in st and "kill switch" in st and balanced(st)
    assert balanced(notify.format_trades())
