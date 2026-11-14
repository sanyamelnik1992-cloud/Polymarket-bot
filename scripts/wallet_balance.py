"""Real wallet balance for /status and the hourly report (reports/42 item 5: check live PnL against money).

  python scripts/wallet_balance.py [OUT]      OUT = data/state/wallet.json

Runs from pmbot-wallet.timer with the secrets env (/etc/pmbot/pmbot.env), so the report never needs them.
Writes only numbers: {"t", "usd" (collateral), "positions_value" (open positions at market), "rewards" (the exchange's
own liquidity-reward numbers per public market id)} or {"t", "error": <type>}.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def fetch():
    from polymarket import AsyncSecureClient
    from pmbot.config import DATA
    from pmbot.http import get_json
    c = await AsyncSecureClient.create(private_key=os.environ["PMBOT_PRIVATE_KEY"],
                                       wallet=os.environ.get("PMBOT_WALLET") or None)
    bal = await c.get_balance_allowance(asset_type="COLLATERAL")
    out = {"usd": round(bal.balance / 1e6, 4)}
    wallet = getattr(c, "wallet", None) or os.environ.get("PMBOT_WALLET")
    try:
        d = get_json(f"{DATA}/positions", {"user": str(wallet), "limit": 500})
        rows = d.get("data", []) if isinstance(d, dict) else d
        out["positions_value"] = round(sum(float(r.get("currentValue") or 0) for r in rows), 4)
    except Exception:  # noqa: BLE001 - the cash balance alone is still useful
        pass
    try:
        out["rewards"] = await rewards(c)
    except Exception as e:  # noqa: BLE001 - never at the cost of the balance
        out["rewards_error"] = type(e).__name__
    return out


async def rewards(c, days=3, now=None):
    """What the exchange itself says about liquidity rewards of this account (the live rewards test reads it,
    reports/68): our share per market right now and the $ credited per market for the last days. Market ids are public
    condition ids; nothing here identifies the wallet."""
    now = now or time.time()
    pct = await c.get_reward_percentages()
    out = {"pct": {str(k): round(float(v), 4) for k, v in list(pct.items())[:200] if v}, "earn": {}}
    for back in range(days):
        day = time.strftime("%Y-%m-%d", time.gmtime(now - back * 86400))
        m = {}
        async for e in c.list_user_earnings_for_day(date=day).iter_items():
            m[str(e.condition_id)] = round(m.get(str(e.condition_id), 0.0) + float(e.earnings), 6)
        out["earn"][day] = m
    return out


def main(path):
    rec = {"t": round(time.time())}
    try:
        rec.update(asyncio.run(fetch()))
    except Exception as e:  # noqa: BLE001
        rec["error"] = type(e).__name__          # the type only: an error text may echo request details
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(path + ".tmp", path)
    print(json.dumps(rec))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/state/wallet.json")
