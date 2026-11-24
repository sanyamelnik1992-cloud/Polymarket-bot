"""Run the paper market maker (pmbot/bot/fairmaker.py, reports/89): bids on both sides of the BTC 5m windows around the
Binance fair value. PAPER ONLY (the config loader refuses anything else).

    python scripts/run_maker.py --config config/maker_fair.toml [--minutes N]

Stop with Ctrl+C / SIGTERM or the kill file STOP (no new bids; positions are held to resolution).
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.fairmaker import FairMaker, load_maker_config  # noqa: E402
from pmbot.bot.journal import Journal  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/maker_fair.toml")
    ap.add_argument("--minutes", type=float, default=None)
    args = ap.parse_args()
    cfg, mm = load_maker_config(args.config)
    run_dir = os.path.join(cfg.journal_dir, time.strftime("%Y%m%d-%H%M%S"))
    from pmbot.bot.alerts import TelegramAlerts
    journal = Journal(run_dir, alerts=TelegramAlerts(prefix="maker-paper", config_path=args.config, mode="paper"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "bot.log"))])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot = FairMaker(cfg, mm, journal)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(bot, "_stop", True))
        if args.minutes:
            loop.call_later(args.minutes * 60, lambda: setattr(bot, "_stop", True))
        await bot.run()

    logging.info("maker (paper) journal=%s", run_dir)
    asyncio.run(runner())
    journal.close()


if __name__ == "__main__":
    main()
