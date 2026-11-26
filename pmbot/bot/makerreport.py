"""The maker's periodic report (reports/95 section 10; owner, 26 Nov: "добавь какой то отчет по мейкеру в тг, например каждые
30 мин или час"): what the test did in the last `maker.report_every_s` seconds, written as ONE journal record `mm_report` that
pmbot.notify turns into a Telegram message (live only: the paper twin journals the record and stays silent).

The numbers that are not a difference of two cumulative counters are collected here, one `Interval` per report: windows quoted,
cancels by reason, fills (favourite / underdog side, our edge against the model at the fill), the markouts of those fills (the
side's fair value 5 / 30 s later), the windows that closed (result, paired or one-sided). pmbot/bot/fairmaker.py feeds it from
the places where those things happen and adds the rest of the record (counters by difference, position, day result, latency).
"""

# why of FairMaker._cancel_sides -> the report's group: "requote" = moved to follow the fair value, "pause" = a Binance jump,
# "gate" = a side blocked by a move against it, "end" = the window's quoting time is over, "limits" = our own limits (price band,
# inventory, open risk), "trouble" = something is wrong with a feed or the exchange (a human should look)
GROUPS = ("requote", "pause", "gate", "end", "limits", "trouble")
_END = {"time", "window_gone", "stop"}
_LIMITS = {"price", "side_full", "unpaired", "too_low"}


def cancel_group(why):
    """The report group of a cancel reason (any unknown reason counts as trouble: it must not hide)."""
    why = str(why)
    if why in ("requote", "pause", "gate"):
        return why
    if why in _END:
        return "end"
    if why in _LIMITS or why.startswith("risk"):
        return "limits"
    return "trouble"


def next_boundary(now, every):
    """The next wall-clock multiple of `every` seconds after `now`: a 30-minute report comes at :00 and :30 UTC."""
    every = float(every)
    return (int(now // every) + 1) * every


class Interval:
    """Accumulators of one report interval. `t0` = when it began (wall clock, s)."""

    def __init__(self, t0):
        self.t0 = float(t0)
        self.windows = set()                       # window keys where we placed a bid
        self.filled = set()                        # window keys where a bid was filled
        self.cancels = {g: 0 for g in GROUPS}
        self.f = {"n": 0, "shares": 0.0, "cost": 0.0, "fav": 0, "dog": 0, "up": 0.0, "dn": 0.0, "edge_w": 0.0, "edge_sh": 0.0}
        self.mo = {}                               # h (s) -> [sum(markout * shares), shares, n]
        self.closed = {"n": 0, "won": 0, "lost": 0, "pnl": 0.0, "paired": 0}

    def quote(self, key):
        self.windows.add(key)

    def cancel(self, why):
        self.cancels[cancel_group(why)] += 1

    def fill(self, key, side, price, size, fav=None, edge=None):
        """fav: the filled side was the model's favourite (None = unknown, e.g. an adopted order); edge: the side's fair value at
        the fill minus the price paid (None when the model had no value)."""
        f = self.f
        f["n"] += 1
        f["shares"] += size
        f["cost"] += price * size
        f["up" if side == "U" else "dn"] += size
        if fav is not None:
            f["fav" if fav else "dog"] += 1
        if edge is not None:
            f["edge_w"] += edge * size
            f["edge_sh"] += size
        self.filled.add(key)

    def markout(self, h, d, size):
        """d = the side's fair value h seconds after the fill minus at the fill (None = no value then)."""
        if d is None:
            return
        m = self.mo.setdefault(int(h), [0.0, 0.0, 0])
        m[0] += d * size
        m[1] += size
        m[2] += 1

    def settle(self, pnl, up, dn):
        c = self.closed
        c["n"] += 1
        c["won"] += pnl > 0
        c["lost"] += pnl < 0
        c["pnl"] += pnl
        c["paired"] += up > 0 and dn > 0

    def summary(self):
        """JSON-able record of the interval (cents are cents of a share: 0.01 = 1 c)."""
        f, c = self.f, self.closed
        return {
            "windows_quoted": len(self.windows), "windows_filled": len(self.filled),
            "cancels": dict(self.cancels),
            "fills": f["n"], "shares": round(f["shares"], 4), "cost": round(f["cost"], 4), "fav": f["fav"], "dog": f["dog"],
            "up": round(f["up"], 4), "dn": round(f["dn"], 4),
            "avg_price": round(f["cost"] / f["shares"], 4) if f["shares"] else None,
            "edge_c": round(100 * f["edge_w"] / f["edge_sh"], 2) if f["edge_sh"] else None,
            "mo_c": {str(h): round(100 * m[0] / m[1], 2) for h, m in sorted(self.mo.items()) if m[1]},
            "mo_n": {str(h): m[2] for h, m in sorted(self.mo.items()) if m[1]},
            "closed": c["n"], "won": c["won"], "lost": c["lost"], "pnl": round(c["pnl"], 4), "paired": c["paired"],
        }
