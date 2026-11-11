"""Optional Telegram alerts for important journal events (set PMBOT_TG_TOKEN and PMBOT_TG_CHAT).

Messages are formatted by pmbot.notify (HTML). Sent asynchronously from a background thread so trading is never
blocked by the network.
  - paper bots only report what needs attention (kill switch, errors); start / pause / final / every closed window
    are sent only in LIVE mode (closed windows also with PMBOT_TG_SETTLES=1). Paper results come in the digests
  - the same error kind is sent at most once per ERROR_EVERY_S; the next message says how many were skipped
"""
import os
import queue
import re
import threading
import time

import requests

ALERT_TYPES = {"start", "kill", "pause", "order_rejected", "cancel_failed", "heartbeat_error", "user_stream_error",
               "broker_fatal", "api_error", "reset_failed", "settle_timeout", "existing_position", "final", "settle", "geoblock_ok", "balance",
               "order_failed", "order_pending", "lock_hedge"}
LIVE_ONLY = {"start", "pause", "final", "settle", "existing_position", "geoblock_ok", "balance", "lock_hedge"}
RATE_LIMITED = {"order_rejected", "cancel_failed", "heartbeat_error", "user_stream_error", "settle_timeout",
                "api_error", "reset_failed", "order_failed", "order_pending"}
# the book moved during our latency: routine, not an alert (a maker's post-only bid would cross; a taker's FAK found
# its ask already gone). The hourly report counts them ("продажи уже не было")
NORMAL_REJECTS = {"post_only_would_cross", "fak_not_filled", "fok_not_filled"}
ERROR_EVERY_S = 600


class TelegramAlerts:
    def __init__(self, token=None, chat=None, prefix="pmbot", config_path=None, mode="paper", clock=time.time):
        self.token = token or os.environ.get("PMBOT_TG_TOKEN")
        self.chat = chat or os.environ.get("PMBOT_TG_CHAT")
        self.prefix = prefix
        self.mode = mode
        self.per_window = os.environ.get("PMBOT_TG_SETTLES", "").strip() == "1"
        self.clock = clock
        self.unit = None
        if config_path:
            try:
                from ..notify import unit_for_config
                self.unit = unit_for_config(config_path)
            except Exception:  # noqa: BLE001  (labels are cosmetic; never break the bot)
                self.unit = None
        self._last_err = {}       # kind -> last sent ts
        self._skipped = {}        # kind -> messages not sent since then
        self.q = queue.Queue(maxsize=500)
        self.enabled = bool(self.token and self.chat)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def render(self, kind, rec):
        """HTML text for an event, or None (not an alert / rate-limited / paper window)."""
        if kind not in ALERT_TYPES:
            return None
        if kind == "order_rejected" and rec.get("code") in NORMAL_REJECTS:
            return None
        if kind in LIVE_ONLY and self.mode != "live" and not (kind == "settle" and self.per_window):
            return None
        if kind in RATE_LIMITED:
            now = self.clock()
            if now - self._last_err.get(kind, -1e18) < ERROR_EVERY_S:
                self._skipped[kind] = self._skipped.get(kind, 0) + 1
                return None
            self._last_err[kind] = now
        from ..notify import event_message
        try:
            msg = event_message(kind, rec, unit=self.unit, prefix=self.prefix, mode=self.mode,
                                per_window=self.per_window)
        except Exception as e:  # noqa: BLE001
            msg = f"{self.prefix} {kind} (format error {type(e).__name__})"
        if msg and self._skipped.get(kind):
            msg += f"\n(ещё таких же за {ERROR_EVERY_S // 60} мин: {self._skipped.pop(kind)})"
        return msg

    def maybe_send(self, kind, rec):
        if not self.enabled:
            return
        msg = self.render(kind, rec)
        if not msg:
            return
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            pass

    def _worker(self):
        while True:
            text = self.q.get()
            send_html(self.token, self.chat, text)


def chat_ids(chat):
    """PMBOT_TG_CHAT may hold several chat ids separated by commas (the first is the owner)."""
    return [c.strip() for c in str(chat or "").split(",") if c.strip()]


def send_photo(token, chat, png, caption=""):
    """Send a PNG with an HTML caption (Telegram caption limit 1024) to every chat in `chat`."""
    ok = False
    for one in chat_ids(chat):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                              data={"chat_id": one, "caption": caption[:1024], "parse_mode": "HTML"},
                              files={"photo": ("chart.png", png, "image/png")}, timeout=30)
            ok = r.status_code == 200 or ok
        except requests.RequestException:
            pass
    return ok


def send_html(token, chat, text):
    """Send to every chat in `chat`; True if at least one got it."""
    ok = False
    for one in chat_ids(chat):
        ok = _send_html_one(token, one, text) or ok
    return ok


def _send_html_one(token, chat, text):
    """Send as HTML; if Telegram rejects the markup, resend the same text without tags."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=10)
        if r.status_code == 400:
            plain = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            requests.post(url, json={"chat_id": chat, "text": plain[:4000], "disable_web_page_preview": True},
                          timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        return False
