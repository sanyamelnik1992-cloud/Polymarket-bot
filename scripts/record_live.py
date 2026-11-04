"""Record live 5m Up/Down books + Chainlink + Binance prices.

usage: python scripts/record_live.py OUT_DIR [duration_minutes]
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.recorder import Recorder  # noqa: E402

if __name__ == "__main__":
    out = sys.argv[1]
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 60
    asyncio.run(Recorder(out, duration_s=minutes * 60).run())
