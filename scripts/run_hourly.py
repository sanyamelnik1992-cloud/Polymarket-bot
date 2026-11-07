"""Run the hourly crypto Up/Down taker (BTC/ETH).

  paper:  python scripts/run_hourly.py --config config/hourly.toml --minutes 600
  live:   PMBOT_PRIVATE_KEY=... PMBOT_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY_RISK \\
          python scripts/run_hourly.py --config config/hourly_live.toml --live
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.hourly import HourlyEngine, load_hourly_config  # noqa: E402
from pmbot.bot.journal import Journal  # noqa: E402


def cfg_mode(cfg):
    return getattr(getattr(cfg, "execution", cfg), "mode", "?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/hourly.toml")
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--journal", default=None)
    args = ap.parse_args()
    cfg = load_hourly_config(args.config)
    if args.live != (cfg.mode == "live"):
        sys.exit("refusing to start: --live flag and hourly.mode must agree")
    run_dir = os.path.join(args.journal or cfg.journal_dir, time.strftime("%Y%m%d-%H%M%S"))
    from pmbot.bot.alerts import TelegramAlerts
    journal = Journal(run_dir, alerts=TelegramAlerts(prefix="hourly-" + cfg_mode(cfg), config_path=args.config,
                                                       mode=cfg_mode(cfg)))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "bot.log"))])
    logging.getLogger("websockets").setLevel(logging.WARNING)

    async def runner():
        broker = None
        if cfg.mode == "live":
            from pmbot.bot.live import TakerLiveBroker
            broker = TakerLiveBroker(cfg.max_open_cost, journal=journal)
            await broker.start()
        eng = HourlyEngine(cfg, journal, broker=broker, duration_s=args.minutes * 60 if args.minutes else None)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(eng, "_stop", True))
        await eng.run()

    asyncio.run(runner())
    journal.close()


if __name__ == "__main__":
    main()
