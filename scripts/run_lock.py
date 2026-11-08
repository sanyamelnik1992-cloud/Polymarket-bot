"""Run the TWAP-lock bot (reports/44_twap_lock.md).

  paper (default):  python scripts/run_lock.py --config config/lock.toml
  live:             python scripts/run_lock.py --config config/lock_live.toml --live   (REAL MONEY)

Stop with Ctrl+C / SIGTERM or the kill file STOP (no new entries; open entries are held to resolution).
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.journal import Journal  # noqa: E402
from pmbot.bot.lock import LockBot, load_lock_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/lock.toml")
    ap.add_argument("--live", action="store_true", help="REAL MONEY. Requires execution.mode='live' and env gates.")
    ap.add_argument("--minutes", type=float, default=None)
    args = ap.parse_args()
    cfg = load_lock_config(args.config)
    if args.live != (cfg.execution.mode == "live"):
        sys.exit("refusing to start: --live flag and execution.mode in the config must agree")
    run_dir = os.path.join(cfg.journal_dir, time.strftime("%Y%m%d-%H%M%S"))
    from pmbot.bot.alerts import TelegramAlerts
    journal = Journal(run_dir, alerts=TelegramAlerts(prefix="lock-" + cfg.execution.mode, config_path=args.config,
                                                       mode=cfg.execution.mode))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "bot.log"))])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot = LockBot(cfg, journal)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(bot, "_stop", True))
        if args.minutes:
            loop.call_later(args.minutes * 60, lambda: setattr(bot, "_stop", True))
        await bot.run()

    logging.info("lock bot mode=%s journal=%s", cfg.execution.mode, run_dir)
    asyncio.run(runner())
    journal.close()


if __name__ == "__main__":
    main()
