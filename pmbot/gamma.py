"""Market discovery for the recurring 5m Up/Down crypto markets."""
import json
from .config import GAMMA, WINDOW
from .http import get_json


def window_start(ts: float, window: int = WINDOW) -> int:
    return int(ts) - int(ts) % window


def slug_for(asset: str, start: int, dur: str = "5m") -> str:
    return f"{asset}-updown-{dur}-{start}"


def fetch_event(slug: str):
    d = get_json(f"{GAMMA}/events", {"slug": slug})
    return d[0] if d else None


def parse_event(e: dict) -> dict:
    """Flatten an Up/Down event into the fields we need."""
    m = e["markets"][0]
    toks = json.loads(m["clobTokenIds"])
    outs = json.loads(m["outcomes"])
    prices = json.loads(m["outcomePrices"]) if m.get("outcomePrices") else [None, None]
    meta = e.get("eventMetadata") or {}
    up_idx = outs.index("Up")
    winner = None
    if m.get("closed") and prices[0] is not None:
        p_up = float(prices[up_idx])
        if p_up in (0.0, 1.0):
            winner = "Up" if p_up == 1.0 else "Down"
    return {
        "slug": e["slug"],
        "event_id": e["id"],
        "condition_id": m["conditionId"],
        "token_up": toks[up_idx],
        "token_down": toks[1 - up_idx],
        "start": e.get("startTime") or m.get("eventStartTime"),
        "end": m["endDate"],
        "closed": bool(m.get("closed")),
        "winner": winner,
        "price_to_beat": meta.get("priceToBeat"),
        "final_price": meta.get("finalPrice"),
        "volume": float(m.get("volume") or 0),
        "tick": m.get("orderPriceMinTickSize"),
        "fee_rate": (m.get("feeSchedule") or {}).get("rate"),
        "twap_lookback": (m.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds"),
    }
