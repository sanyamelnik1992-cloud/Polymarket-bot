"""Run the 5m Up/Down market-making bot.

  paper (default):  python scripts/run_bot.py --config config/paper.toml --minutes 120
  live:             PMBOT_PRIVATE_KEY=... PMBOT_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY_RISK \\
                    python scripts/run_bot.py --config config/live.toml --live

Stop at any time with Ctrl+C or by creating the kill file (default ./STOP): all orders are cancelled.
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.config import load_config  # noqa: E402
from pmbot.bot.engine import Engine  # noqa: E402
from pmbot.bot.journal import Journal  # noqa: E402
from pmbot.bot.paper import PaperBroker  # noqa: E402


def cfg_mode(cfg):
    return getattr(getattr(cfg, "execution", cfg), "mode", "?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/paper.toml")
    ap.add_argument("--minutes", type=float, default=None, help="stop after N minutes (default: run until stopped)")
    ap.add_argument("--live", action="store_true", help="REAL MONEY. Requires execution.mode='live' and env gates.")
    ap.add_argument("--journal", default=None, help="override journal directory")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.live != (cfg.execution.mode == "live"):
        sys.exit("refusing to start: --live flag and execution.mode in the config must agree")
    run_dir = os.path.join(args.journal or cfg.journal_dir, time.strftime("%Y%m%d-%H%M%S"))
    from pmbot.bot.alerts import TelegramAlerts
    journal = Journal(run_dir, alerts=TelegramAlerts(prefix="5m-" + cfg_mode(cfg), config_path=args.config,
                                                       mode=cfg_mode(cfg)))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "bot.log"))])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # live: every heartbeat / order request at INFO

    if cfg.execution.mode == "live":
        from pmbot.bot.live import LiveBroker

        def factory(on_fill, journal):
            return LiveBroker(cfg, on_fill=on_fill, journal=journal)
    else:
        def factory(on_fill, journal):
            return PaperBroker(cfg.execution.place_latency_ms, cfg.execution.cancel_latency_ms,
                               on_fill=on_fill, journal=journal, fill_at_touch=cfg.execution.paper_fill_at_touch)

    eng = Engine(cfg, factory, journal, duration_s=args.minutes * 60 if args.minutes else None)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(eng, "_stop", True))
        await eng.run()

    logging.info("mode=%s journal=%s", cfg.execution.mode, run_dir)
    asyncio.run(runner())
    journal.close()


if __name__ == "__main__":
    main()
