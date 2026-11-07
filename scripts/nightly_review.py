"""Nightly review of all bots for one UTC day (default: yesterday), with a verdict on every experiment pair.

  python scripts/nightly_review.py [--day YYYY-MM-DD] [--send] [--out data/reviews]
  --send   post the review and the 7-day PnL chart to Telegram (PMBOT_TG_TOKEN / PMBOT_TG_CHAT)
Writes OUT/<day>.md (copied into the server-reports branch by scripts/server_report.py).
Runs from pmbot-review.timer at 00:20 UTC. Rules: pmbot/review.py.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot import charts, ops, review  # noqa: E402
from pmbot.bot.alerts import send_html, send_photo  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=ops.today_utc(time.time() - 86400))
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--out", default=os.path.join(ops.REPO, "data", "reviews"))
    a = ap.parse_args()
    tg, md, _ = review.build(a.day)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, f"{a.day}.md"), "w") as f:
        f.write(md)
    print(md)
    token, chat = os.environ.get("PMBOT_TG_TOKEN", "").strip(), os.environ.get("PMBOT_TG_CHAT", "").strip()
    if a.send and token and chat:
        send_html(token, chat, tg)
        png = charts.equity_png(7)
        if png:
            send_photo(token, chat, png, "📈 Накопленный PnL за 7 дней (пунктир — строгие близнецы)")


if __name__ == "__main__":
    main()
