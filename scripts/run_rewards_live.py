"""LIVE test of the liquidity-rewards rule: REAL MONEY, one market, about $20 in resting bids (pmbot/rewards/live.py).

  python scripts/run_rewards_live.py --config config/rewards_live.toml --live [--kind alone|shared|any] [--minutes 180]

Needs --live, rewards.mode = "live" in the config, PMBOT_LIVE_CONFIRM and PMBOT_PRIVATE_KEY in the environment.
Ends by itself: after live.minutes, on the first fill, on the kill file (./STOP) or SIGINT / SIGTERM. On the way out it
cancels its own orders (by id, then by the market's tokens; never cancel-all) and checks that none are left.
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.journal import Journal  # noqa: E402
from pmbot.bot.live import LiveGateError  # noqa: E402
from pmbot.rewards.live import LiveFarmer, RewardsLiveBroker, load_rewards_live_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/rewards_live.toml")
    ap.add_argument("--live", action="store_true", help="required: this script only trades real money")
    ap.add_argument("--kind", default=None, choices=["alone", "shared", "any"])
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--slug", default=None, help="exact market slug instead of the automatic choice")
    args = ap.parse_args()
    rc, lc = load_rewards_live_config(args.config, live_flag=args.live)
    over = {k: v for k, v in (("kind", args.kind), ("minutes", args.minutes), ("slug", args.slug)) if v is not None}
    if over:
        lc = replace(lc, **over)
    run_dir = os.path.join(rc.journal_dir, time.strftime("%Y%m%d-%H%M%S"))
    from pmbot.bot.alerts import TelegramAlerts
    journal = Journal(run_dir, alerts=TelegramAlerts(prefix="rewards-live", config_path=args.config, mode="live"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "bot.log"))])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    broker = RewardsLiveBroker(lc, journal=journal)
    farmer = LiveFarmer(rc, lc, journal, broker)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: farmer.finish("signal"))
        return await farmer.run()

    logging.info("rewards LIVE test (real money), kind=%s, %s min, journal=%s", lc.kind, lc.minutes, run_dir)
    code = 0
    try:
        done = asyncio.run(runner())
        logging.info("done: %s", done)
    except LiveGateError as e:
        journal.log("kill", reason=f"live gate: {e}")
        logging.error("live gate: %s", e)
        code = 0                         # a refused start is not a crash: systemd must not retry it
    journal.close()
    time.sleep(2)                        # let the last Telegram messages leave
    sys.exit(code)


if __name__ == "__main__":
    main()
