"""Telegram control bot for the server: status, PnL, trades, server health, logs, emergency stop, updates.

  env: PMBOT_TG_TOKEN (from @BotFather), PMBOT_TG_CHAT (your chat id; only this chat is obeyed)
       PMBOT_TG_DIGEST_MIN=60   hourly digest (0 = off); a daily report is sent at 00:05 UTC
  run: python scripts/tg_bot.py          (systemd unit pmbot-tg)

Without PMBOT_TG_CHAT the bot only answers "your chat id is ..." so the id can be put into /etc/pmbot/pmbot.env.
Trading bots do not depend on this process: if it dies nothing changes for them.
"""
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot import ops  # noqa: E402

log = logging.getLogger("tg")
TOKEN = os.environ.get("PMBOT_TG_TOKEN", "").strip()
CHAT = os.environ.get("PMBOT_TG_CHAT", "").strip()
DIGEST_MIN = float(os.environ.get("PMBOT_TG_DIGEST_MIN", "60") or 0)
API = f"https://api.telegram.org/bot{TOKEN}"

HELP = """Команды:
/status — боты, PnL за сегодня, режим
/pnl — PnL по дням
/trades — последние 10 закрытых окон
/server — CPU, RAM, диск
/log [юнит] — последние строки лога (по умолчанию pmbot-5m)
/stop — АВАРИЙНЫЙ СТОП: снять все заявки и остановить торговлю
/resume — снять STOP и запустить ботов снова
/update — сейчас подтянуть код с GitHub
/restart [юнит] — перезапустить юнит
Юниты: """ + ", ".join(ops.ALL_UNITS)


def send(text, chat=None):
    chat = chat or CHAT
    for i in range(0, max(len(text), 1), 4000):
        try:
            requests.post(f"{API}/sendMessage", json={"chat_id": chat, "text": text[i:i + 4000],
                                                      "disable_web_page_preview": True}, timeout=15)
        except requests.RequestException as e:
            log.warning("send failed: %s", e)


def systemctl(*args):
    cmd = ["systemctl", *args]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def enabled_units(units):
    out = []
    for u in units:
        r = subprocess.run(["systemctl", "is-enabled", u], capture_output=True, text=True)
        if r.stdout.strip() == "enabled":
            out.append(u)
    return out


# ---------------------------------------------------------------- commands
def cmd_stop(_):
    open(ops.KILL_FILE, "w").write(f"stopped from Telegram at {datetime.now(timezone.utc).isoformat()}\n")
    return ("⛔ STOP создан. Боты снимают все заявки и выключаются (обычно за несколько секунд).\n"
            "Вернуть: /resume")


def cmd_resume(_):
    removed, cleared = ops.resume_after_stop()
    started = []
    for u in enabled_units(ops.BOT_UNITS):
        ok, _ = systemctl("start", "--no-block", u)
        if ok:
            started.append(u)
    msg = ("STOP удалён. " if removed else "STOP не было. ") + (f"kill снят: {', '.join(cleared)}. " if cleared else "")
    msg += f"Запускаю: {', '.join(started) or 'нечего'}.\n"
    msg += "Если бот остановился по дневному стопу убытка, он сам не начнёт торговать до 00:00 UTC."
    return msg


def cmd_update(_):
    ok, out = systemctl("start", "--no-block", "pmbot-autoupdate.service")
    return "🔄 Проверяю GitHub, результат придёт сюда." if ok else f"не получилось: {out[:500]}"


def cmd_restart(arg):
    unit = arg or "pmbot-5m"
    if unit not in ops.ALL_UNITS:
        return f"неизвестный юнит {unit}. Юниты: {', '.join(ops.ALL_UNITS)}"
    ok, out = systemctl("restart", "--no-block", unit)
    return f"перезапускаю {unit}" if ok else f"не получилось: {out[:500]}"


def cmd_log(arg):
    unit = arg or "pmbot-5m"
    if unit not in ops.ALL_UNITS:
        return f"неизвестный юнит {unit}"
    return ops.journal_tail(unit, 15) or "лог пуст"


COMMANDS = {
    "/start": lambda a: HELP, "/help": lambda a: HELP,
    "/status": lambda a: ops.format_status(), "/pnl": lambda a: ops.format_pnl(),
    "/trades": lambda a: ops.format_trades(), "/server": lambda a: ops.format_server(),
    "/log": cmd_log, "/stop": cmd_stop, "/resume": cmd_resume, "/update": cmd_update, "/restart": cmd_restart,
}


def handle(msg):
    chat = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not CHAT:
        log.info("message from chat %s: %s", chat, text[:50])
        send(f"Твой chat id: {chat}\nВпиши его в /etc/pmbot/pmbot.env как PMBOT_TG_CHAT и перезапусти pmbot-tg.", chat)
        return
    if chat != CHAT:
        log.warning("ignored message from foreign chat %s", chat)
        return
    parts = text.split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()
    fn = COMMANDS.get(cmd)
    if fn is None:
        send("не понял. /help — список команд")
        return
    try:
        send(fn(parts[1] if len(parts) > 1 else ""))
    except Exception as e:  # noqa: BLE001
        log.exception("command %s failed", cmd)
        send(f"ошибка: {e}")


# ---------------------------------------------------------------- background notifications
class Watcher:
    """Unit state changes, low disk, hourly digest, daily report."""

    def __init__(self):
        self.states = {u: ops.unit_state(u)[0] for u in ops.ALL_UNITS}
        self.last_digest = time.time()
        self.last_daily = ops.today_utc()
        self.disk_warned = False

    def tick(self):
        for u in ops.ALL_UNITS:
            st = ops.unit_state(u)[0]
            prev = self.states.get(u)
            if prev != st and st in ("active", "failed", "inactive") and prev not in (None, "n/a"):
                if st == "failed":
                    send(f"🔴 {u} упал. /log {u}")
                elif st == "inactive" and prev == "active":
                    reason = " (файл STOP)" if os.path.exists(ops.KILL_FILE) else ""
                    send(f"⚪ {u} остановлен{reason}")
                elif st == "active" and prev in ("failed", "inactive", "activating"):
                    send(f"🟢 {u} запущен")
            self.states[u] = st
        h = ops.server_health()
        if h["disk_free_gb"] < 2 and not self.disk_warned:
            send(f"⚠️ на диске осталось {h['disk_free_gb']:.1f} ГБ")
            self.disk_warned = True
        elif h["disk_free_gb"] > 4:
            self.disk_warned = False
        now = time.time()
        if DIGEST_MIN > 0 and now - self.last_digest >= DIGEST_MIN * 60:
            self.digest(self.last_digest)
            self.last_digest = now
        day = ops.today_utc()
        if day != self.last_daily and datetime.now(timezone.utc).minute >= 5:
            self.daily(self.last_daily)
            self.last_daily = day

    def digest(self, since):
        parts = []
        for u in ops.BOT_UNITS:
            s = ops.bot_summary(u, since_ts=since)
            if not s["settles"] and not s["fills"]:
                continue
            pnl = sum(e.get("pnl", 0) for e in s["settles"])
            parts.append(f"{u}: окон {len(s['settles'])}, исполнений {len(s['fills'])}, {pnl:+.2f}$ "
                         f"(сегодня {ops.fmt_money(s['pnl_today'])})")
        if parts:
            send(f"🕐 за {DIGEST_MIN:.0f} мин:\n" + "\n".join(parts))

    def daily(self, day):
        lines = [f"📅 итог {day} (UTC)"]
        for u in ops.BOT_UNITS:
            _, state_file, mode = ops.bot_paths(u)
            by_day = ops.risk_state(state_file).get("realized_by_day", {})
            if day in by_day:
                lines.append(f"{u} [{mode}]: {by_day[day]:+.2f}$, всего {sum(by_day.values()):+.2f}$")
        if len(lines) > 1:
            lines.append(ops.format_server())
            send("\n".join(lines))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        sys.exit("PMBOT_TG_TOKEN is not set (/etc/pmbot/pmbot.env)")
    try:
        requests.post(f"{API}/setMyCommands", timeout=15, json={"commands": [
            {"command": c.strip("/"), "description": d} for c, d in [
                ("/status", "статус и PnL"), ("/pnl", "PnL по дням"), ("/trades", "последние сделки"),
                ("/server", "сервер"), ("/log", "лог"), ("/stop", "АВАРИЙНЫЙ СТОП"), ("/resume", "снять стоп"),
                ("/update", "обновить код"), ("/restart", "перезапустить юнит"), ("/help", "помощь")]]})
    except requests.RequestException:
        pass
    if CHAT:
        send("🤖 pmbot-tg запущен\n\n" + ops.format_status())
    watcher = Watcher() if CHAT else None
    offset, next_watch = None, 0.0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=35).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    handle(u["message"])
        except (requests.RequestException, ValueError) as e:
            log.warning("getUpdates: %s", e)
            time.sleep(5)
        if watcher and time.time() >= next_watch:
            try:
                watcher.tick()
            except Exception:  # noqa: BLE001
                log.exception("watcher")
            next_watch = time.time() + 60


if __name__ == "__main__":
    main()
