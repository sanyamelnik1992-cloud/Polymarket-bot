"""Run the liquidity-rewards farmer (paper only).

  python scripts/run_rewards.py --config config/rewards.toml [--minutes 120] [--journal DIR]

Stop with Ctrl+C / SIGTERM or the kill file (./STOP). Results: scripts/report_rewards.py <journal run dir>.
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
from pmbot.rewards.config import load_rewards_config  # noqa: E402
from pmbot.rewards.farmer import Farmer  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/rewards.toml")
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--journal", default=None, help="override journal directory")
    ap.add_argument("--state", default=None, help="override state file ('' = none)")
    args = ap.parse_args()
    over = {}
    if args.state is not None:
        over["state_file"] = args.state
    rc = load_rewards_config(args.config, over)
    run_dir = os.path.join(args.journal or rc.journal_dir, time.strftime("%Y%m%d-%H%M%S"))
    from pmbot.bot.alerts import TelegramAlerts
    journal = Journal(run_dir, alerts=TelegramAlerts(prefix="rewards-paper", config_path=args.config, mode="paper"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "bot.log"))])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    farmer = Farmer(rc, journal)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(farmer, "_stop", True))
        await farmer.run(duration_s=args.minutes * 60 if args.minutes else None)

    logging.info("rewards farmer, paper, journal=%s", run_dir)
    asyncio.run(runner())
    journal.close()


if __name__ == "__main__":
    main()
