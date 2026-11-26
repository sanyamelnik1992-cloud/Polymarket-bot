"""Run the market maker (pmbot/bot/fairmaker.py, reports/89): bids on both sides of the BTC 5m windows around the Binance fair
value.

  paper (default):  python scripts/run_maker.py --config config/maker_fair.toml [--minutes N]
  live:             python scripts/run_maker.py --config config/maker_live.toml --live   (REAL MONEY: the test of reports/94)

A live config runs only with --live and a paper config only without it (the loaders refuse the other way round).
Stop with Ctrl+C / SIGTERM or the kill file STOP: the bids are pulled at once, positions are held to resolution.
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.fairmaker import FairMaker, load_maker_config, load_maker_live_config  # noqa: E402
from pmbot.bot.journal import Journal  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/maker_fair.toml")
    ap.add_argument("--live", action="store_true", help="REAL MONEY: the live maker test (execution.mode = 'live' + env gates)")
    ap.add_argument("--minutes", type=float, default=None)
    args = ap.parse_args()
    lc = None
    if args.live:
        cfg, mm, lc = load_maker_live_config(args.config, live_flag=True)
    else:
        cfg, mm = load_maker_config(args.config)
    mode = cfg.execution.mode
    run_dir = os.path.join(cfg.journal_dir, time.strftime("%Y%m%d-%H%M%S"))
    from pmbot.bot.alerts import TelegramAlerts
    journal = Journal(run_dir, alerts=TelegramAlerts(prefix="maker-" + mode, config_path=args.config, mode=mode))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "bot.log"))])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot = FairMaker(cfg, mm, journal, live=lc)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(bot, "_stop", True))
        if args.minutes:
            loop.call_later(args.minutes * 60, lambda: setattr(bot, "_stop", True))
        await bot.run()

    logging.info("maker (%s) journal=%s", mode, run_dir)
    asyncio.run(runner())
    journal.close()


if __name__ == "__main__":
    main()
