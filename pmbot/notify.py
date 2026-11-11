"""Human-friendly Telegram messages (HTML parse mode, Russian) for the control bot and the trading bots' alerts.

Everything here only formats: data comes from pmbot.ops (journals, risk state files, systemd). Every value that is
not ours (reasons, errors, keys) goes through esc() so it cannot break the HTML.
"""
import html
import os
import time
from datetime import datetime, timezone

from . import ops

# unit -> (icon, letter, short name). Paired bots share a colour: C is A + strict filter, D is B + strict filter.
BOTS = {
    "pmbot-5m": ("🔷", "A", "основной (модель, 4¢)"),
    "pmbot-5m-b": ("🔶", "B", "1¢ от середины"),
    "pmbot-5m-c": ("🔹", "C", "A + строгий фильтр"),
    "pmbot-5m-d": ("🔸", "D", "B + строгий фильтр"),
    "pmbot-5m-e": ("🧪", "E", "paper-близнец живого"),
    "pmbot-5m-f": ("🧬", "F", "v3: у лучшей цены + дисбаланс очереди"),
    "pmbot-5m-g": ("🥇", "G", "v3 + первыми в очереди, без фильтров модели"),
    "pmbot-5m-g2": ("⭐", "G2", "G + заявки только на фаворита (paper)"),
    "pmbot-5m-h": ("🛡", "H", "v3 + строгий фильтр дисбаланса 0.50/0.60"),
    "pmbot-5m-live": ("💰", "LIVE", "реальные деньги на копейки (v3, BTC, 5 акций, стоп −$2/день)"),
    "pmbot-lock": ("🎯", "L", "TWAP-lock: решённая сторона в последнюю минуту, только p ≥ 0.99, цена 0.90–0.98 (paper)"),
    "pmbot-lock-wide": ("🎲", "L2", "TWAP-lock + спорные окна 0.50–0.98 (paper, эксперимент)"),
    "pmbot-lock-15": ("⏳", "L15", "TWAP-lock на 15-минутных рынках, те же фильтры (paper)"),
    "pmbot-lock-maker": ("🪤", "LM", "TWAP-lock мейкером: заявка на решённую сторону ≤0.98 (paper)"),
    "pmbot-lock-tier-wide": ("🧪", "Ступени без фильтров · L4", "старое правило без защит + цены от 0.50: для сравнения (paper)"),
    "pmbot-lock-tier": ("🪜", "Ступени · L3", "то же правило, что на реальных деньгах, без денег (paper-близнец)"),
    "pmbot-lock-maker2": ("🧲", "LM2", "LM с заявкой ≤0.96 и строгим фильтром Binance (paper)"),
    "pmbot-lock-maker3": ("🧲", "Мейкер · LM3", "та же сторона, но заявкой в стакан и без комиссии (paper)"),
    "pmbot-lock-tier-pb": ("⚡", "Ступени на быстрых ценах · L3-PB", "правило «Ступени» с новым потоком цен биржи, на 0.25 с раньше (paper)"),
    "pmbot-lock-tier-late": ("🌙", "Ступени + поздняя нижняя · L3-late", "«Ступени» плюс дешёвые входы (до 0.91) только с 260-й секунды: проверка, возвращать ли их (paper)"),
    "pmbot-lock-15-tier": ("⏳", "Ступени 15 мин · L3-15", "правило «Ступени» на 15-минутных рынках (paper)"),
    "pmbot-lock-99": ("🎯", "Снайпер · L99", "то же правило, что на реальных деньгах, без денег (paper-близнец)"),
    "pmbot-lock-99-live": ("🎯", "Снайпер · L99", "почти решённая сторона по 0.96–0.99, зона конкурента: +5¢ со сделки, минус −$4.95 (5 акций, один минус = стоп на день)"),
    "pmbot-lock-live": ("🪜", "Ступени · L3", "решённая сторона по 0.90–0.98, уверенность от 95%: чем она меньше, тем дешевле вход (10 акций от 97%, иначе 5; страховка при развороте; стоп −$10/день)"),
    "pmbot-lock-15-live": ("⏳", "Ступени 15 мин · L3-15", "правило «Ступени» на 15-минутных рынках: 4 ступени, уверенность от 90% (5 акций, стоп −$5/день)"),
    "pmbot-rewards": ("🎁", "Награды · R", "заявки рядом с ценой на рынках с пулами наград, после исполнения сразу хедж (paper)"),
    "pmbot-arb": ("⚖️", "ARB", "арбитраж 5m/15m на копейки (только гарантированные комплекты)"),
    "pmbot-hourly": ("⏱", "H", "часовой тейкер"),
    "pmbot-recorder": ("📼", "REC", "рекордер"),
    "pmbot-tg": ("🤖", "TG", "телеграм-бот"),
}
PAIRS = [("pmbot-5m-c", "pmbot-5m"), ("pmbot-5m-d", "pmbot-5m-b")]   # (variant, its control)
# Paper bots that run exactly the rule of a live bot (shown right under "real money" so the two can be compared at a glance)
TWINS = {"pmbot-lock-tier": "pmbot-lock-live", "pmbot-lock-99": "pmbot-lock-99-live", "pmbot-lock-15-tier": "pmbot-lock-15-live",
         "pmbot-5m-e": "pmbot-5m-live"}
GROUPS = [("live", "💰 <b>РЕАЛЬНЫЕ ДЕНЬГИ</b>"), ("twin", "📄 <b>БУМАГА · близнецы живых</b> (то же правило, без денег)"),
          ("lab", "🧪 <b>БУМАГА · эксперименты</b>")]


def group_of(unit, mode):
    if mode == "live":
        return "live"
    return "twin" if unit in TWINS else "lab"
LINE = "━━━━━━━━━━━━━━━"


def esc(x):
    return html.escape(str(x), quote=False)


def label(unit, full=True):
    icon, letter, name = BOTS.get(unit, ("•", unit, ""))
    return f"{icon} <b>{letter}</b>" + (f" · {esc(name)}" if full and name else "")


def money(x, bold=False):
    if x is None:
        return "—"
    s = f"{x:+.2f}$".replace("-", "−")
    return f"<b>{s}</b>" if bold else s


def dot(x):
    if x is None or abs(x) < 0.005:
        return "⚪"
    return "🟢" if x > 0 else "🔴"


def squares(values):
    """Last days as coloured squares: 🟩 plus, 🟥 minus, ⬜ zero."""
    return "".join("🟩" if v > 0.005 else "🟥" if v < -0.005 else "⬜" for v in values)


def hhmm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def window_name(key, length_s=300):
    """'btc-1793988600' -> 'BTC 18:10–18:15'."""
    try:
        asset, start = str(key).rsplit("-", 1)
        start = int(start)
        if start < 1_000_000_000:
            raise ValueError
    except ValueError:
        return esc(key)
    return f"{esc(asset.upper())} {hhmm(start)}–{hhmm(start + length_s)}"


def unit_for_config(config_path):
    """Which deploy/<unit>.service runs this config (to label a trading bot's own alerts)."""
    want = os.path.normpath(str(config_path or ""))
    for unit in ops.BOT_UNITS + ops.REWARD_UNITS:
        cfg = ops.unit_config(unit)
        if cfg and os.path.normpath(cfg) == want:
            return unit
    return None


def paper_note(units):
    modes = {ops.bot_paths(u)[2] for u in units}
    if "live" in modes:
        return "💰 есть LIVE-бот: это реальные деньги"
    return "📄 всё в paper — реальные деньги не участвуют"


# ---------------------------------------------------------------- alerts from the trading bots
KILL_RU = [("daily loss limit", "достигнут дневной лимит убытка"), ("kill file", "создан файл STOP (/stop)")]
ERROR_RU = {
    "order_rejected": "биржа отклонила заявку",
    "cancel_failed": "не получилось снять заявку",
    "api_error": "ошибка API биржи (пауза в котировании на несколько секунд)",
    "reset_failed": "не получилось снять все заявки после ошибки (повторяю)",
    "broker_fatal": "⛔ биржа отказала в доступе (401/403): бот остановлен",
    "heartbeat_error": "ошибка heartbeat (биржа снимет заявки через 10 с тишины)",
    "user_stream_error": "оборвался поток исполнений",
    "settle_timeout": "окно не рассчиталось за 30 мин",
    "order_failed": "заявка: биржа не ответила или ответила ошибкой, исход неизвестен (в этом окне больше не торгую)",
    "order_pending": "заявка принята, но ещё не исполнена (в этом окне больше не торгую, проверьте кошелёк)",
}


def _who(unit, prefix):
    return label(unit) if unit else f"<b>{esc(prefix)}</b>"


def event_message(kind, rec, unit=None, prefix="pmbot", mode="paper", per_window=False):
    """Text for one journal event, or None if this event should not be sent."""
    who = _who(unit, prefix)
    live = mode == "live" or rec.get("mode") == "live"
    length = 3600 if unit == "pmbot-hourly" else 300
    if kind == "settle":
        if not (live or per_window):
            return None                       # paper windows go into the hourly digest instead of 30 messages/hour
        pnl = rec.get("pnl", 0) or 0
        win = rec.get("winner")
        win = {"U": "Up", "D": "Down"}.get(win, win)
        if "up" in rec:
            pos = f"Up {rec.get('up', 0):.0f} · Down {rec.get('down', 0):.0f}"
        else:
            pos = f"{esc({'U': 'Up', 'D': 'Down'}.get(rec.get('side'), rec.get('side')))} {rec.get('shares', 0):.0f}"
        return (f"{'✅' if pnl > 0 else '❌' if pnl < 0 else '➖'} {label(unit, False) if unit else who} · "
                f"{window_name(rec.get('key'), length)} → <b>{esc(win)}</b>\n"
                f"{pos} · вложено {rec.get('cost', 0):.2f}$\n"
                f"Итог: {money(pnl, True)}" + (" 💰" if live else " (paper)"))
    if kind == "start":
        cfg = ops.unit_config(unit) if unit else None
        m = rec.get("mode", mode)
        return (f"▶️ {who} запущен\n"
                f"режим: {'<b>LIVE 💰 реальные деньги</b>' if m == 'live' else 'paper (без денег)'}"
                + (f"\nконфиг: <code>{esc(cfg)}</code>" if cfg else ""))
    if kind == "kill":
        reason = str(rec.get("reason", ""))
        ru = next((r for k, r in KILL_RU if k in reason), reason)
        if "kill file" in reason:
            how = "Вернуть: /resume"
        elif "награды" in reason:
            how = "Котировки включатся сами в 00:00 UTC, /resume не нужен."
        elif "daily loss" in reason:
            how = "Сегодня бот больше не торгует. Новый день начнётся в 00:00 UTC (тогда /resume)."
        else:
            how = "Посмотреть: /status, /log"
        return f"⛔ {who} — <b>торговля остановлена</b>\nПричина: {esc(ru)}\nВсе заявки сняты. {how}"
    if kind == "pause":
        until = rec.get("until")
        return (f"⏸ {who} — пауза" + (f" до <b>{hhmm(until)} UTC</b>" if until else "") + "\n"
                f"{rec.get('losing_streak', '?')} убыточных окон подряд. Это защита: заявки сняты, "
                "бот продолжит сам.")
    if kind == "final":
        unres = rec.get("unresolved", 0)
        return (f"🏁 {who} — итог запуска: {money(rec.get('pnl'), True)}"
                + (f" (ребейты {rec.get('rebates', 0):.2f}$)" if rec.get("rebates") else "")
                + (f"\n⚠️ не рассчитано окон: {unres}" if unres else ""))
    if kind in ERROR_RU:
        details = {k: v for k, v in rec.items() if k not in ("t", "type", "mode", "token")}
        where = window_name(details.pop("key")) if "key" in details else ""
        body = ", ".join(f"{k}={v}" for k, v in details.items())[:400]
        return f"⚠️ {who}: {ERROR_RU[kind]}" + (f" ({where})" if where else "") + (f"\n<code>{esc(body)}</code>" if body else "")
    if kind == "lock_hedge":                  # reports/64: the insurance on a reversal of the lock bot
        side = {"U": "Up", "D": "Down"}.get(rec.get("side"), rec.get("side"))
        return (f"🛡 {label(unit, False) if unit else who} · {window_name(rec.get('key'), length)}: страховка — модель развернулась "
                f"({100 * (rec.get('p') or 0):.0f}% за {esc(side)}), куплено {esc(side)} {rec.get('shares', 0):.0f} по "
                f"{rec.get('price', 0):.2f}\nИтог окна теперь {money(rec.get('worst'), True)}"
                + (f" … {money(rec.get('best'), True)}" if abs((rec.get("best") or 0) - (rec.get("worst") or 0)) > 0.05 else "")
                + (" 💰" if live else " (paper)"))
    if kind == "geoblock_ok":
        return f"🌍 {who}: гео-проверка пройдена ({esc(rec.get('country'))} {esc(rec.get('region') or '')})".rstrip()
    if kind == "balance":
        return f"💵 {who}: баланс кошелька <b>{rec.get('pusd', 0):.2f}$</b>"
    if kind == "existing_position":
        return (f"📦 {who}: на кошельке уже есть позиция {window_name(rec.get('key'))} "
                f"{esc(rec.get('side'))} {rec.get('shares', 0):.0f} @ {rec.get('avg_price', 0):.2f} — учтена в лимитах")
    return None


# ---------------------------------------------------------------- period statistics
def _settles(unit, t0, t1=None):
    journal = ops.bot_paths(unit)[0]
    if not journal:
        return [], []
    ev = [e for e in ops.events_since(journal, t0) if t1 is None or e["t"] < t1]
    return [e for e in ev if e["type"] == "settle"], [e for e in ev if e["type"] == "fill"]


def _stats(settles):
    pnls = [e.get("pnl", 0) or 0 for e in settles]
    return {"n": len(pnls), "pnl": sum(pnls), "wins": sum(p > 0 for p in pnls), "losses": sum(p < 0 for p in pnls),
            "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None}


def _by_day(unit):
    return ops.risk_state(ops.bot_paths(unit)[1]).get("realized_by_day", {})


def _shown_units():
    """Bot units worth showing: running now, or with any PnL history."""
    out = []
    for u in ops.BOT_UNITS:
        st = ops.unit_state(u)[0]
        if st in ("active", "activating", "failed") or _by_day(u):
            out.append(u)
    return out


def pairs_block(day):
    rows = []
    for var, ctl in PAIRS:
        bv, bc = _by_day(var), _by_day(ctl)
        if day not in bv or day not in bc:      # compare only days both bots traded
            continue
        d = bv.get(day, 0.0) - bc.get(day, 0.0)
        verdict = "строгий лучше" if d > 0.005 else "строгий хуже" if d < -0.005 else "поровну"
        rows.append(f"{label(var, False)} vs {label(ctl, False)}: {money(d)} — {verdict}")
    if not rows:
        return ""
    return "⚖️ <b>Строгий фильтр против контроля</b> (за сегодня)\n" + "\n".join(rows)


def format_digest(since, hours=6):
    units = _shown_units()
    if not units:
        return None
    now = time.time()
    lines = [f"🕐 <b>Сводка за {hours:g} ч</b> · {hhmm(since)}–{hhmm(now)} UTC", ""]
    groups = {g: [] for g, _ in GROUPS}
    for u in units:
        groups[group_of(u, ops.bot_paths(u)[2])].append(u)
    for g, title in GROUPS:
        if not groups[g]:
            continue
        lines.append(title)
        tot = 0.0
        for u in groups[g]:
            settles, fills = _settles(u, since)
            s = _stats(settles)
            today = _by_day(u).get(ops.today_utc())
            state = ops.unit_state(u)[0]
            head = f"{label(u, False)}"
            if state != "active":
                head += f"  {ops.STATE_RU.get(state, esc(state))}"
            if s["n"]:
                lines.append(f"{head}: {dot(s['pnl'])} {money(s['pnl'], True)} · окон {s['n']} (✅{s['wins']} ❌{s['losses']})"
                             f" · сегодня {money(today)}")
            else:
                lines.append(f"{head}: ⚪ сделок не было · сегодня {money(today)}")
            tot += s["pnl"]
        what = "реальные деньги" if g == "live" else "бумага"
        day = sum(_by_day(u).get(ops.today_utc(), 0) for u in groups[g])
        lines += [f"   Σ {what}: за период {money(tot, True)} · сегодня {money(day, True)}", ""]
    pb = pairs_block(ops.today_utc())
    if pb:
        lines += [pb, ""]
    lines += [paper_note(units)]
    return "\n".join(lines)


# ---------------------------------------------------------------- commands
def _status_block(s):
    """One bot of /status: name, what it does, state, today, total."""
    unit = s["unit"]
    out = [label(unit)]
    line = f"   {ops.STATE_RU.get(s['state'], esc(s['state']))} {ops.fmt_age(s['since'])}".rstrip()
    out.append(line)
    if s["settles"]:
        out.append(f"   {dot(s['pnl_today'])} сегодня {money(s['pnl_today'], True)} · сделок {len(s['settles'])} "
                   f"(✅{s['wins']} ❌{s['losses']})")
    else:
        out.append(f"   {dot(s['pnl_today'])} сегодня {money(s['pnl_today'], True)} · сделок пока нет")
    extra = []
    if s["pnl_all"] is not None:
        extra.append(f"всего {money(s['pnl_all'])} за {len(s['pnl_by_day'])} дн.")
    if s["open_worst"]:
        extra.append(f"сейчас в сделке {s['open_worst']:.2f}$")
    if extra:
        out.append("   " + " · ".join(extra))
    if s["killed"]:
        out.append(f"   ⛔ остановлен до ручного запуска: {esc(s['kill_reason'])}")
    return out


def _rewards_block(unit):
    """The liquidity-rewards farmer in /status. Its income is an ESTIMATE of Polymarket's payout, so it is shown apart
    from the bots' totals: the estimate by the book, the cautious figure, and what the fills cost."""
    r = ops.rewards_summary(unit)
    if not r:
        return []
    out = [f"{label(unit)}", f"   {ops.STATE_RU.get(r['state'], esc(r['state']))} {ops.fmt_age(r['since'])} · бумага"]
    st = r["status"]
    if not st:
        return out + ["   статуса ещё нет"]
    out.append(f"   🎁 награды, оценка: сегодня {money(st['rewards_today'], True)} · всего {money(st['rewards_total'])}")
    out.append(f"   из них на рынках, где мы одни: {money(st.get('alone_total'))} (самая ненадёжная часть)")
    out.append(f"   {dot(st['trading_pnl'])} потери на исполнениях: {money(st['trading_pnl'])} · исполнений сегодня "
               f"{st['fills_today']} · хеджей {st.get('hedges_today', 0)}")
    out.append(f"   итог осторожно: {money(st.get('cautious_pnl'), True)} · рынков {st['markets']} "
               f"(котирует {st['quoting']})")
    if st.get("halted"):
        out.append("   ⛔ котировки выключены до 00:00 UTC (дневной убыток)")
    return out


def format_status():
    """/status: real money first, then the paper twins of the live bots, then paper experiments. The totals of real
    money and paper are never added together (the owner read a paper +$20 as his own on 10 Nov)."""
    lines = ["📊 <b>Боты · 5-минутные рынки</b>"]
    w = ops.wallet_line()
    if w:
        lines.append(w)
    lines.append("")
    groups = {g: [] for g, _ in GROUPS}
    for unit in ops.BOT_UNITS:
        s = ops.bot_summary(unit)
        if s["state"] in ("inactive", "unknown", "n/a") and not s["settles"] and s["pnl_today"] in (None, 0):
            continue
        groups[group_of(unit, s["mode"])].append(s)
    shown = 0
    for g, title in GROUPS:
        if not groups[g]:
            continue
        lines.append(title)
        for s in groups[g]:
            shown += 1
            lines += _status_block(s)
        tot = sum(s["pnl_today"] or 0 for s in groups[g])
        what = "реальные деньги" if g == "live" else "бумага"
        lines += [f"   Σ {what} сегодня: {money(tot, True)}", ""]
    for unit in ops.REWARD_UNITS:
        block = _rewards_block(unit)
        if block:
            shown += 1
            lines += block + [""]
    if not shown:
        lines.append("ни один бот не запущен\n")
    st, since = ops.unit_state("pmbot-recorder")
    if st not in ("inactive", "unknown", "n/a"):
        lines.append(f"{label('pmbot-recorder')}: {ops.STATE_RU.get(st, esc(st))} {ops.fmt_age(since)}")
    if os.path.exists(ops.KILL_FILE):
        lines.append("⛔ <b>файл STOP есть</b>: торговля остановлена (/resume — снять)")
    g = ops.git_info()
    lines += [LINE, f"код: <code>{esc(g['branch'])} {esc(g['commit'])}</code>"]
    return "\n".join(lines)


def format_trades(n=10):
    rows = []
    for unit in ops.BOT_UNITS:
        journal = ops.bot_paths(unit)[0]
        if not journal:
            continue
        for e in ops.events_since(journal, time.time() - 3 * 86400):
            if e["type"] == "settle":
                rows.append((e["t"], unit, e))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return "сделок пока нет"
    out = [f"🧾 <b>Последние {min(n, len(rows))} окон</b>", ""]
    for _, unit, e in rows[-n:]:
        pnl = e.get("pnl", 0) or 0
        win = {"U": "Up", "D": "Down"}.get(e.get("winner"), e.get("winner"))
        pos = f"U{e.get('up', 0):.0f}/D{e.get('down', 0):.0f}" if "up" in e else esc(e.get("side", ""))
        length = 3600 if unit == "pmbot-hourly" else 300
        out.append(f"{dot(pnl)} {label(unit, False)} {window_name(e.get('key'), length)} → {esc(win)} "
                   f"{pos} {money(pnl, True)}")
    return "\n".join(out)


def format_server():
    return "🖥 <b>Сервер</b>\n" + esc(ops.format_server())
