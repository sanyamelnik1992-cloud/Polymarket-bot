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
        # inventory limit on the net position: never let a fill take |net| above the limit (partial fills of
        # several orders used to: live 8 Nov, 10.97 shares on one side with a limit of 10)
        room = rc.max_net_shares_per_window - inv_sign * net
        size = 0 if room < 1e-9 else min(size, math.floor(room + 1e-9))
        # $ budget per window
        budget = rc.max_cost_per_window - pos.cost
        if px > 0:
            size = min(size, math.floor(budget / px))
        opening = inv_sign * net >= -1e-9          # this bid does not reduce our net position
        in_band = sc.min_open_price - 1e-9 <= px <= sc.max_open_price + 1e-9
        if opening and not in_band:
            size = 0                                # opening legs only inside the band; closing legs at any price
        elif not opening and not in_band and size > abs(net) + 1e-9:
            # a closing bid larger than the unpaired lot opens the rest outside the band (live 8 Nov: Down 1.92
            # unpaired, a 5-share Up bid at 0.65 would add 3.08 Up at 0.65). Allowed only if the pairs it locks pay
            # for that rest (Up at 0.15: the window can no longer lose); otherwise close only what is open
            up2, dn2 = pos.up + (size if side == "U" else 0.0), pos.down + (size if side == "D" else 0.0)
            if pos.cost + px * size - min(up2, dn2) > pos.worst_loss() + 1e-9:
                size = math.floor(abs(net) + 1e-9)
        if px < tick or px > 0.99 or size < max(w.min_size, 5):
            out[side] = None
        else:
            out[side] = (px, float(size))
    return out
