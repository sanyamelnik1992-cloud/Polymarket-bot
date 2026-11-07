"""Telegram control bot for the server: status, PnL, trades, server health, logs, emergency stop, updates.

  env: PMBOT_TG_TOKEN (from @BotFather), PMBOT_TG_CHAT (your chat id; only this chat is obeyed)
       PMBOT_TG_DIGEST_HOURS=6,12,18   UTC hours of the digest ("" = off); the nightly review comes at 00:20 UTC
                                       from pmbot-review.timer
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
from pmbot import notify, ops  # noqa: E402
from pmbot.notify import esc, label  # noqa: E402

log = logging.getLogger("tg")
TOKEN = os.environ.get("PMBOT_TG_TOKEN", "").strip()
CHAT = os.environ.get("PMBOT_TG_CHAT", "").strip()
DIGEST_HOURS = sorted({int(h) % 24 for h in os.environ.get("PMBOT_TG_DIGEST_HOURS", "6,12,18").replace(" ", "").split(",")
                        if h.isdigit()})
API = f"https://api.telegram.org/bot{TOKEN}"

HELP = """🤖 <b>pmbot — команды</b>

📊 /status — боты, PnL за сегодня, режим
📈 /chart [дней] — график накопленного PnL (по умолчанию 7 дней)
🔬 /review — разбор дня и вердикт по экспериментам (C/D) прямо сейчас
🧾 /trades — последние 10 закрытых окон
🖥 /server — CPU, RAM, диск
📜 /log [юнит] — последние строки лога

⛔ /stop — <b>АВАРИЙНЫЙ СТОП</b>: снять все заявки и остановить торговлю
▶️ /resume — снять STOP и запустить ботов снова
🔄 /update — подтянуть код с GitHub
♻️ /restart [юнит] — перезапустить юнит

<b>Боты</b>
""" + "\n".join(f"{label(u)} — <code>{u}</code>" for u in ops.BOT_UNITS if u.startswith("pmbot-5m")) + """

Сам присылаю: сводку в """ + ", ".join(f"{h:02d}:00" for h in DIGEST_HOURS) + """ UTC, ночной разбор с графиком в 00:20 UTC,
тревоги (стоп, ошибки, падение юнита, мало места)."""


def chunks(text, size=3900):
    """Split on line breaks so HTML tags (one line each) are never cut in half."""
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line[:size]
    return out + [cur] if cur else out or [""]


def send(text, chat=None):
    from pmbot.bot.alerts import send_html
    for part in chunks(text):
        if not send_html(TOKEN, chat or CHAT, part):
            log.warning("send failed")


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
    return ("⛔ <b>STOP создан</b>\nБоты снимают все заявки и выключаются (обычно за несколько секунд).\n"
            "Вернуть: /resume")


def cmd_resume(_):
    removed, cleared = ops.resume_after_stop()
    started = []
    for u in enabled_units(ops.BOT_UNITS):
        ok, _ = systemctl("start", "--no-block", u)
        if ok:
            started.append(u)
    msg = ("▶️ STOP удалён. " if removed else "STOP не было. ") + \
        (f"kill снят: {', '.join(label(u, False) for u in cleared)}. " if cleared else "")
    msg += f"Запускаю: {', '.join(label(u, False) for u in started) or 'нечего'}.\n"
    msg += "Если бот остановился по дневному стопу убытка, он сам не начнёт торговать до 00:00 UTC."
    return msg


def cmd_update(_):
    ok, out = systemctl("start", "--no-block", "pmbot-autoupdate.service")
    return "🔄 Проверяю GitHub, результат придёт сюда." if ok else f"не получилось: {esc(out[:500])}"


def cmd_restart(arg):
    unit = arg or "pmbot-5m"
    if unit not in ops.ALL_UNITS:
        return f"неизвестный юнит {esc(unit)}. Юниты: {', '.join(ops.ALL_UNITS)}"
    ok, out = systemctl("restart", "--no-block", unit)
    return f"♻️ перезапускаю {label(unit)}" if ok else f"не получилось: {esc(out[:500])}"


def cmd_chart(arg):
    from pmbot import charts
    from pmbot.bot.alerts import send_photo
    try:
        days = max(1, min(60, int(arg or 7)))
    except ValueError:
        days = 7
    png = charts.equity_png(days)
    if not png:
        return "за этот период закрытых окон нет"
    send_photo(TOKEN, CHAT, png, f"📈 Накопленный PnL за {days} дн. (пунктир — строгие близнецы)")
    return None


def cmd_review(_):
    # separate low-priority process: it parses whole journals (pandas), the trading bots must not feel it
    script = os.path.join(ops.REPO, "scripts", "nightly_review.py")
    subprocess.Popen(["nice", "-n", "15", sys.executable, script, "--day", ops.today_utc(), "--send",
                      "--out", os.path.join(ops.REPO, "data", "reviews", "partial")],
                     cwd=ops.REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "🔬 считаю разбор за сегодня (до текущего момента), пришлю через 1–2 минуты"


def cmd_log(arg):
    unit = arg or "pmbot-5m"
    if unit not in ops.ALL_UNITS:
        return f"неизвестный юнит {esc(unit)}"
    tail = ops.journal_tail(unit, 15)
    return f"📜 {label(unit)}\n<pre>{esc(tail[-3500:])}</pre>" if tail else "лог пуст"


COMMANDS = {
    "/start": lambda a: HELP, "/help": lambda a: HELP,
    "/status": lambda a: notify.format_status(),
    "/trades": lambda a: notify.format_trades(), "/server": lambda a: notify.format_server(),
    "/chart": cmd_chart, "/review": cmd_review,
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
        reply = fn(parts[1] if len(parts) > 1 else "")
        if reply:
            send(reply)
    except Exception as e:  # noqa: BLE001
        log.exception("command %s failed", cmd)
        send(f"ошибка: {esc(e)}")


# ---------------------------------------------------------------- background notifications
class Watcher:
    """Unit state changes, low disk, hourly digest, daily report."""

    def __init__(self):
        self.states = {u: ops.unit_state(u)[0] for u in ops.ALL_UNITS}
        self.last_slot = self.slot()
        self.disk_warned = False

    def tick(self):
        for u in ops.ALL_UNITS:
            st = ops.unit_state(u)[0]
            prev = self.states.get(u)
            if prev != st and st in ("active", "failed", "inactive") and prev not in (None, "n/a"):
                if st == "failed":
                    send(f"🔴 {label(u)} <b>упал</b>\nsystemd перезапустит его сам (до 5 раз за 10 мин). "
                         f"Лог: /log {u}")
                elif st == "inactive" and prev == "active":
                    reason = "\nпричина: файл STOP (/resume — снять)" if os.path.exists(ops.KILL_FILE) else ""
                    send(f"⚪ {label(u)} остановлен{reason}")
                elif st == "active" and prev in ("failed", "inactive", "activating") and u not in ops.BOT_UNITS:
                    send(f"🟢 {label(u)} запущен")   # trading bots announce their own start
            self.states[u] = st
        h = ops.server_health()
        if h["disk_free_gb"] < 2 and not self.disk_warned:
            send(f"⚠️ <b>мало места</b>: на диске осталось {h['disk_free_gb']:.1f} ГБ")
            self.disk_warned = True
        elif h["disk_free_gb"] > 4:
            self.disk_warned = False
        slot = self.slot()
        if slot != self.last_slot:
            self.last_slot = slot
            if slot is not None:
                self.digest(slot)

    @staticmethod
    def slot():
        """Start (ts) of the latest digest hour that has passed, or None when digests are off."""
        if not DIGEST_HOURS:
            return None
        now = datetime.now(timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        past = [day0 + h * 3600 for h in DIGEST_HOURS if day0 + h * 3600 <= now.timestamp()]
        return past[-1] if past else day0 - 86400 + DIGEST_HOURS[-1] * 3600

    def digest(self, slot):
        """Digest for the period since the previous digest hour (the night is covered by the nightly review)."""
        prev = [slot - k * 3600 for k in range(1, 25) if datetime.fromtimestamp(slot - k * 3600, timezone.utc).hour
                in DIGEST_HOURS or datetime.fromtimestamp(slot - k * 3600, timezone.utc).hour == 0]
        since = prev[0] if prev else slot - 6 * 3600
        msg = notify.format_digest(since, (slot - since) / 3600)
        if msg:
            send(msg)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        sys.exit("PMBOT_TG_TOKEN is not set (/etc/pmbot/pmbot.env)")
    try:
        requests.post(f"{API}/setMyCommands", timeout=15, json={"commands": [
            {"command": c.strip("/"), "description": d} for c, d in [
                ("/status", "статус и PnL"), ("/chart", "график PnL"), ("/review", "разбор и вердикт"),
                ("/trades", "последние сделки"),
                ("/server", "сервер"), ("/log", "лог"), ("/stop", "АВАРИЙНЫЙ СТОП"), ("/resume", "снять стоп"),
                ("/update", "обновить код"), ("/restart", "перезапустить юнит"), ("/help", "помощь")]]})
    except requests.RequestException:
        pass
    log.info("started; chat %s", "set" if CHAT else "not set")   # silent: restarts come with every code update
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
