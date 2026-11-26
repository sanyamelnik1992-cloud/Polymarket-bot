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
    "pmbot-lock-15-alt": ("🪙", "Ступени 15 мин · новые монеты", "то же правило 15 минут на DOGE, BNB, HYPE: проверка перед реальными деньгами (paper)"),
    "pmbot-lock-99": ("🎯", "Снайпер · L99", "то же правило, что на реальных деньгах, без денег (paper-близнец)"),
    "pmbot-lock-99-live": ("🎯", "Снайпер · 5 мин", "почти решённая сторона по 0.96–0.99 в последнюю минуту"),
    "pmbot-lock-99-sigma": ("🎯", "Снайпер · гибкая пауза", "правило Снайпера, но пауза после скачка Binance по волатильности: больше "
                            "входов? Экзамен: 300 входов (отчёт 88)"),
    "pmbot-maker": ("🏦", "Мейкер · 5 мин", "заявки на обе стороны BTC 5 мин чуть ниже справедливой цены по Binance "
                    "(paper; выключен 26 ноя: живой тест идёт по осторожному варианту, отчёт 94)"),
    "pmbot-maker-safe": ("🏦", "Мейкер осторожный · 5 мин", "то же, но заявка не выше середины стакана: проверка, кто прав, "
                         "модель или рынок (отчёт 89)"),
    "pmbot-maker-live": ("🏦", "Мейкер · живой тест", "заявки на обе стороны BTC 5 мин на минимуме биржи: сколько реально "
                         "исполняется и сколько платит биржа мейкеру (отчёт 94)"),
    "pmbot-lock-live": ("🪜", "Ступени · L3", "решённая сторона в последнюю минуту, только уверенность от 99% по цене до 0.98 (10 акций; страховка при развороте; стоп −$5/день)"),
    "pmbot-lock-15-live": ("⏳", "Ступени · 15 мин", "уверенная сторона (от 90%) в последние минуты 15-минутных окон"),
    "pmbot-rewards": ("🎁", "Награды · R", "заявки рядом с ценой на рынках с пулами наград, после исполнения сразу хедж (paper)"),
    "pmbot-weather-rec": ("🌡", "Погода · запись", "данные рынков температуры: сводки METAR, стаканы, прогнозы; без заявок"),
    "pmbot-feedrec": ("📡", "Гонка источников цены", "какой источник показывает движение раньше Binance (отчёт 87); без заявок"),
    "pmbot-rewards-live": ("🧪", "Награды · живой тест", "РЕАЛЬНЫЕ ДЕНЬГИ: один рынок, две заявки ≈$20, один час; читает долю и начисления с биржи"),
    "pmbot-arb": ("⚖️", "ARB", "арбитраж 5m/15m на копейки (только гарантированные комплекты)"),
    "pmbot-hourly": ("⏱", "H", "часовой тейкер"),
    "pmbot-lock-early": ("🌅", "Ранний снайпер 5 мин", "BTC, SOL, XRP: лидер за 3–1 минуты до конца, только по 0.985–0.99, отрыв ≥ 4 сигм за час (paper, отчёты 80–81)"),
    "pmbot-lock-15-early": ("🌄", "Ранний снайпер · 15 мин", "то же правило без денег (бумажный близнец живого)"),
    "pmbot-lock-fair": ("⚡", "Перехватчик · 5 мин", "закрыт 24 ноя: дешёвые цены забирают быстрее нас (отчёты 86, 87)"),
    "pmbot-lock-fair-live": ("⚡", "Перехватчик · 5 мин", "сторона, которая в стакане на 15¢+ дешевле цены по Binance (остановлен 23 ноя, отчёт 86)"),
    "pmbot-lock-fair-fut": ("⚡", "Перехватчик 5 мин · фьючерсы", "то же правило, но сигнал по фьючерсам Binance (они обычно двигаются раньше): сравнение с обычным (paper)"),
    "pmbot-lock-15-fair": ("⚡", "Перехватчик 15 мин", "то же на 15-минутках: за 14–1 минуты до конца (paper, отчёт 83)"),
    "pmbot-lock-15-early-live": ("🌄", "Ранний снайпер · 15 мин", "лидер за 5–2 мин до конца по 0.985–0.99, как у конкурента; 5 акций, стоп после проигрыша (отчёт 92)"),
    "pmbot-hourly-last": ("🕐", "Часовые · последняя минута", "часовые рынки BTC/ETH: вход в последнюю минуту, когда цена Binance далеко от открытия часа (paper)"),
    "pmbot-recorder": ("📼", "REC", "рекордер"),
    "pmbot-tg": ("🤖", "TG", "телеграм-бот"),
}
PAIRS = [("pmbot-5m-c", "pmbot-5m"), ("pmbot-5m-d", "pmbot-5m-b")]   # (variant, its control)
# Paper bots that run exactly the rule of a live bot (shown right under "real money" so the two can be compared at a glance)
TWINS = {"pmbot-lock-tier": "pmbot-lock-live", "pmbot-lock-99": "pmbot-lock-99-live", "pmbot-lock-15-tier": "pmbot-lock-15-live",
         "pmbot-lock-15-early": "pmbot-lock-15-early-live",
         "pmbot-lock-fair": "pmbot-lock-fair-live",
         "pmbot-maker-safe": "pmbot-maker-live",
         "pmbot-5m-e": "pmbot-5m-live"}
# Live units whose closed windows come as ONE summary an hour instead of a message each (the Перехватчик closes dozens of
# windows a day, half of them lost). 22 Nov the owner saw only the stop message and no trade at all (reports/83 section 13).
QUIET_SETTLES = {"pmbot-lock-fair-live", "pmbot-maker-live"}     # the maker: every 5-minute window (reports/94)
GROUPS = [("live", "💰 <b>РЕАЛЬНЫЕ ДЕНЬГИ</b>"), ("twin", "📄 <b>БУМАГА · близнецы живых</b> (то же правило, без денег)"),
          ("lab", "🧪 <b>БУМАГА · эксперименты</b>")]
# What /status and the digest list (reports/88, owner 23 Nov: "те стратегии от которых мы отказались не отображать, хорошо разделить
# то что уже работает на реальных деньгах и то что тестируем, если тест выключаем оно там не отображается ... пометь стратегии
# которые мы отложили или еще не доделали"): only units in these states; a stopped LIVE unit that closed windows today only adds
# to the day's real-money total, which stays complete.
RUNNING = ("active", "activating", "deactivating", "failed")
# Strategies that are shelved or being built (not running): the end of /status, one line each. Keep it short and current.
SHELF = [
    ("🛠", "Больше входов для «почти решённых»", "гибкая пауза Снайпера — на бумаге; Снайпер на нескольких монетах сразу и "
                                                   "новые монеты 15 мин — следующие (отчёт 88)"),
]


def _shares_word(n):
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        return "акция"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "акции"
    return "акций"


def limits(unit):
    """What one window of a live unit can cost, from its config: '10 акций · стоп −$9.5 в день', '2 акции (10 при 99%+) · ...'."""
    cfg = ops.unit_config(unit)
    c = ops.load_toml(cfg) if cfg else {}
    lk, rk = c.get("lock") or {}, c.get("risk") or {}
    parts = []
    sh = (c.get("maker") or {}).get("shares") or lk.get("shares")       # the maker's bids: [maker] shares
    if sh:
        text = f"{sh:g} {_shares_word(sh)}"
        big, big_p = lk.get("big_shares") or 0, lk.get("big_shares_p") or 0
        if big > sh and big_p:
            text += f" ({big:g} при {big_p * 100:.0f}%+)"
        parts.append(text)
    if rk.get("daily_loss_limit"):
        parts.append(f"стоп −${rk['daily_loss_limit']:g} в день")
    return " · ".join(parts)


def _state_mark(s):
    """'🟢 ' for a running bot; otherwise what is wrong with it, followed by ' · '."""
    if s.get("killed"):
        return "⛔ стоп на сегодня · "
    if s["state"] == "active":
        return "🟢 "
    return ops.STATE_RU.get(s["state"], esc(s["state"])) + " · "


def _kill_ru(reason):
    for en, ru in KILL_RU:
        if en in str(reason):
            return ru
    return str(reason)


def _card(s, live):
    """Two or three lines of one running bot in /status."""
    unit = s["unit"]
    icon, name, what = BOTS.get(unit, ("•", unit, ""))
    trades = f"✅{s['wins']} ❌{s['losses']}" if s["settles"] else "сделок пока нет"
    line = f"   {_state_mark(s)}сегодня {money(s['pnl_today'], True)} · {trades}"
    if s["pnl_all"] is not None:
        line += f" · всего {money(s['pnl_all'])} за {len(s['pnl_by_day'])} дн."
    out = [f"{icon} <b>{esc(name)}</b>", line]
    desc = what
    if live:
        lim = limits(unit)
        desc = f"{what} · {lim}" if lim else what
        if s["open_worst"]:
            desc += f" · сейчас в сделке {s['open_worst']:.2f}$"
    if desc:
        out.append(f"   <i>{esc(desc)}</i>")
    if s["killed"] and s.get("kill_reason"):
        out.append(f"   ⛔ {esc(_kill_ru(s['kill_reason']))}")
    return out


def quiet_summary(unit, q, total, minutes=60, mode="live"):
    """One message for the windows a QUIET_SETTLES unit closed in the last ~hour, with the total since its start."""
    tail = " 💰" if mode == "live" else " (paper)"
    return (f"{label(unit, False)} · за {minutes} мин: окон {q['n']} (+{q['won']}/−{q['n'] - q['won']}), итог {money(q['pnl'], True)}{tail}\n"
            f"с запуска: окон {total['n']} (+{total['won']}/−{total['n'] - total['won']}), итог {money(total['pnl'])}")


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
    for unit in ops.BOT_UNITS + ops.REWARD_UNITS + ops.REWARD_LIVE_UNITS:
        cfg = ops.unit_config(unit)
        if cfg and os.path.normpath(cfg) == want:
            return unit
    return None


# ---------------------------------------------------------------- alerts from the trading bots
KILL_RU = [("daily loss limit", "достигнут дневной лимит убытка"), ("kill file", "создан файл STOP (/stop)"),
           ("broker: max_orders_hour", "слишком много заявок за час (защита от зацикливания)"),
           ("broker:", "биржа отказала в доступе (401/403)")]
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
    "mm_live_error": "мейкер: ошибка при работе с заявками (подробности ниже)",
    "fill_failed": "мейкер: сделка не прошла на блокчейне, исполнение снято со счёта",
}


def _who(unit, prefix):
    return label(unit) if unit else f"<b>{esc(prefix)}</b>"


def event_message(kind, rec, unit=None, prefix="pmbot", mode="paper", per_window=False):
    """Text for one journal event, or None if this event should not be sent."""
    who = _who(unit, prefix)
    live = mode == "live" or rec.get("mode") == "live"
    length = 3600 if unit.startswith("pmbot-hourly") else 300
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
        elif "daily loss" in reason and (mode == "live" or rec.get("mode") == "live"):
            # 22 Nov: "(тогда /resume)" here led to a /resume that started four old paper bots and not this one (reports/83 s. 15)
            how = (f"Живой бот сам не включится. Запуск — на следующий день: /restart {esc(unit)}" if unit else
                   "Живой бот сам не включится: на следующий день — /restart.")
        elif "daily loss" in reason:
            how = "Бумажный бот продолжит сам в 00:00 UTC, ничего делать не нужно."
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
    if kind.startswith("rlive"):
        who = label(unit, False) if unit else who   # the short name: these messages are long enough
    if kind == "rlive_market":                # the live test of the rewards rule picked its market (reports/68)
        return (f"{who}: тест начался 💰\nРынок: <b>{esc(rec.get('question'))}</b>\n"
                f"Пул {rec.get('rate', 0):.0f}$ в день · {'в зоне наград мы одни' if rec.get('empty') else 'в зоне есть другие мейкеры'}\n"
                f"Ставлю две заявки по {rec.get('size', 0):.0f} акций, всего ≈{rec.get('capital', 0):.0f}$, на {rec.get('minutes', 0):.0f} мин.\n"
                f"При первом исполнении тест остановится. Цифры: /rlive")
    if kind == "rlive_no_market":
        return (f"{who}: подходящего рынка сейчас нет (вид «{esc(rec.get('test_kind'))}», просмотрено {rec.get('looked_at', 0)}). "
                f"Заявок не было. Запустить ещё раз позже: /restart pmbot-rewards-live")
    if kind == "rlive_fill":
        return (f"{who}: <b>заявку исполнили</b> 💰\n{esc(rec.get('question'))}\n"
                f"Куплено «{'Да' if rec.get('side') == 'Y' else 'Нет'}» {rec.get('size', 0):.0f} по {rec.get('price', 0):.2f} "
                f"= {rec.get('size', 0) * rec.get('price', 0):.2f}$\nТест остановлен, заявки сняты. Акции остались в кошельке.")
    if kind == "rlive_done":
        return rewards_live_done(rec, who)
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
    """The 6-hourly digest (reports/88): the period's result of the running bots, real money apart from the tests. A live unit
    stopped during the period is still listed when it closed windows in it (real money is never left out)."""
    live, paper = [], []
    today = ops.today_utc()
    day_live = 0.0
    for u in ops.BOT_UNITS:
        state = ops.unit_state(u)[0]
        mode = ops.bot_paths(u)[2]
        settles, _ = _settles(u, since)
        if mode == "live":
            day_live += _by_day(u).get(today, 0.0)
        if state not in RUNNING and not (mode == "live" and settles):
            continue
        (live if mode == "live" else paper).append((u, state, _stats(settles)))
    rewards = [u for u in ops.REWARD_UNITS if ops.unit_state(u)[0] in RUNNING]
    if not live and not paper and not rewards:
        return None
    now = time.time()
    lines = [f"🕐 <b>Сводка {hhmm(since)}–{hhmm(now)} UTC</b>"]

    def row(u, state, s):
        icon, name, _ = BOTS.get(u, ("•", u, ""))
        if s["n"]:
            text = f"{icon} {esc(name)}: {dot(s['pnl'])} {money(s['pnl'], True)} · окон {s['n']} (✅{s['wins']} ❌{s['losses']})"
        else:
            text = f"{icon} {esc(name)}: ⚪ сделок не было"
        if state not in ("active", "activating"):
            text += f" · {'⏹ остановлен' if state == 'inactive' else ops.STATE_RU.get(state, esc(state))}"
        return text

    if live:
        tot = sum(s["pnl"] for _, _, s in live)
        lines += ["", f"💰 <b>Реальные деньги</b>: {money(tot, True)} за {hours:g} ч · сегодня {money(day_live)}"]
        lines += [row(*x) for x in live]
    if paper:
        lines += ["", "🧪 <b>Тесты</b> · без денег"] + [row(*x) for x in paper]
    pb = pairs_block(today)
    if pb:
        lines += ["", pb]
    for u in rewards:
        rl = rewards_digest_line(u)
        if rl:
            lines += ["", rl]
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


REWARDS_HOW = """🎁 <b>Как работает бот «Награды» (R)</b>

Polymarket каждый день платит тем, кто держит заявки на покупку и продажу рядом с ценой. На каждый рынок назначена
сумма в день, её делят между всеми, чьи заявки стоят достаточно близко к цене.

1️⃣ Раз в 10 минут бот выбирает до 45 рынков, где его маленькая заявка получит самую большую долю этой суммы.
Рынки, где уже стоит много других, пропускает: там чаще исполняют и меньше платят.
2️⃣ В каждом ставит две заявки по ~20 акций: купить «Да» чуть ниже цены и купить «Нет» чуть выше.
Пока они стоят, капает награда.
3️⃣ Иногда кто-то торгует против нашей заявки — это <b>исполнение</b>. У нас остаётся одна сторона. Бот сразу
докупает вторую: пара «Да» + «Нет» всегда стоит ровно $1, позиция закрыта с потерей в несколько центов на акцию.
4️⃣ Если докупить дорого — бот держит позицию и ставит одну заявку, которая её закроет. Новых заявок в этом рынке
не ставит, пока не закроет.
5️⃣ <b>Заработок = награды − потери на исполнениях.</b>
6️⃣ Стоп: если итог дня (потери плюс надёжная часть наград) хуже −$50, бот выключается до 00:00 UTC и потом
включается сам.

<b>Что значат цифры</b>
💰 <b>награды</b> — расчёт по формуле биржи. Бот бумажный: денег нет, это оценка.
🏝 <b>где мы одни</b> — награды с рынков, где кроме нас никто не стоит. Самая ненадёжная часть: заплатит ли биржа
за неё — проверит только живой тест.
💸 <b>потери</b> — выходы из исполненных заявок и позиции, которые ещё не закрыты (по текущей цене).
🛡 <b>осторожный итог</b> — награды без «где мы одни» минус потери.
✅ <b>честный итог</b> — награды только с тех минут, когда другие мейкеры держали хотя бы половину пула, минус ВСЕ
потери. Живые замеры 14–15 ноября: за пустой и почти пустой пул биржа платит в разы меньше расчёта, и такие заявки
забирают первыми. Только честный итог можно сравнивать с реальными деньгами.

Цифры подробно: /r"""


def _rewards_parts(st):
    """(rewards, alone, losses, total, cautious) of today from an rstatus record; older records lack some keys."""
    rew = st.get("rewards_today") or 0.0
    alone = st.get("alone_today") or 0.0
    loss = st.get("trading_pnl_today") or 0.0
    cautious = st.get("cautious_today")
    if cautious is None:
        low = min(rew, st.get("official_today", rew)) - alone
        cautious = max(low, 0.0) + loss
    return rew, alone, loss, rew + loss, cautious


def _rewards_block(unit):
    """The liquidity-rewards farmer in /status: what it earned today, what it lost, the result. Its income is an
    ESTIMATE of Polymarket's payout, so it is shown apart from the bots' totals."""
    r = ops.rewards_summary(unit)
    if not r:
        return []
    out = [f"🎁 <b>Награды · R</b> · бумага · {ops.STATE_RU.get(r['state'], esc(r['state']))} {ops.fmt_age(r['since'])}"]
    st = r["status"]
    if not st:
        return out + ["   статуса ещё нет"]
    rew, alone, loss, total, cautious = _rewards_parts(st)
    out.append(f"   💰 заработал сегодня (расчёт): {money(rew, True)}  · 🏝 из них где мы одни {money(alone)}")
    out.append(f"   💸 потерял на исполнениях: {money(loss, True)}  · исполнений {st.get('fills_today', 0)}")
    out.append(f"   {dot(total)} итог дня по расчёту: {money(total, True)}  · 🛡 осторожно {money(cautious, True)}")
    if "honest_today" in st:
        out.append(f"   ✅ <b>честный итог дня: {money(st['honest_today'], True)}</b> (награды только там, где другие держат "
                   f"половину пула: {money(st.get('shared_today'))})")
    if st.get("halted"):
        out.append("   ⛔ стоп до 00:00 UTC: итог дня хуже −$50, потом включится сам")
    out.append("   подробно: /r · как работает: /rhow")
    return out


WHY_RU = {"time": "время вышло", "fill": "заявку исполнили", "signal": "остановлен вручную", "kill_file": "STOP",
          "no_market": "не нашлось рынка", "broker_fatal": "биржа отказала в доступе",
          "order_outcome_unknown": "биржа не ответила на заявку", "day_stop": "дневной стоп", "stopped": "остановлен"}


def _usd(x):
    return "нет данных" if x is None else f"{x:.2f}$"


def rewards_live_done(d, who="🧪 <b>Награды · живой тест</b>"):
    """The end of one live rewards test: what the exchange accrued and showed against the paper estimate."""
    pct, top, last, paper = d.get("exch_pct_mean"), d.get("exch_pct_max"), d.get("exch_pct_last"), d.get("paper_share_mean")
    sc, got, est = d.get("scoring_all"), d.get("exch_earned"), d.get("paper_reward")
    sq = d.get("squeezed_after_min")
    lines = [f"{who}: тест закончен ({WHY_RU.get(d.get('why'), esc(d.get('why')))})",
             f"Рынок: <b>{esc(d.get('question'))}</b>", f"Заявки стояли {d.get('quoted_min', 0):.0f} мин · пул {d.get('rate', 0):.0f}$ в день",
             "",
             f"💰 Биржа начислила за тест: <b>{_usd(got)}</b>",
             f"🧮 Бот на бумаге записал бы: {_usd(est)}"
             + (f" (биржа дала {100 * got / est:.0f}% от расчёта)" if got is not None and est else ""),
             "",
             "Наша доля пула по данным биржи: " + ("нет ответа" if pct is None else
                                                   f"лучшая {pct if top is None else top:.0f}%, средняя {pct:.0f}%, "
                                                   f"в конце {pct if last is None else last:.0f}%"),
             f"Доля по расчёту бота: {'—' if paper is None else f'{100 * paper:.0f}%'}",
             ("Другие мейкеры не сжали нашу долю за время теста." if sq is None and pct is not None else
              f"Долю сжали вдвое через {sq:.0f} мин после старта." if sq is not None else "Долю оценить нельзя: биржа не ответила."),
             f"Биржа считала заявки: {'нет ответа' if sc is None else f'{100 * sc:.0f}% замеров'}"]
    if d.get("fills"):
        p = d.get("position") or {}
        lines += ["", f"Исполнений: {d['fills']}. В кошельке: Да {p.get('yes', 0):.0f}, Нет {p.get('no', 0):.0f}, куплено за {p.get('cost', 0):.2f}$"]
    left = d.get("orders_left_open")
    lines += ["", "Заявки сняты, открытых не осталось." if left == 0 else
              "⚠️ Не удалось проверить, что заявок не осталось." if left is None else f"⚠️ Осталось открытых заявок: {left}. Сними: /stop",
              "Подробно: /rlive"]
    return "\n".join(lines)


def format_rewards_live(unit="pmbot-rewards-live"):
    """/rlive: the latest live test of the rewards rule in plain words."""
    r = ops.rewards_live_summary(unit)
    if not r:
        return ("🧪 живой тест «Наград» ещё не запускался.\nЗапуск: /restart pmbot-rewards-live\n"
                "Это РЕАЛЬНЫЕ ДЕНЬГИ: один рынок, две заявки ≈$20, один час. При первом исполнении тест остановится.")
    head = f"🧪 <b>Награды · живой тест</b> 💰 · {ops.STATE_RU.get(r['state'], esc(r['state']))} {ops.fmt_age(r['since'])}"
    if r["refused"]:
        return head + f"\n⛔ запуск отклонён: {esc(r['refused'])}"
    m = r["market"]
    if not m:
        return head + ("\nподходящего рынка не нашлось, заявок не было" if r["no_market"] else "\nвыбирает рынок…")
    out = [head, f"Рынок: <b>{esc(m.get('question'))}</b>",
           f"Пул {m.get('rate', 0):.0f}$ в день · {'в зоне наград мы одни' if m.get('empty') else 'в зоне есть другие мейкеры'}", ""]
    sm = r["samples"]
    if sm and not r["done"]:
        x = sm[-1]
        pct = x.get("exch_pct")
        sc = x.get("scoring")
        out += [f"<b>Сейчас</b> (замеров {len(sm)})",
                f"доля по данным биржи: <b>{'нет ответа' if pct is None else f'{pct:.1f}%'}</b>",
                f"доля по расчёту бота: {100 * x.get('paper_share', 0):.1f}%",
                "биржа считает заявки: " + ("нет ответа" if sc is None else "да" if sc and all(sc.values()) else "не все" if any(sc.values()) else "нет"),
                "заявки: " + (", ".join(f"«{'Да' if k == 'Y' else 'Нет'}» по {v:.2f}" for k, v in sorted((x.get('bids') or {}).items())) or "не стоят"), ""]
    for fl in r["fills"]:
        out.append(f"⚠️ исполнение: «{'Да' if fl.get('side') == 'Y' else 'Нет'}» {fl.get('size', 0):.0f} по {fl.get('price', 0):.2f}")
    if r["done"]:
        out += rewards_live_done(r["done"]).split("\n")[2:-1] + [""]
    if r["earn"]:
        out += ["<b>Счётчик биржи по этому рынку за день</b> (все тесты дня вместе)"] + [f"{d}: <b>{v:.3f}$</b>" for d, v in sorted(r["earn"].items())]
    elif r["earn_days"]:
        out.append("В счётчике биржи за последние дни этого рынка пока нет.")
    else:
        out.append("Начисления биржи ещё не прочитаны.")
    hist = r.get("history") or []
    if len(hist) > 1 or any((h.get("position") or {}).get("cost") for h in hist):
        out += ["", "<b>Все тесты за 8 дней</b>"]
        for h in hist:
            when = datetime.fromtimestamp(h["t"] or 0, timezone.utc).strftime("%d.%m %H:%M")
            if h["refused"]:
                out.append(f"{when} · не запустился: {esc(str(h['refused'])[:80])}")
                continue
            p = h.get("position") or {}
            left = (f" · осталось в кошельке: {'Да' if p.get('yes') else 'Нет'} {p.get('yes') or p.get('no'):.0f} за {p.get('cost', 0):.2f}$"
                    if p.get("cost") else "")
            out.append(f"{when} · {esc((h['question'] or 'рынок не выбран')[:60])} · {WHY_RU.get(h['why'], esc(h['why'] or 'идёт'))}"
                       f" · {h.get('quoted_min') or 0:.0f} мин{left}")
    if r.get("earn_all"):
        out.append("Биржа начислила за наши заявки наград всего по дням: "
                   + ", ".join(f"{d[5:]} {v:.3f}$" for d, v in sorted(r["earn_all"].items()) if v))
    return "\n".join(out)


def format_rewards(unit="pmbot-rewards"):
    """/r: the liquidity-rewards farmer in detail -- today, since the start, by day, what it holds now."""
    r = ops.rewards_summary(unit)
    if not r:
        return "🎁 бот «Награды» (R) не запущен. Запуск: /restart pmbot-rewards\nКак работает: /rhow"
    lines = [f"🎁 <b>Награды · R</b> · бумага (без денег)",
             f"{ops.STATE_RU.get(r['state'], esc(r['state']))} {ops.fmt_age(r['since'])}", ""]
    st = r["status"]
    if not st:
        return "\n".join(lines + ["статуса ещё нет: первые цифры через 5 минут после запуска"])
    rew, alone, loss, total, cautious = _rewards_parts(st)
    real = st.get("realized_today")
    lines += [f"<b>Сегодня</b> (с 00:00 UTC)",
              f"💰 награды (расчёт): {money(rew, True)}",
              f"   🏝 из них где мы одни: {money(alone)}",
              f"💸 потери: {money(loss, True)}"]
    if real is not None:
        lines.append(f"   закрытые выходы: {money(real)} · незакрытые позиции по цене: {money(loss - real)}")
    lines += [f"{dot(total)} итог дня по расчёту: {money(total, True)}",
              f"🛡 осторожный итог: {money(cautious, True)}"]
    if "honest_today" in st:
        lines += [f"✅ <b>честный итог: {money(st['honest_today'], True)}</b>",
                  f"   награды только там, где другие держат половину пула: {money(st.get('shared_today'))}"]
    lines.append("")
    tot_rew = st.get("rewards_total") or 0.0
    tot_trade = st.get("trading_pnl") or 0.0
    lines += [f"<b>С начала</b>",
              f"💰 {money(tot_rew)} (🏝 {money(st.get('alone_total'))}) · 💸 {money(tot_trade)}",
              f"{dot(tot_rew + tot_trade)} итог {money(tot_rew + tot_trade, True)} · 🛡 осторожно "
              f"{money(st.get('cautious_pnl'), True)}"]
    if "honest_pnl" in st:
        lines.append(f"✅ честно: {money(st['honest_pnl'], True)} (награды «другие держат половину» считаются с 15 ноября)")
    lines.append("")
    days = (r.get("days") or [])[-5:]
    if days:
        lines.append("<b>По дням</b> (💰 награды · 🏝 одни · 💸 потери → 🛡 осторожно)")
        for d in days:
            dr, da, dt = d.get("rewards") or 0.0, d.get("alone") or 0.0, d.get("trading_pnl_day") or 0.0
            dc = max(min(dr, d.get("official", dr)) - da, 0.0) + dt
            lines.append(f"{esc(str(d['day'])[5:])}: 💰 {money(dr)} · 🏝 {money(da)} · 💸 {money(dt)} → 🛡 {money(dc, True)}")
        lines.append("")
    lines += ["<b>Сейчас</b>",
              f"рынков {st.get('markets', 0)}, заявки стоят в {st.get('quoting', 0)} (одни в {st.get('alone_now', 0)}) "
              f"· на ${st.get('bids_usd', 0):.0f}",
              f"📦 незакрытые позиции: рынков {st.get('open_positions', '?')}, куплены за ${st.get('unhedged_usd', 0):.0f}, "
              f"сейчас стоят ${st.get('inv_value', 0):.0f}",
              f"исполнений сегодня {st.get('fills_today', 0)} · закрыто докупкой {st.get('hedges_today', 0)} "
              f"(потеряно {money(-(st.get('hedge_loss_today') or 0))})"]
    lines.append("⛔ стоп до 00:00 UTC: итог дня хуже −$50" if st.get("halted")
                 else f"стоп: если осторожный итог дня станет хуже −$50 (сейчас {money(cautious)})")
    if st.get("feed") is False:
        lines.append("⚠️ поток цен молчит: заявки сняты, пока он не вернётся")
    lines += ["", "Живые замеры 14–15 ноября: за пустой пул биржа начислила в ~5 раз меньше расчёта, чужие мейкеры пришли "
                  "за минуты, одинокие заявки забирали за секунды. Поэтому верный итог — ✅ честный. Как работает: /rhow"]
    return "\n".join(lines)


def rewards_digest_line(unit="pmbot-rewards"):
    """One line for the digest, or None when the farmer has no status."""
    r = ops.rewards_summary(unit)
    if not r or not r["status"]:
        return None
    rew, alone, loss, total, cautious = _rewards_parts(r["status"])
    halted = " · ⛔ стоп до 00:00" if r["status"].get("halted") else ""
    st = r["status"]
    honest = f" · ✅ честно {money(st['honest_today'], True)}" if "honest_today" in st else ""
    return (f"🎁 <b>Награды · R</b> (бумага), сегодня: 💰 заработал {money(rew)} (🏝 где одни {money(alone)}) · "
            f"💸 потерял {money(loss)} · итог {money(total, True)} · 🛡 осторожно {money(cautious)}{honest}{halted} · /r")


def format_status():
    """/status (reports/88): real money first, then the tests, the data recorders and what is shelved or being built. Only running
    units are listed: a strategy that was dropped or a test that was switched off is not shown. Real money and paper are never
    added together (the owner read a paper +$20 as his own on 10 Nov); the day's real-money total includes a live unit stopped
    during the day."""
    live, paper, day_stopped, stopped_live, live_today = [], [], [], 0.0, 0.0
    for unit in ops.BOT_UNITS:
        s = ops.bot_summary(unit)
        is_live = s["mode"] == "live"
        if is_live:
            live_today += s["pnl_today"] or 0.0
        if s["state"] not in RUNNING:
            if is_live and s["killed"]:
                day_stopped.append(s)              # its own day stop: the owner must know, and restart it by hand
            elif is_live and s["pnl_today"]:
                stopped_live += s["pnl_today"]
            continue
        (live if is_live else paper).append(s)
    lines = [f"📊 <b>Боты</b> · {hhmm(time.time())} UTC"]
    w = ops.wallet_line()
    if w:
        lines.append(f"💼 {esc(w)}")
    lines += [f"💰 сегодня на реальных деньгах: {money(live_today, True)}", "", "💰 <b>НА РЕАЛЬНЫХ ДЕНЬГАХ</b>"]
    for s in live:
        lines += _card(s, True)
    for s in day_stopped:
        icon, name, _ = BOTS.get(s["unit"], ("•", s["unit"], ""))
        lines += [f"{icon} <b>{esc(name)}</b>",
                  f"   ⛔ дневной стоп: сегодня {money(s['pnl_today'], True)} · сам не включится, завтра: "
                  f"<code>/restart {esc(s['unit'])}</code>"]
    if not live and not day_stopped:
        lines.append("   сейчас ни один бот на деньгах не запущен")
    if abs(stopped_live) >= 0.005:
        lines.append(f"   ⏹ остановленные сегодня: {money(stopped_live)}")
    tests = list(paper)
    for unit in ops.REWARD_LIVE_UNITS:          # the rewards live test is real money too
        if ops.unit_state(unit)[0] in RUNNING and ops.rewards_live_summary(unit):
            lines += format_rewards_live(unit).split("\n")[:3] + ["   подробно: /rlive"]
    rewards = [u for u in ops.REWARD_UNITS if ops.unit_state(u)[0] in RUNNING]
    if tests or rewards:
        lines += ["", "🧪 <b>ТЕСТЫ</b> · без денег"]
        for s in tests:
            lines += _card(s, False)
        for unit in rewards:
            block = _rewards_block(unit)
            if block:
                lines += block
    recs = []
    for unit in ["pmbot-recorder"] + ops.WEATHER_UNITS + ops.RESEARCH_UNITS:
        st, since = ops.unit_state(unit)
        if st in RUNNING:
            icon, name, _ = BOTS.get(unit, ("•", unit, ""))
            note = ops.rec_note(unit)
            mark = "🟢" if st == "active" else ops.STATE_RU.get(st, esc(st))
            recs.append(f"{icon} <b>{esc(name)}</b> {mark} {ops.fmt_age(since)}" + (f" · {esc(note)}" if note else ""))
    if recs:
        lines += ["", "📡 <b>СБОР ДАННЫХ</b>"] + recs
    if SHELF:
        lines += ["", "🗂 <b>ОТЛОЖЕНО И В РАБОТЕ</b>"] + [f"{i} <b>{esc(n)}</b> — {esc(t)}" for i, n, t in SHELF]
    if os.path.exists(ops.KILL_FILE):
        lines += ["", "⛔ <b>файл STOP есть</b>: торговля остановлена (/resume — снять)"]
    g = ops.git_info()
    lines += [LINE, f"<code>{esc(g['branch'])} {esc(g['commit'])}</code>"]
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
        length = 3600 if unit.startswith("pmbot-hourly") else 300
        out.append(f"{dot(pnl)} {label(unit, False)} {window_name(e.get('key'), length)} → {esc(win)} "
                   f"{pos} {money(pnl, True)}")
    return "\n".join(out)


def format_server():
    return "🖥 <b>Сервер</b>\n" + esc(ops.format_server())
