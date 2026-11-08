"""Server ops helpers (pmbot/ops.py) and the Telegram bot's command routing, on a fake repo layout."""
import importlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot import ops  # noqa: E402


def make_repo(tmp_path, monkeypatch):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "deploy" / "pmbot-5m.service").write_text(
        "[Service]\nExecStart=__REPO__/.venv/bin/python scripts/run_bot.py --config config/a.toml\n")
    (tmp_path / "config" / "a.toml").write_text(
        'journal_dir = "data/j"\n[risk]\nstate_file = "data/state/a.json"\n[execution]\nmode = "paper"\n')
    run = tmp_path / "data" / "j" / "20261106-120000"
    run.mkdir(parents=True)
    now = time.time()
    ev = [{"t": now - 600, "type": "start", "mode": "paper"},
          {"t": now - 500, "type": "fv", "key": "btc-1", "p": 0.5},
          {"t": now - 400, "type": "fill", "key": "btc-1", "side": "U", "price": 0.45, "size": 10},
          {"t": now - 100, "type": "settle", "key": "btc-1", "winner": "Up", "pnl": 5.5, "up": 10, "down": 0,
           "cost": 4.5, "payoff": 10, "rebate": 0},
          {"t": now - 50, "type": "status", "pnl": 5.5, "open_worst": 0.0}]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in ev) + "\n")
    (tmp_path / "data" / "state").mkdir()
    (tmp_path / "data" / "state" / "a.json").write_text(json.dumps(
        {"realized_by_day": {"2026-11-05": -1.0, ops.today_utc(): 5.5}, "killed_day": ops.today_utc(),
         "kill_reason": "kill file STOP present"}))
    monkeypatch.setattr(ops, "REPO", str(tmp_path))
    monkeypatch.setattr(ops, "KILL_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(ops, "unit_state", lambda u: ("active", time.time() - 60) if u == "pmbot-5m" else ("inactive", None))
    return tmp_path


def test_bot_summary_reads_journal_and_state(tmp_path, monkeypatch):
    make_repo(tmp_path, monkeypatch)
    s = ops.bot_summary("pmbot-5m")
    assert s["journal"] == "data/j" and s["mode"] == "paper"
    assert s["pnl_today"] == 5.5 and s["pnl_all"] == 4.5
    assert len(s["settles"]) == 1 and len(s["fills"]) == 1 and s["wins"] == 1
    assert s["killed"]
    text = ops.format_status()
    assert "pmbot-5m" in text and "+5.50$" in text and "kill switch" in text
    assert "2026-11-05: -1.00$" in ops.format_pnl()
    assert "btc-1 → Up" in ops.format_trades()


def test_resume_clears_only_kill_file_kills(tmp_path, monkeypatch):
    root = make_repo(tmp_path, monkeypatch)
    (root / "STOP").write_text("x")
    removed, cleared = ops.resume_after_stop()
    assert removed and cleared == ["pmbot-5m"] and not (root / "STOP").exists()
    assert json.load(open(root / "data/state/a.json"))["killed_day"] is None
    # a daily-loss kill must survive /resume
    p = root / "data/state/a.json"
    p.write_text(json.dumps({"realized_by_day": {}, "killed_day": ops.today_utc(), "kill_reason": "daily loss limit 20"}))
    assert ops.resume_after_stop() == (False, [])
    assert json.load(open(p))["killed_day"] == ops.today_utc()


def test_tg_bot_obeys_only_its_chat(tmp_path, monkeypatch):
    make_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("PMBOT_TG_TOKEN", "t")
    monkeypatch.setenv("PMBOT_TG_CHAT", "42")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import tg_bot as tg  # noqa: E402
    importlib.reload(tg)  # re-read PMBOT_TG_* from the patched environment
    sent = []
    monkeypatch.setattr(tg, "send", lambda text, chat=None: sent.append((chat, text)))
    tg.handle({"chat": {"id": 7}, "text": "/stop"})
    assert sent == [] and not (tmp_path / "STOP").exists()
    tg.handle({"chat": {"id": 42}, "text": "/stop"})
    assert (tmp_path / "STOP").exists() and "STOP" in sent[-1][1]
    tg.handle({"chat": {"id": 42}, "text": "/status@mybot"})
    assert "<b>A</b>" in sent[-1][1] and "+5.50$" in sent[-1][1]
    for cmd in ("/trades", "/help", "/server"):
        tg.handle({"chat": {"id": 42}, "text": cmd})
        assert sent[-1][1] and not sent[-1][1].startswith("ошибка"), cmd


def test_deployed_5m_units_are_paper_with_own_journal_and_state():
    """Every 5m unit in deploy/ loads, is paper, and has its own journal dir and risk state file (a shared
    state file would mix the daily-loss kill switch of two bots)."""
    from pmbot.bot.config import load_config
    journals, states = set(), set()
    for unit in [u for u in ops.BOT_UNITS if u.startswith("pmbot-5m")]:
        path = ops.unit_config(unit)
        assert path, unit
        cfg = load_config(os.path.join(ops.REPO, path))
        # exactly one unit may trade real money, and its config and --live flag must agree
        if unit == "pmbot-5m-live":
            assert cfg.execution.mode == "live" and ops.unit_is_live(unit), unit
            assert cfg.risk.max_open_worst_loss <= 10 and cfg.risk.daily_loss_limit <= 5, "live limits raised?"
        else:
            assert cfg.execution.mode == "paper" and not ops.unit_is_live(unit), unit
        assert cfg.journal_dir not in journals and cfg.risk.state_file not in states, unit
        journals.add(cfg.journal_dir)
        states.add(cfg.risk.state_file)
