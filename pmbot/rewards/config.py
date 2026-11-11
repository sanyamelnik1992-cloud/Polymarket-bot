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
    min_rate: float = 5.0                # $/day pool of the market (the ranking is by expected reward, this only trims)
    min_expected_reward: float = 1.0     # $/day our quote must be expected to earn in a market (payouts under $1 are not paid)
    min_days_left: float = 2.0           # stay away from markets close to resolution (outcome often known)
    price_lo: float = 0.10               # displayed YES price range we quote in
    price_hi: float = 0.90
    exclude_regex: str = (r"\bvs\.?\b|o/u|spread:|handicap|game \d|map \d|\bset \d|winner|match|"
                          r"up or down|kills|total (?:goals|points|rounds)")
    max_pages: int = 10                  # rewards collection pages (500 markets each, best pools first)
    max_candidates: int = 400            # how many of the best candidates (by the official estimate) get a book lookup
    max_markets: int = 45
    max_empty_frac: float = 0.5          # share of picks allowed where nobody else quotes in the reward zone
    quote_budget_usd: float = 1000.0     # pUSD all resting bids may tie up
    max_per_market_usd: float = 60.0     # pUSD both bids of one market may tie up
    reselect_min: float = 10.0           # empty pools are found by other makers within hours (reports/68): look often
    # quoting
    dist_cents: float = 2.0              # our bid/ask distance from the reference price when others score in the zone
    alone_frac: float = 0.85             # ... and when nobody else scores: this share of the max spread (a lone maker
                                         # takes the whole pool whatever its score, so stay as far out as still counts)
    size_mult: float = 1.0               # order size = max(rewards min size, min order) * size_mult
    requote_min_s: float = 30.0          # do not move a resting order more often than this (crossing orders move at once)
    requote_ticks: int = 1               # ... and keep it while the target is at most this many ticks away
    min_order_age_s: float = 30.0        # an order scores only after resting this long (market field r.moas, 30 s on
                                         # 96 of 100 markets checked); used when the market's own value is unknown
    competitive_spread_mult: float = 2.0 # book counts as "quoted by others" if spread <= mult * max spread
    # hedge
    hedge: bool = True                   # after a fill buy the other outcome at once and merge the pair back to $1
                                         # (what the largest reward earners do: 97 % of their exits within 5 s)
    max_hedge_cost: float = 0.06         # ... if the pair costs at most 1 + this per share, taker fee included
    hedge_latency_ms: float = 300.0      # the hedge order reaches the book this long after our fill
    fee_rate: float = 0.05               # taker fee rate when the market's own is unknown (0.04-0.05 on most, 0 geopolitics)
    # risk
    min_fill_shares: float = 1.0         # fills at least this big start the cooldown and count toward the daily cap
    cooldown_min: float = 10.0           # no quotes in a market after a material fill
    max_fills_per_market_day: int = 3
    max_inventory_usd_per_market: float = 40.0   # stop bidding a side once its unhedged inventory costs this much
    feed_stale_s: float = 45.0           # no websocket message for this long -> quote nothing
    max_ref_disagree: float = 0.03       # no quotes while the last trade (<= trade_fresh_s old) is this far off the book
    trade_fresh_s: float = 600.0
    daily_loss_limit: float = 50.0       # trading loss of today (realised + marks) -> stop quoting until 00:00 UTC
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
    if not 0 < cfg.alone_frac < 1:
        raise ValueError("rewards.alone_frac must be in (0, 1): the lone-maker distance as a share of the max spread")
    if cfg.dist_cents <= 0 or cfg.max_hedge_cost < 0 or cfg.min_order_age_s < 0:
        raise ValueError("rewards: dist_cents > 0, max_hedge_cost >= 0, min_order_age_s >= 0")
    return cfg
