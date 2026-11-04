"""Quote generation: two bids (Up and Down) below fair value, post-only, never better than the market."""
import math
from dataclasses import dataclass

from .config import RiskCfg, StrategyCfg
from .state import Book, Window


@dataclass
class Position:
    up: float = 0.0
    down: float = 0.0
    cost: float = 0.0          # $ spent (both sides)
    fee_eq: float = 0.0        # fee-equivalent of our maker fills (rebate basis)
    fills: int = 0

    @property
    def net(self):
        return self.up - self.down

    def worst_loss(self):
        """Loss if the outcome goes against the larger side: cost - shares paid in the worst state."""
        return self.cost - min(self.up, self.down)


def floor_tick(x, tick=0.01):
    return math.floor(x / tick + 1e-9) * tick


def target_quotes(w: Window, p_up: float, bu: Book, bd: Book, pos: Position, sc: StrategyCfg, rc: RiskCfg):
    """Returns {"U": (price, size) | None, "D": (price, size) | None}."""
    tick = 0.01
    net = pos.net
    out = {}
    for side, p_tok, book, inv_sign in (("U", p_up, bu, 1), ("D", 1 - p_up, bd, -1)):
        # skew: long Up -> bid less for Up, more for Down (and vice versa)
        px = floor_tick(p_tok - sc.half_spread - inv_sign * sc.skew_per_share * net, tick)
        ask, bid = book.best_ask(), book.best_bid()
        if ask is not None:
            px = min(px, round(ask - tick, 2))          # post-only: never cross
        if sc.join_only and bid is not None:
            px = min(px, bid)                            # never improve the market
        px = round(px, 2)
        size = sc.quote_size
        # inventory limit on the net position
        if inv_sign * net >= rc.max_net_shares_per_window:
            size = 0
        # $ budget per window
        budget = rc.max_cost_per_window - pos.cost
        if px > 0:
            size = min(size, math.floor(budget / px))
        if px < tick or px > 0.99 or size < max(w.min_size, 5):
            out[side] = None
        else:
            out[side] = (px, float(size))
    return out
