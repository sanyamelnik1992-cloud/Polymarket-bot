"""Nightly review: how every bot did on a UTC day, where it lost money, and a pre-registered verdict on each
experiment pair (strict variant vs its control).

Pairs are compared window by window, only on windows when BOTH bots were running (their journal runs cover the
whole window), so a bot that started later is not credited or blamed for the other's early trades.

The promotion rule is fixed here in advance, so one lucky night cannot talk us into a change:
  promote  : >= PROMOTE_MIN_DAYS days with data, paired t >= PROMOTE_T, variant better on >= PROMOTE_DAY_SHARE of days
  harmful  : paired t <= -PROMOTE_T
  otherwise: too early / no difference
The verdict is a recommendation: changing the main bot stays the owner's decision.
"""
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import ops
from .notify import PAIRS, dot, esc, label, money

PROMOTE_MIN_DAYS = 3
PROMOTE_T = 2.0
PROMOTE_DAY_SHARE = 2 / 3
MIN_WINDOWS_PER_DAY = 10      # a day counts for the rule only with at least this many paired windows
WINDOW_S = 300
RULE_TEXT = (f"Правило переноса: ≥{PROMOTE_MIN_DAYS} дн. с данными, t ≥ {PROMOTE_T:.0f}, "
             f"строгий лучше в ≥{PROMOTE_DAY_SHARE * 100:.0f}% дней.")


def window_start(key):
    try:
        return int(str(key).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def coverage(unit, since):
    """[(t0, t1)] time spans the bot's journal runs were alive (first event .. last write)."""
    journal = ops.bot_paths(unit)[0]
    spans = []
    for d in ops.run_dirs(journal, max_age_s=time.time() - since + 86400) if journal else []:
        p = os.path.join(d, "events.jsonl")
        try:
            with open(p) as f:
                t0 = json.loads(f.readline())["t"]
            spans.append((t0, os.path.getmtime(p)))
        except (OSError, ValueError, KeyError):
            continue
    return spans


def covered(spans, start):
    return any(t0 <= start and start + WINDOW_S <= t1 for t0, t1 in spans)


def window_pnls(unit, since):
    """{window key: realised pnl} for windows that started at/after `since`."""
    journal = ops.bot_paths(unit)[0]
    out = defaultdict(float)
    for e in ops.events_since(journal, since) if journal else []:
        if e["type"] == "settle":
            s = window_start(e.get("key"))
            if s is not None and s >= since:
                out[e["key"]] += e.get("pnl", 0) or 0
    return dict(out)


def tstat(xs):
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / sd * math.sqrt(n) if sd > 0 else float("nan")


def pair_stats(var, ctl, since, until=None):
    until = until or time.time()
    pv, pc = window_pnls(var, since), window_pnls(ctl, since)
    cv, cc = coverage(var, since), coverage(ctl, since)
    diffs, per_day = [], defaultdict(list)
    for k in set(pv) | set(pc):
        s = window_start(k)
        if s is None or s + WINDOW_S > until or not (covered(cv, s) and covered(cc, s)):
            continue
        d = pv.get(k, 0.0) - pc.get(k, 0.0)
        diffs.append(d)
        per_day[utc_day(s)].append(d)
    days = {d: sum(v) for d, v in per_day.items() if len(v) >= MIN_WINDOWS_PER_DAY}
    better = sum(1 for v in days.values() if v > 0)
    t = tstat(diffs)
    st = {"var": var, "ctl": ctl, "windows": len(diffs), "diff": sum(diffs), "t": t, "days": days,
          "better_days": better}
    st["verdict"], st["verdict_text"] = verdict(st)
    return st


def verdict(st):
    n, t = len(st["days"]), st["t"]
    if not math.isnan(t) and t <= -PROMOTE_T:
        return "harmful", "❌ строгий фильтр <b>вредит</b> — предлагаю выключить эксперимент"
    if n < PROMOTE_MIN_DAYS:
        return "early", f"⏳ рано: {n} из {PROMOTE_MIN_DAYS} дн. с данными"
    if not math.isnan(t) and t >= PROMOTE_T and st["better_days"] >= PROMOTE_DAY_SHARE * n:
        return "promote", "✅ строгий <b>надёжно лучше</b> — предлагаю перенести фильтр в контрольного бота"
    return "nodiff", "🤷 устойчивой разницы пока нет — продолжаем"


def bot_day(unit, day):
    """Settles and fill breakdown of one bot for the windows that started on `day`."""
    from .fills import load
    t0 = ops.day_start_ts(day)
    journal = ops.bot_paths(unit)[0]
    pn = {k: v for k, v in window_pnls(unit, t0).items() if window_start(k) < t0 + 86400}
    out = {"unit": unit, "windows": len(pn), "pnl": sum(pn.values()), "wins": sum(v > 0 for v in pn.values()),
           "fills": 0, "markout": None, "agree_pnl": None, "disagree_pnl": None, "fill_df": None}
    if not journal or not pn:
        return out
    import pandas as pd
    frames = []
    for d in ops.run_dirs(journal, max_age_s=time.time() - t0 + 86400):
        try:
            f = load(d)
        except OSError:
            continue
        if len(f):
            frames.append(f[f.key.isin(pn.keys())])
    if frames:
        df = pd.concat(frames)
        if len(df):
            out.update(fills=len(df), markout=df.mk10.mean(), fill_df=df,
                       agree_pnl=df[df.dis <= 0.04].pnl.sum(), disagree_pnl=df[df.dis > 0.04].pnl.sum())
    return out


def _t(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.1f}"


def build(day, since=None):
    """(telegram_html, markdown) for the review of `day`; `since` = start of the pair comparison (default 30 days)."""
    since = since if since is not None else ops.day_start_ts(day) - 29 * 86400
    until = ops.day_start_ts(day) + 86400
    bots = [bot_day(u, day) for u in ops.BOT_UNITS]
    bots = [b for b in bots if b["windows"]]
    tg = [f"🔬 <b>Ночной разбор · {day}</b>", ""]
    md = [f"# Ночной разбор {day} (UTC)", "", "Сгенерировано `scripts/nightly_review.py` на сервере.", ""]
    if not bots:
        tg.append("За день закрытых окон нет.")
    for b in bots:
        wr = 100 * b["wins"] / b["windows"]
        tg.append(f"{label(b['unit'])}\n   {dot(b['pnl'])} {money(b['pnl'], True)} · окон {b['windows']} · "
                  f"выиграно {wr:.0f}% · исп. {b['fills']}")
        if b["markout"] is not None:
            tg.append(f"   через 10 с после исполнения цена против нас на {-b['markout'] * 100:.1f}¢"
                      if b["markout"] < 0 else
                      f"   через 10 с после исполнения цена за нас на {b['markout'] * 100:.1f}¢")
            tg.append(f"   расхождение модели ≤4¢: {money(b['agree_pnl'])} · >4¢: {money(b['disagree_pnl'])}")
        md += [f"## {b['unit']}", "", f"PnL {b['pnl']:+.2f}$, окон {b['windows']}, выиграно {wr:.0f}%, "
               f"исполнений {b['fills']}", ""]
        df = b.pop("fill_df")
        if df is not None:
            from .fills import table
            md += ["```", "по |fair − mid| в момент исполнения:",
                   table(df, "dis", [0, .02, .04, .06, .08, 1]).to_string(), "",
                   "по секунде окна:", table(df, "rel", [0, 30, 60, 120, 180, 240, 300]).to_string(), "",
                   "по цене:", table(df, "price", [0, .2, .35, .5, .65, .8, 1]).to_string(), "```", ""]
    pairs = [pair_stats(v, c, since, until) for v, c in PAIRS]
    pairs = [p for p in pairs if p["windows"]]
    if pairs:
        tg += ["", "⚖️ <b>Эксперименты</b> (окна, когда работали оба бота)"]
        md += ["## Эксперименты", ""]
        for p in pairs:
            nd = len(p["days"])
            line = (f"{label(p['var'], False)} vs {label(p['ctl'], False)}: {money(p['diff'], True)} · "
                    f"окон {p['windows']} · t={_t(p['t'])}" + (f" · лучше в {p['better_days']} из {nd} дн." if nd else ""))
            tg += [line, f"   {p['verdict_text']}"]
            md += [f"- {p['var']} vs {p['ctl']}: {p['diff']:+.2f}$ over {p['windows']} paired windows, t={_t(p['t'])}, "
                   f"days {json.dumps({d: round(v, 2) for d, v in sorted(p['days'].items())})} → **{p['verdict']}**"]
        tg += ["", f"<i>{esc(RULE_TEXT)}</i>"]
        md += ["", RULE_TEXT]
    return "\n".join(tg), "\n".join(md) + "\n", pairs
