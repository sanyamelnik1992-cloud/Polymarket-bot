"""Nightly review (pmbot/review.py): paired comparison only on windows both bots covered, pre-registered verdicts."""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot import charts, ops, review  # noqa: E402

DAY0 = 1793923200          # 2026-11-06 00:00 UTC


def write_bot(root, unit, cfg, journal, settles, t0, t1):
    (root / "deploy" / f"{unit}.service").write_text(
        f"[Service]\nExecStart=__REPO__/.venv/bin/python scripts/run_bot.py --config config/{cfg}.toml\n")
    (root / "config" / f"{cfg}.toml").write_text(f'journal_dir = "data/{journal}"\n[risk]\nstate_file = "data/state/{cfg}.json"\n')
    run = root / "data" / journal / "20261106-000000"
    run.mkdir(parents=True)
    ev = [{"t": t0, "type": "start", "mode": "paper"}]
    ev += [{"t": s + 390, "type": "settle", "key": f"btc-{s}", "winner": "Up", "pnl": p} for s, p in settles]
    p = run / "events.jsonl"
    p.write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in ev) + "\n")
    os.utime(p, (t1, t1))


def make(tmp_path, monkeypatch, days, edge, c_start=None):
    root = tmp_path
    (root / "deploy").mkdir()
    (root / "config").mkdir()
    rnd = random.Random(1)
    starts = [DAY0 + d * 86400 + i * 300 for d in range(days) for i in range(0, 288, 4)]
    a = [(s, rnd.gauss(0, 2)) for s in starts]
    c = [(s, p + edge + rnd.gauss(0, 0.5)) for s, p in a]
    end = DAY0 + days * 86400 + 600
    write_bot(root, "pmbot-5m", "a", "ja", a, DAY0 - 60, end)
    write_bot(root, "pmbot-5m-c", "c", "jc", c, c_start or DAY0 - 60, end)
    monkeypatch.setattr(ops, "REPO", str(root))
    monkeypatch.setattr(ops, "BOT_UNITS", ["pmbot-5m", "pmbot-5m-c"])
    monkeypatch.setattr(ops, "unit_state", lambda u: ("active", 0))
    monkeypatch.setattr(ops, "events_since", lambda j, since: [e for d in ops.run_dirs(j) for e in ops.read_events(d, since)])
    monkeypatch.setattr(ops, "run_dirs", lambda j, max_age_s=None: [str(root / j / "20261106-000000")])
    return root


def test_consistently_better_variant_is_promoted_after_three_days(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch, days=3, edge=0.5)
    st = review.pair_stats("pmbot-5m-c", "pmbot-5m", DAY0, DAY0 + 3 * 86400)
    assert st["windows"] == 3 * 72 and len(st["days"]) == 3 and st["better_days"] == 3
    assert st["t"] > review.PROMOTE_T and st["verdict"] == "promote"


def test_one_good_day_is_too_early_and_a_worse_variant_is_harmful(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch, days=1, edge=0.5)
    assert review.pair_stats("pmbot-5m-c", "pmbot-5m", DAY0, DAY0 + 86400)["verdict"] == "early"


def test_worse_variant_is_flagged_harmful(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch, days=2, edge=-0.5)
    assert review.pair_stats("pmbot-5m-c", "pmbot-5m", DAY0, DAY0 + 2 * 86400)["verdict"] == "harmful"


def test_windows_before_the_variant_started_are_not_compared(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch, days=1, edge=0.5, c_start=DAY0 + 12 * 3600)
    st = review.pair_stats("pmbot-5m-c", "pmbot-5m", DAY0, DAY0 + 86400)
    assert st["windows"] == 36           # only the second half of the day


def test_build_and_chart(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch, days=3, edge=0.5)
    tg, md, pairs = review.build("2026-11-08", since=DAY0)
    assert "Ночной разбор · 2026-11-08" in tg and "надёжно лучше" in tg and "Правило переноса" in tg
    assert "promote" in md
    png = charts.equity_png(days=3650)
    assert png and png[:4] == b"\x89PNG"
