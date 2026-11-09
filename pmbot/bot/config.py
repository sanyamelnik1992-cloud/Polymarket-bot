"""Bot configuration (TOML file + defaults). Unknown keys are rejected so typos cannot silently disable a limit."""
import tomllib
from dataclasses import dataclass, field, fields


@dataclass
class StrategyCfg:
    assets: list = field(default_factory=lambda: ["btc", "eth", "sol"])  # XRP lost money in tests
    half_spread: float = 0.04        # bid = fair - h on Up and on Down
    quote_size: float = 10.0         # shares per quote (market minimum is 5)
    join_only: bool = True           # never bid above the current best bid (do not improve the market)
    start_s: float = 5.0             # quote from start+5s ...
    stop_s: float = 240.0            # ... until start+240s (last minute = TWAP lock-in, market is better there)
    p_min: float = 0.05              # only quote while fair P(Up) is inside [p_min, p_max]
    p_max: float = 0.95
    skew_per_share: float = 0.0005   # inventory skew of the bid price per net share
    requote_ticks: int = 1           # move a resting quote up only if target is >= this many ticks higher
    min_requote_ms: int = 300        # ... and not more often than this (moving DOWN is always immediate)
    vol_mult: float = 1.0
    model_weight: float = 1.0        # quote around w*model + (1-w)*book mid (1 = pure model)
    t_df: float = 4.0                # Student-t degrees of freedom of the TWAP model
    vol_halflife_s: float = 900.0
    max_open_price: float = 1.0      # a bid that does NOT reduce the net position (an opening leg) only at <= this:
                                     # an unpaired window then loses <= price*size and wins >= (1-price)*size.
                                     # Closing legs (they complete a pair) are not limited. 1.0 = off
    min_open_price: float = 0.0      # ... and only at >= this. A band symmetric around 0.5 lets both sides rest at once
                                     # (a pair can form) and opens nothing in the tails. 0.0 = off
    close_through_filters: bool = False  # an unpaired lot keeps its closing bid (other side, at the best bid) while
                                     # opening filters (calm market, model, p range, jumps, too late, losing-streak
                                     # pause...) are on: its fill only removes risk. Live 8 Nov: 4 of 6 losing
                                     # windows swung our way first, but the filters had pulled the closing bid
    close_stop_s: float = 290.0      # ... until start + this (opening bids still stop at stop_s)
    favorite_only: float = 0.0       # > 0: an opening bid only on the side priced >= 0.5 + this (the market's favourite =
                                     # ahead of the strike). All maker fills, BTC 8-9 Nov: ahead of the strike +1.3c a share
                                     # at 60 s, behind -1.0c (reports/46). Closing legs are not limited. 0 = off


@dataclass
class FilterCfg:
    cl_max_age_ms: int = 3500        # Chainlink (RTDS) print must be fresher than this (typical delivery ~1.4s)
    bn_max_age_ms: int = 5000        # Binance stream must have sent something recently
    book_max_age_ms: int = 20000     # CLOB stream for the token must be alive
    max_spread: float = 0.03         # skip a window whose Up/Down book is wider than this
    max_disagree: float = 0.20       # |fair - book mid| above this = probably our model is wrong (basis/strike)
    jump_sigmas: float = 6.0         # Binance 1s move > N sigma -> pull quotes for jump_cooldown_ms
    jump_cooldown_ms: int = 3000
    max_sigma_bp: float = 3.0        # per-second vol above this (bp/s) = too wild, do not quote
    min_strike_seconds: int = 50     # strike computed from >= this many Chainlink seconds (if not published)
    vol_spike_ratio: float = 0.0     # short (60s) / long sigma above this = volatility burst, do not quote (0=off)
    max_fair_move: float = 0.0       # fair P(Up) moved more than this over fair_move_window_ms = unstable (0=off)
    fair_move_window_ms: int = 5000
    toxic_shares: float = 0.0        # >= this many shares filled on one side within toxic_window_ms ... (0=off)
    toxic_fills: int = 0             # ... or >= this many separate fills (0=off)
    toxic_window_ms: int = 5000
    toxic_cooldown_ms: int = 30000   # ... blocks that side for this long
    max_loop_lag_ms: int = 0         # event loop late by more than this = our view of the market is old (0=off)
    min_queue_imbalance: float = 0.0  # do not bid a token whose best-level imbalance bid/(bid+ask) is below this:
                                      # a thin bid queue is about to be eaten (BTC 5m: I<0.1 -> bid drops 57% vs
                                      # rises 29% within 5 s, reports/40). 0 = off
    min_vol_5m_bp: float = 0.0       # skip calm markets: Chainlink realized vol of the last 10 min, in bp per 5 min,
                                     # below this = no quotes (calm windows lost most in both halves, reports/40 §6)
    queue_imbalance_hysteresis: float = 0.10  # ... re-enter only at min + this (each re-entry = back of the queue)


@dataclass
class RiskCfg:
    max_net_shares_per_window: float = 50.0
    max_cost_per_window: float = 30.0       # $ spent on one 5m window (both sides)
    max_open_worst_loss: float = 120.0      # $ worst-case loss across all unresolved windows (assets are correlated)
    daily_loss_limit: float = 50.0          # realised $ loss in a UTC day -> kill switch
    max_losing_windows_in_row: int = 8      # pause after this many losing windows in a row
    pause_after_losses_s: int = 1800
    max_actions_per_sec: float = 8.0        # order placements + cancels
    kill_file: str = "STOP"                 # create this file to cancel everything and stop
    state_file: str = ""                    # daily PnL + kill flag survive restarts (required in live mode)
    daily_loss_hard: bool = False           # daily_loss_limit counts from the day's best realized PnL (not from 0)
                                            # and an opening bid is placed only if losing it and every open lot
                                            # cannot pass the limit: the day never gives back more than the limit
    resume_next_day: bool = False           # after a daily-loss kill keep running idle and resume at 00:00 UTC
                                            # (paper controls of paired tests); live: false = stop, human restarts


@dataclass
class ExecCfg:
    mode: str = "paper"                     # "paper" | "live"
    place_latency_ms: int = 150             # paper: order becomes live this long after the decision
    cancel_latency_ms: int = 150            # paper: cancel takes effect this long after the decision
    paper_fill_at_touch: bool = False       # paper: also fill when a trade prints AT our price (front of queue)
    paper_queue: bool = False               # paper: join the back of the queue at our price (size shown at placement)
                                            # and fill from trades AT our price once that queue is used up
    tick_ms: int = 200                      # decision loop period
    fast_react: bool = True                 # a Binance move > fast_move_sigmas * sigma_1s wakes the loop at once
    fast_move_sigmas: float = 1.0
    gtd_min_lifetime_s: int = 185           # GTD expiration must be >= now+180s (exchange rule)
    heartbeat_s: float = 4.0                # live: order heartbeat period (server cancels all after 10s silence)
    hit_pause_ms: int = 2000                # live: cancel answered "matched" -> no new bid on that side for this long


@dataclass
class BotConfig:
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    filters: FilterCfg = field(default_factory=FilterCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    execution: ExecCfg = field(default_factory=ExecCfg)
    journal_dir: str = "data/journal"


def _apply(obj, data: dict, path: str):
    names = {f.name for f in fields(obj)}
    for k, v in data.items():
        if k not in names:
            raise ValueError(f"unknown config key: {path}{k}")
        cur = getattr(obj, k)
        if hasattr(cur, "__dataclass_fields__"):
            if not isinstance(v, dict):
                raise ValueError(f"config section {path}{k} must be a table")
            _apply(cur, v, f"{path}{k}.")
        else:
            if isinstance(cur, bool) and not isinstance(v, bool):
                raise ValueError(f"{path}{k} must be true/false")
            if isinstance(cur, (int, float)) and not isinstance(cur, bool) and not isinstance(v, (int, float)):
                raise ValueError(f"{path}{k} must be a number")
            setattr(obj, k, type(cur)(v) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else v)


def validate(cfg: BotConfig):
    s, r, e = cfg.strategy, cfg.risk, cfg.execution
    if e.mode not in ("paper", "live"):
        raise ValueError("execution.mode must be 'paper' or 'live'")
    if not 0 <= s.half_spread < 0.5:
        raise ValueError("strategy.half_spread must be in [0, 0.5)")
    if s.quote_size < 5:
        raise ValueError("strategy.quote_size must be >= 5 shares (market minimum)")
    if not 0 < s.p_min < s.p_max < 1:
        raise ValueError("strategy.p_min < p_max inside (0,1)")
    if not 0 <= s.start_s < s.stop_s <= 300:
        raise ValueError("strategy.start_s < stop_s <= 300")
    if not s.stop_s <= s.close_stop_s < 300:
        raise ValueError("strategy.stop_s <= close_stop_s < 300")
    for name in ("max_cost_per_window", "max_open_worst_loss", "daily_loss_limit", "max_net_shares_per_window"):
        if getattr(r, name) <= 0:
            raise ValueError(f"risk.{name} must be > 0")
    if e.mode == "live" and not r.state_file:
        raise ValueError("risk.state_file is required in live mode (daily loss + kill switch must survive restarts)")
    if e.mode == "live" and r.resume_next_day:
        raise ValueError("risk.resume_next_day is paper-only: a live bot stopped by the daily loss limit waits for a human")
    if r.max_cost_per_window > r.max_open_worst_loss:
        raise ValueError("risk.max_cost_per_window cannot exceed risk.max_open_worst_loss")
    bad = [a for a in s.assets if a not in ("btc", "eth", "sol", "xrp")]
    if bad:
        raise ValueError(f"unsupported assets: {bad}")
    return cfg


def load_config(path: str | None = None, overrides: dict | None = None) -> BotConfig:
    cfg = BotConfig()
    if path:
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f), "")
    if overrides:
        _apply(cfg, overrides, "")
    return validate(cfg)
