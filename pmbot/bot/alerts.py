"""Optional Telegram alerts for important journal events (set PMBOT_TG_TOKEN and PMBOT_TG_CHAT).

Sent asynchronously from a background thread so trading is never blocked by the network.
"""
import os
import queue
import threading

import requests

ALERT_TYPES = {"start", "kill", "pause", "cancel_everything", "order_rejected", "cancel_failed", "heartbeat_error",
               "user_stream_error", "existing_position", "final", "settle", "geoblock_ok", "balance"}


class TelegramAlerts:
    def __init__(self, token=None, chat=None, prefix="pmbot"):
        self.token = token or os.environ.get("PMBOT_TG_TOKEN")
        self.chat = chat or os.environ.get("PMBOT_TG_CHAT")
        self.prefix = prefix
        self.q = queue.Queue(maxsize=500)
        self.enabled = bool(self.token and self.chat)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def maybe_send(self, kind, rec):
        if not self.enabled or kind not in ALERT_TYPES:
            return
        if kind == "settle":
            msg = f"{rec.get('key')} {rec.get('winner')} pnl {rec.get('pnl', 0):+.2f}"
        elif kind == "start":
            msg = f"started mode={rec.get('mode')}"
        else:
            msg = ", ".join(f"{k}={v}" for k, v in rec.items() if k not in ("t", "type", "config"))[:600]
        try:
            self.q.put_nowait(f"[{self.prefix}] {kind}: {msg}")
        except queue.Full:
            pass

    def _worker(self):
        while True:
            text = self.q.get()
            try:
                requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat, "text": text}, timeout=10)
            except requests.RequestException:
                pass
