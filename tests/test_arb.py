import asyncio
import time

import pytest

from pmbot.arb_live import ArbTrader, leg_b_limit
from pmbot.fees import fee_per_share


def test_leg_b_limit_keeps_the_set_below_one():
    for pa in (0.10, 0.35, 0.50, 0.62, 0.90):
        lb = leg_b_limit(pa, 0.01)
        assert pa + fee_per_share(pa) + lb + fee_per_share(lb) <= 0.99 + 1e-9
        nxt = round(lb + 0.01, 2)
        assert pa + fee_per_share(pa) + nxt + fee_per_share(nxt) > 0.99


def test_paper_trader_buys_both_legs_once_and_settles(tmp_path, monkeypatch):
    tr = ArbTrader(str(tmp_path), 1, min_edge=0.02, usd_per_pair=5, usd_per_day=20)
    T15 = int(time.time()) - 650                      # inside the last 5 minutes of the 15m window
    p = {"asset": "btc", "T15": T15, "o15": 100.0, "o5": 101.0, "est": False,
         "m15": {"token_up": "U15", "token_down": "D15"}, "m5": {"token_up": "U5", "token_down": "D5"}}
    tr.pairs[("btc", T15)] = p
    tr.books = {"U15": {"b": {}, "a": {0.60: 50}}, "D5": {"b": {}, "a": {0.30: 8}}}   # set 0.90 + fees < 0.98
    trades = []

    async def scenario():
        tr.check()
        await asyncio.sleep(0.05)
        tr.check()                                    # the pair is not traded twice
        await asyncio.sleep(0.05)
    asyncio.run(scenario())
    lines = (tmp_path / "arb_trades.jsonl").read_text().splitlines()
    kinds = [l.split('"type": "')[1].split('"')[0] for l in lines]
    assert kinds.count("arb_start") == 1 and "arb_filled" in kinds
    rec = tr.open_sets[("btc", T15)]
    assert rec["a_shares"] == pytest.approx(rec["b_shares"]) and rec["sets"] >= 5
    assert rec["spent"] < rec["sets"] * 0.98                      # locked: pays >= 1 per set
