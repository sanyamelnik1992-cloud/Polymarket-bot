"""In-memory market state shared by feeds, strategy and brokers."""
import math
import time
from collections import deque
from dataclasses import dataclass, field


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Book:
    bids: dict = field(default_factory=dict)  # price -> size
    asks: dict = field(default_factory=dict)
    last_rx: int = 0

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def clear(self):
        self.bids.clear()
        self.asks.clear()
        self.last_rx = 0


@dataclass
class Window:
    asset: str
    start: int                   # unix seconds
    slug: str
    condition_id: str
    token_up: str
    token_down: str
    end: int = 0
    tick: float = 0.01
    min_size: float = 5.0
    strike: float | None = None  # Chainlink TWAP at start ("price to beat")
    strike_src: str = ""         # "gamma" | "chainlink"
    winner: str | None = None

    def __post_init__(self):
        if not self.end:
            self.end = self.start + 300

    @property
    def key(self):
        return f"{self.asset}-{self.start}"

    def other(self, token):
        return self.token_down if token == self.token_up else self.token_up

    def side_of(self, token):
        return "U" if token == self.token_up else "D"


class AssetPrices:
    """Chainlink prints (as received from RTDS) and Binance mid with per-second sampling + EWMA vol."""

    def __init__(self, halflife_s=900.0):
        self.cl_by_sec = {}           # oracle second -> price
        self.cl_last = None           # (value, oracle_ts_ms, rx_ms)
        self.bn_hist = deque(maxlen=3000)  # (rx_ms, mid) recent ticks for nowcast lookups
        self.bn_last_rx = 0
        self.bn_sec_mid = {}          # second -> last mid of that second
        self._last_sec = None
        self._last_sec_mid = None
        self.var = None               # EWMA of squared 1s log returns
        self.alpha = 1 - 0.5 ** (1 / halflife_s)
        self.last_ret = 0.0           # most recent completed 1s log return

    # ---- Chainlink
    def on_chainlink(self, value, ts_ms, rx_ms):
        self.cl_by_sec[ts_ms // 1000] = value
        if self.cl_last is None or ts_ms >= self.cl_last[1]:
            self.cl_last = (value, ts_ms, rx_ms)
        if len(self.cl_by_sec) > 4000:
            for s in sorted(self.cl_by_sec)[:1000]:
                self.cl_by_sec.pop(s, None)

    def cl_twap(self, end_sec, lookback=60):
        """Mean of Chainlink prints over [end-lookback, end-1]; returns (value, seconds_present)."""
        xs = [self.cl_by_sec[s] for s in range(end_sec - lookback, end_sec) if s in self.cl_by_sec]
        return (sum(xs) / len(xs) if xs else None), len(xs)

    def cl_partial(self, from_sec, upto_sec):
        """Sum and count of Chainlink prints for seconds in [from_sec, upto_sec)."""
        xs = [self.cl_by_sec[s] for s in range(from_sec, upto_sec) if s in self.cl_by_sec]
        return sum(xs), len(xs)

    # ---- Binance
    def on_binance(self, mid, rx_ms):
        self.bn_last_rx = rx_ms
        if self.bn_hist and self.bn_hist[-1][1] == mid and rx_ms - self.bn_hist[-1][0] < 250:
            return  # unchanged mid: keep the history compact (staleness uses bn_last_rx)
        self.bn_hist.append((rx_ms, mid))
        sec = rx_ms // 1000
        if self._last_sec is None:
            self._last_sec, self._last_sec_mid = sec, mid
        elif sec != self._last_sec:
            # close the previous second(s); gaps are filled with the last mid (zero return)
            prev = self._last_sec_mid
            r = math.log(mid / prev) if prev else 0.0
            self.last_ret = r
            self._ewma(r * r)
            for _ in range(min(sec - self._last_sec - 1, 60)):
                self._ewma(0.0)
            self.bn_sec_mid[self._last_sec] = prev
            self._last_sec, self._last_sec_mid = sec, mid
            if len(self.bn_sec_mid) > 4000:
                for s in sorted(self.bn_sec_mid)[:1000]:
                    self.bn_sec_mid.pop(s, None)
        else:
            self._last_sec_mid = mid

    def _ewma(self, x):
        self.var = x if self.var is None else (1 - self.alpha) * self.var + self.alpha * x

    def seed_vol(self, closes):
        """Bootstrap the EWMA from historical 1s closes (oldest first)."""
        for a, b in zip(closes[:-1], closes[1:]):
            if a > 0 and b > 0:
                self._ewma(math.log(b / a) ** 2)

    def sigma(self):
        return math.sqrt(self.var) if self.var else None

    def bn_mid(self):
        return self.bn_hist[-1][1] if self.bn_hist else None

    def bn_mid_at(self, t_ms):
        """Binance mid as of local receive time t_ms (last tick at or before t)."""
        best = None
        for rx, mid in reversed(self.bn_hist):
            if rx <= t_ms:
                return mid
            best = mid
        return best

    def bn_move_since(self, ms):
        """log-return of Binance mid over the last `ms` milliseconds."""
        if not self.bn_hist:
            return 0.0
        now_rx, now_mid = self.bn_hist[-1]
        then = self.bn_mid_at(now_rx - ms)
        return math.log(now_mid / then) if then else 0.0


class State:
    def __init__(self, assets, halflife_s=900.0):
        self.assets = list(assets)
        self.prices = {a: AssetPrices(halflife_s) for a in self.assets}
        self.books = {}            # token -> Book
        self.windows = {}          # window key -> Window
        self.token_window = {}     # token -> Window
        self.feed_alive = {"clob": {}, "rtds": 0, "binance": 0}  # last message rx per feed
        self.cooldown_until = {a: 0 for a in self.assets}

    def add_window(self, w: Window):
        if w.key in self.windows:
            return self.windows[w.key]
        self.windows[w.key] = w
        self.token_window[w.token_up] = w
        self.token_window[w.token_down] = w
        self.books.setdefault(w.token_up, Book())
        self.books.setdefault(w.token_down, Book())
        return w

    def drop_window(self, key):
        w = self.windows.pop(key, None)
        if w:
            for t in (w.token_up, w.token_down):
                self.token_window.pop(t, None)
                self.books.pop(t, None)
