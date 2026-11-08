"""Risk manager: exposure caps, daily loss kill switch, losing-streak pause, action rate limit, kill file."""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from .config import RiskCfg
from .strategy import Position


class RateLimiter:
    def __init__(self, per_sec: float, burst: float | None = None, clock=time.monotonic):
        self.rate = per_sec
        self.cap = burst or max(per_sec * 2, 1)
        self.tokens = self.cap
        self.clock = clock
        self.t = clock()

    def allow(self, n=1.0):
        now = self.clock()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RiskManager:
    def __init__(self, rc: RiskCfg, journal=None, clock=time.time):
        self.rc = rc
        self.journal = journal
        self.clock = clock      # seconds
        self.positions = defaultdict(Position)   # window key -> Position
        self.unresolved = set()                  # window keys with a position not yet settled
        self.realized_by_day = defaultdict(float)
        self.realized_total = 0.0
        self.rebates_total = 0.0
        self.losing_streak = 0
        self.paused_until = 0.0
        self.killed = False
        self.kill_reason = ""
        self.killed_day = None                   # UTC day of the kill
        self.settled = []                        # recently settled window keys (persisted: a restart must not
                                                 # settle a window again from the wallet's leftover tokens)
        self.kill_kind = ""                      # "daily" (loss limit) | "file" (STOP) | "fatal" (broker) | ...
        self.provisional = {}                    # window key -> expected PnL counted at the window end (the official
                                                 # result arrives ~5 min later and replaces it)
        self.peak_by_day = defaultdict(float)    # UTC day -> best realized PnL of the day (>= 0)
        self.limiter = RateLimiter(rc.max_actions_per_sec, clock=clock)
        self._load_state()

    # ------------------------------------------------------------ persistence (survives restarts)
    def _load_state(self):
        p = getattr(self.rc, "state_file", "")
        if not p or not os.path.exists(p):
            return
        try:
            d = json.load(open(p))
        except (OSError, ValueError):
            return
        self.realized_by_day.update(d.get("realized_by_day", {}))
        self.peak_by_day.update(d.get("peak_by_day", {}))
        for day, v in d.get("realized_by_day", {}).items():       # older state files: the peak is at least now
            self.peak_by_day[day] = max(self.peak_by_day.get(day, 0.0), v, 0.0)
        self.settled = list(d.get("settled", []))[-500:]
        if d.get("killed_day") == self.today():
            self.killed, self.kill_reason = True, d.get("kill_reason", "killed earlier today")
            self.killed_day, self.kill_kind = d["killed_day"], d.get("kill_kind", "daily")

    def _save_state(self):
        p = getattr(self.rc, "state_file", "")
        if not p:
            return
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"realized_by_day": dict(self.realized_by_day), "peak_by_day": dict(self.peak_by_day),
                       "killed_day": (self.killed_day or self.today()) if self.killed else None,
                       "kill_reason": self.kill_reason, "kill_kind": self.kill_kind,
                       "settled": self.settled[-500:]}, f)
        os.replace(tmp, p)

    # ------------------------------------------------------------ state
    @staticmethod
    def day(ts=None):
        return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%d")

    def today(self):
        return self.day(self.clock())

    def open_worst_loss(self):
        return sum(self.positions[k].worst_loss() for k in self.unresolved)

    def day_pnl(self, day=None):
        """Realized PnL of the day plus expected losses of ended windows not settled yet."""
        day = day or self.today()
        return self.realized_by_day[day] + sum(min(v, 0.0) for v in self.provisional.values())

    def day_drawdown(self, day=None):
        """Loss counted against the daily limit: from 0, or from the day's best realized PnL (daily_loss_hard)."""
        day = day or self.today()
        peak = max(self.peak_by_day.get(day, 0.0), 0.0) if self.rc.daily_loss_hard else 0.0
        return peak - self.day_pnl(day)

    def day_room(self):
        """$ the day may still lose before the daily stop, after a total loss of every open lot."""
        open_w = sum(self.positions[k].worst_loss() for k in self.unresolved if k not in self.provisional)
        return self.rc.daily_loss_limit - self.day_drawdown() - open_w

    def kill(self, reason, kind="other"):
        if not self.killed:
            self.killed = True
            self.kill_reason = reason
            self.kill_kind = kind
            self.killed_day = self.today()
            if self.journal:
                self.journal.log("kill", reason=reason, kill_kind=kind)
            self._save_state()

    def check_kill_file(self):
        if self.rc.kill_file and os.path.exists(self.rc.kill_file):
            self.kill(f"kill file {self.rc.kill_file} present", kind="file")
        return self.killed

    @property
    def stays_alive_when_killed(self):
        """Paper controls wait for the next UTC day instead of exiting (only after a daily-loss kill)."""
        return self.rc.resume_next_day and self.kill_kind == "daily"

    def _maybe_resume(self):
        if self.killed and self.stays_alive_when_killed and self.killed_day != self.today():
            if self.journal:
                self.journal.log("resume", reason="new UTC day", was=self.kill_reason)
            self.killed, self.kill_reason, self.kill_kind, self.killed_day = False, "", "", None
            self.losing_streak, self.paused_until = 0, 0.0
            self._save_state()

    # ------------------------------------------------------------ gates
    def can_quote(self, now=None):
        now = now or self.clock()
        self._maybe_resume()
        if self.killed or self.check_kill_file():
            return False, self.kill_reason
        if self.day_drawdown(self.day(now)) >= self.rc.daily_loss_limit - 1e-9:
            peak = self.peak_by_day.get(self.day(now), 0.0) if self.rc.daily_loss_hard else 0.0
            self.kill(f"daily loss limit {self.rc.daily_loss_limit} reached"
                      + (f" (from the day's best {peak:+.2f})" if peak > 0 else ""), kind="daily")
            return False, self.kill_reason
        if now < self.paused_until:
            return False, "losing_streak_pause"
        return True, ""

    def clamp_size(self, key, price, size, resting_notional=0.0, no_new_risk=False, resting_opening=None):
        """Reduce a new bid so worst-case open loss + all resting bids stay under the global cap.
        no_new_risk = the bid closes an unpaired lot and its fill cannot raise the worst case: never limited.
        daily_loss_hard: an opening bid must also fit the day's remaining loss budget (a total loss of it and of every
        open lot cannot pass the daily stop)."""
        if price <= 0:
            return 0.0
        if no_new_risk:
            return float(size)
        room = self.rc.max_open_worst_loss - self.open_worst_loss() - resting_notional
        if self.rc.daily_loss_hard:
            room = min(room, self.day_room() - (resting_notional if resting_opening is None else resting_opening))
        if room <= 0:
            return 0.0
        return float(min(size, int(room / price + 1e-9)))

    def allow_action(self):
        return self.limiter.allow()

    # ------------------------------------------------------------ events
    def on_fill(self, key, side, price, size, fee_eq):
        p = self.positions[key]
        if side == "U":
            p.up += size
        else:
            p.down += size
        p.cost += price * size
        p.fee_eq += fee_eq
        p.fills += 1
        self.unresolved.add(key)

    def is_settled(self, key):
        return key in self.settled

    def _count_result(self, pnl):
        if pnl < 0:
            self.losing_streak += 1
            if self.losing_streak >= self.rc.max_losing_windows_in_row:
                self.paused_until = self.clock() + self.rc.pause_after_losses_s
                if self.journal:
                    self.journal.log("pause", losing_streak=self.losing_streak, until=self.paused_until)
                self.losing_streak = 0
        elif pnl > 0:
            self.losing_streak = 0

    def on_provisional(self, key, winner):
        """The window ended and its book shows `winner`; the official result comes ~5 min later. Counts the
        expected result for the losing streak and (losses only) for the daily stop right now."""
        p = self.positions.get(key)
        if p is None or key not in self.unresolved or key in self.settled or key in self.provisional:
            return None
        pnl = (p.up if winner == "Up" else p.down) - p.cost
        self.provisional[key] = pnl
        self._count_result(pnl)
        return pnl

    def on_settle(self, key, winner, rebate_share=0.2, ts=None):
        p = self.positions.get(key)
        counted = self.provisional.pop(key, None) is not None
        if p is None or key not in self.unresolved or key in self.settled:
            self.unresolved.discard(key)
            return None
        payoff = p.up if winner == "Up" else p.down
        rebate = rebate_share * p.fee_eq
        pnl = payoff - p.cost + rebate
        day = self.day(ts or self.clock())
        self.realized_by_day[day] += pnl
        self.peak_by_day[day] = max(self.peak_by_day.get(day, 0.0), self.realized_by_day[day])
        self.realized_total += pnl
        self.rebates_total += rebate
        self.unresolved.discard(key)
        if key not in self.settled:
            self.settled.append(key)
        self._save_state()
        if not counted:                       # the streak already counted this window at its end
            self._count_result(pnl)
        return {"pnl": pnl, "payoff": payoff, "cost": p.cost, "rebate": rebate, "up": p.up, "down": p.down}
