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
            assert cfg.risk.max_open_worst_loss <= 6 and cfg.risk.daily_loss_limit <= 2, "kopeck limits raised?"
        else:
            assert cfg.execution.mode == "paper" and not ops.unit_is_live(unit), unit
        assert cfg.journal_dir not in journals and cfg.risk.state_file not in states, unit
        journals.add(cfg.journal_dir)
        states.add(cfg.risk.state_file)


def test_quality_markout_pairs_latency_and_wallet(tmp_path, monkeypatch):
    root = make_repo(tmp_path, monkeypatch)
    run = root / "data" / "j" / "20261108-170000"
    run.mkdir(parents=True)
    now = time.time()
    t0 = now - 900
    ev = [{"t": t0, "type": "status", "stats": {"lag_max_ms": 40, "lag_blocks": 3}},
          {"t": t0 + 1, "type": "order_placed", "latency_ms": 50},
          {"t": t0 + 2, "type": "order_placed", "latency_ms": 70},
          {"t": t0 + 3, "type": "order_cancelled", "latency_ms": 45},
          {"t": t0 + 10, "type": "fill", "key": "btc-9", "side": "U", "price": 0.40, "size": 5, "ts": (t0 + 10) * 1000},
          {"t": t0 + 11, "type": "fill", "key": "btc-9", "side": "D", "price": 0.50, "size": 5, "ts": (t0 + 11) * 1000}]
    ev += [{"t": t0 + 10 + i, "type": "fv", "key": "btc-9", "mid": 0.46} for i in range(0, 80, 5)]
    ev += [{"t": t0 + 400, "type": "settle", "key": "btc-9", "pnl": 0.5, "up": 5, "down": 5},
           {"t": t0 + 401, "type": "settle", "key": "btc-8", "pnl": -2.0, "up": 5, "down": 0},
           {"t": t0 + 402, "type": "settle", "key": "btc-7", "pnl": -9.0, "up": 0, "down": 9},   # never traded here
           {"t": t0 + 500, "type": "status", "stats": {"lag_max_ms": 120, "lag_blocks": 7}}]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in ev) + "\n")
    q = ops.quality("data/j", now - 3600)
    # Up bought 0.40, mid 60 s later 0.46: +6c; Down bought 0.50, Down mid 0.54: +4c -> +5c per share
    assert abs(q["markout60"] - 5.0) < 1e-6 and q["fills"] == 3                 # 3 = the older run's fill too
    assert (q["paired"], q["one_sided"]) == (1, 1) and q["lag_blocks"] == 4 and q["lag_max_ms"] == 120
    assert abs(q["pnl"] - 6.0) < 1e-9          # btc-1 +5.5 and btc-9 +0.5; btc-8 / btc-7 had no fill in this journal
    assert q["place_ms"][0] in (50, 70) and q["cancel_ms"] == (45, 45)
    (root / "data" / "state" / "wallet.json").write_text(json.dumps({"t": now, "usd": 4.3842, "positions_value": 0}))
    assert "кошелёк: 4.38$" in ops.format_quality(24) and "кошелёк: 4.38$" in ops.format_status()
    (root / "data" / "state" / "wallet.json").write_text(json.dumps({"t": now, "error": "TimeoutError"}))
    assert "нет данных (TimeoutError" in ops.wallet_line()


def test_unit_cpu_priority_live_lock_above_paper_bots():
    """Under CPU contention the live lock bot (real money, ~100 ms decisions) must outrank every paper bot and the recorder."""
    import glob
    import re

    def weight(path):
        m = re.search(r"(?m)^CPUWeight=(\d+)", open(path).read())
        return int(m.group(1)) if m else 100
    live = weight("deploy/pmbot-lock-live.service")
    paper = [f for f in glob.glob("deploy/pmbot-*.service") if re.search(r"pmbot-(5m-g2?|lock|lock-wide|lock-15|lock-maker2?|lock-tier|lock-tier-wide|recorder)\.service$", f)]
    assert len(paper) == 10, paper
    assert live > 100 and all(weight(f) < 100 for f in paper)


def test_cpu_stat_shares_and_psi_parsers():
    a = ops.parse_cpu_stat("cpu  100 0 50 800 10 0 5 35 0 0\ncpu0 1 1 1 1 1 1 1 1\n")
    b = ops.parse_cpu_stat("cpu  160 0 70 860 20 0 5 85 0 0\n")
    s = ops.cpu_shares(a, b)                     # interval of 200 jiffies: idle 60, iowait 10, steal 50
    assert s["steal"] == 25.0 and s["iowait"] == 5.0 and abs(s["busy"] - 65.0) < 1e-9
    assert ops.cpu_shares(a, a) is None and ops.parse_cpu_stat("nothing") is None
    psi = ops.parse_psi("some avg10=1.50 avg60=3.25 avg300=2.00 total=123\nfull avg10=0.00 avg60=0.50 avg300=0.10 total=45\n")
    assert psi == {"some": 3.25, "full": 0.5} and ops.parse_psi("") == {}


def test_short_cmd_and_top_cpu_skip_kernel_threads(monkeypatch):
    assert ops.short_cmd("/x/.venv/bin/python scripts/run_lock.py --config config/lock.toml") == "run_lock.py lock.toml"
    assert ops.short_cmd("/x/.venv/bin/python -u -m pmbot.arb_live data/arb-live --live") == "pmbot.arb_live"
    assert ops.short_cmd("/usr/bin/python3 /srv/bot/scripts/tg_bot.py") == "tg_bot.py"

    class R:
        stdout = ("12.0 120000 /x/.venv/bin/python scripts/run_bot.py --config config/g.toml\n"
                  " 9.5 3000 [kworker/0:1+events]\n"
                  " 6.1 62000 /x/.venv/bin/python scripts/run_lock.py --config config/lock.toml\n")
    monkeypatch.setattr(ops.subprocess, "run", lambda *a, **k: R())
    assert ops.top_cpu(5) == [(12.0, 117, "run_bot.py g.toml"), (6.1, 60, "run_lock.py lock.toml")]


def test_cpu_probe_and_the_report_survives_a_failing_diagnostic(monkeypatch):
    assert ops.cpu_probe(0.02) >= 0.9            # wall time can never be shorter than the CPU time it burned
    monkeypatch.setattr(ops, "load_report", lambda *a, **k: ["проверка 2 с: процессор занят 64 %, ждёт диск 1 %, украден гипервизором 22 %"])
    assert "украден гипервизором 22 %" in ops.format_server()
    def boom(*a, **k):
        raise RuntimeError("no /proc here")
    monkeypatch.setattr(ops, "load_report", boom)
    assert "CPU load" in ops.format_server() or "диск: свободно" in ops.format_server()


def test_instance_type_is_asked_only_on_ec2_and_marks_burstable(monkeypatch):
    import urllib.request

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body.encode()

    def fake_urlopen(req, timeout=0):
        return Resp("TOKEN" if req.full_url.endswith("/api/token") else "t3.small\n")

    def never(*a, **k):
        raise AssertionError("the metadata address must not be asked outside EC2")
    monkeypatch.delenv("PMBOT_NO_IMDS", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", never)
    monkeypatch.setattr(ops, "_is_ec2", lambda: False)
    assert ops.instance_type() == ""                                   # not EC2: no network call at all
    monkeypatch.setattr(ops, "_is_ec2", lambda: True)
    monkeypatch.setenv("PMBOT_NO_IMDS", "1")
    assert ops.instance_type() == ""                                   # switched off
    monkeypatch.delenv("PMBOT_NO_IMDS")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ops.instance_type() == "t3.small"
    monkeypatch.setattr(ops, "cpu_probe", lambda *a, **k: 1.0)
    monkeypatch.setattr(ops, "top_cpu", lambda *a, **k: [])
    first = ops.load_report(sample_s=0.01)[0]
    assert first.startswith("тип сервера: t3.small") and "кредитами" in first
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    assert ops.instance_type() == "" and not any(x.startswith("тип сервера") for x in ops.load_report(sample_s=0.01))


def test_is_ec2_reads_dmi(monkeypatch, tmp_path):
    real_open = open

    def fake_open(path, *a, **k):
        if str(path).endswith("/sys_vendor"):
            return real_open(tmp_path / "sys_vendor", *a, **k)
        raise OSError("absent")
    (tmp_path / "sys_vendor").write_text("Amazon EC2\n")
    monkeypatch.setattr("builtins.open", fake_open)
    assert ops._is_ec2() is True
    (tmp_path / "sys_vendor").write_text("QEMU\n")
    assert ops._is_ec2() is False
