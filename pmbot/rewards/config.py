"""Config of the liquidity-rewards farmer (config/rewards.toml, section [rewards])."""
import tomllib
from dataclasses import dataclass, fields


@dataclass
class RewardsCfg:
    mode: str = "paper"
    journal_dir: str = "data/journal-rewards"
    state_file: str = "data/state/rewards_paper.json"
    kill_file: str = "STOP"
    # selection
    min_rate: float = 20.0               # $/day pool of the market
    min_days_left: float = 2.0           # stay away from markets close to resolution (outcome often known)
    price_lo: float = 0.10               # displayed YES price range we quote in
    price_hi: float = 0.90
    exclude_regex: str = (r"\bvs\.?\b|o/u|spread:|handicap|game \d|map \d|\bset \d|winner|match|"
                          r"up or down|kills|total (?:goals|points|rounds)")
    max_pages: int = 4                   # rewards collection pages (500 markets each, best pools first)
    max_candidates: int = 800            # how many of the largest pools to price (ranked by rate; ~2000 pools >= $20/d)
    max_markets: int = 30
    max_empty_frac: float = 0.5          # share of picks allowed where nobody else quotes in the reward zone
    quote_budget_usd: float = 1000.0     # pUSD all resting bids may tie up
    max_per_market_usd: float = 60.0     # pUSD both bids of one market may tie up
    reselect_min: float = 30.0
    # quoting
    dist_cents: float = 1.0              # our bid/ask distance from the reference price
    size_mult: float = 1.0               # order size = max(rewards min size, min order) * size_mult
    requote_min_s: float = 5.0           # do not move a resting order more often than this (crossing orders move at once)
    competitive_spread_mult: float = 2.0 # book counts as "quoted by others" if spread <= mult * max spread
    # risk
    min_fill_shares: float = 1.0         # fills at least this big start the cooldown and count toward the daily cap
    cooldown_min: float = 15.0           # no quotes in a market after a material fill
    max_fills_per_market_day: int = 3
    max_inventory_usd_per_market: float = 40.0   # stop bidding a side once its unhedged inventory costs this much
    feed_stale_s: float = 45.0           # no websocket message for this long -> quote nothing
    max_ref_disagree: float = 0.03       # no quotes while the last trade (<= trade_fresh_s old) is this far off the book
    trade_fresh_s: float = 600.0
    daily_loss_limit: float = 100.0      # trading loss of today (realised + marks) -> stop quoting until 00:00 UTC
    # bookkeeping
    sample_s: float = 60.0               # reward scoring sample period (Polymarket samples once a minute)
    settle_s: float = 300.0              # how often to look for resolved markets (Gamma)
    status_s: float = 300.0


def load_rewards_config(path, overrides=None):
    raw = tomllib.load(open(path, "rb"))
    sec = dict(raw.get("rewards") or {})
    for k in ("journal_dir",):
        if k in raw and k not in sec:
            sec[k] = raw[k]
    sec.update(overrides or {})
    known = {f.name for f in fields(RewardsCfg)}
    unknown = set(sec) - known
    if unknown:
        raise ValueError(f"unknown config key(s) in [rewards]: {sorted(unknown)}")
    cfg = RewardsCfg(**sec)
    if cfg.mode != "paper":
        raise ValueError("the rewards farmer is paper-only for now (live needs a separate, reviewed step)")
    return cfg
