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
    t_df: float = 4.0                # Student-t degrees of freedom of the TWAP model
    vol_halflife_s: float = 900.0


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
    max_book_gap_ms: int = 0         # reserved


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


@dataclass
class ExecCfg:
    mode: str = "paper"                     # "paper" | "live"
    place_latency_ms: int = 150             # paper: order becomes live this long after the decision
    cancel_latency_ms: int = 150            # paper: cancel takes effect this long after the decision
    tick_ms: int = 200                      # decision loop period
    gtd_min_lifetime_s: int = 185           # GTD expiration must be >= now+180s (exchange rule)
    heartbeat_s: float = 4.0                # live: order heartbeat period (server cancels all after 10s silence)


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
    if not 0 < s.half_spread < 0.5:
        raise ValueError("strategy.half_spread must be in (0, 0.5)")
    if s.quote_size < 5:
        raise ValueError("strategy.quote_size must be >= 5 shares (market minimum)")
    if not 0 < s.p_min < s.p_max < 1:
        raise ValueError("strategy.p_min < p_max inside (0,1)")
    if not 0 <= s.start_s < s.stop_s <= 300:
        raise ValueError("strategy.start_s < stop_s <= 300")
    for name in ("max_cost_per_window", "max_open_worst_loss", "daily_loss_limit", "max_net_shares_per_window"):
        if getattr(r, name) <= 0:
            raise ValueError(f"risk.{name} must be > 0")
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
