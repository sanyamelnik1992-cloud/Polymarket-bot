"""Send one Telegram message (used by deploy/*.sh).  usage: python scripts/tg_send.py "text"   (or text on stdin)

Silently does nothing when PMBOT_TG_TOKEN / PMBOT_TG_CHAT are not set.
"""
import os
import sys

import requests


def main():
    token, chat = os.environ.get("PMBOT_TG_TOKEN", "").strip(), os.environ.get("PMBOT_TG_CHAT", "").strip()
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    if not (token and chat and text.strip()):
        return
    for one in [c.strip() for c in chat.split(",") if c.strip()]:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": one, "text": text[:4000], "disable_web_page_preview": True}, timeout=15)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
