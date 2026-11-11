"""5m live rule (+ insurance) and the 15m rule together, by UTC day and by recorder period (replays of 7-11 Nov).
usage: REPLAY_GRID=<grids GRID_P_MIN=0.5> python reports/65_tools/combo.py CANDS15.json"""
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "64_tools"))
from hedge_sim import hedge, limits, load_live, run  # noqa: E402

PER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111", "d1111b"]
from pmbot.fees import fee_per_share  # noqa: E402

g, ok, lim = load_live(PER)
e5 = limits(run(g, ok, lim).sort_values("t"), 5, 1e9).reset_index(drop=True)
e5 = hedge(e5, g, 0.65, lambda p: p - 0.20, slip=0.02)
e5["day"] = pd.to_datetime(e5["T"] + 300, unit="s").dt.strftime("%m-%d")
d = json.load(open(sys.argv[1]))
e15 = pd.DataFrame(d["entries"]).drop_duplicates(["a", "ws"]).sort_values(["ws", "s", "p"], ascending=[True, True, False])
e15 = e15.drop_duplicates("ws")                                            # one entry at a time (all 15m windows end together)
e15["pnl"] = 5 * (e15.won.astype(float) - e15.px - e15.px.map(fee_per_share))
e15["day"] = pd.to_datetime(e15.ws + 900, unit="s").dt.strftime("%m-%d")
for key, lab in (("period", "recorder period"), ("day", "UTC day")):
    t = pd.DataFrame({"5m now": e5.groupby(key).pnl.sum(), "5m + insurance": e5.groupby(key).pnl_h.sum(),
                      "15m": e15.groupby(key).pnl.sum()}).fillna(0.0)
    t["5m + 15m"] = t["5m now"] + t["15m"]
    t["5m + insurance + 15m"] = t["5m + insurance"] + t["15m"]
    t["trades 5m"] = e5.groupby(key).size()
    t["trades 15m"] = e15.groupby(key).size()
    t = t.fillna(0)
    if key == "period":
        t = t.loc[[p for p in PER if p in t.index]]
    print(f"== by {lab}")
    print(t.round(2).to_string())
    cols = ["5m now", "5m + insurance", "5m + 15m", "5m + insurance + 15m"]
    print("   total:", {c: round(t[c].sum(), 2) for c in cols}, " negative:", {c: int((t[c] < 0).sum()) for c in cols},
          " worst:", {c: round(t[c].min(), 2) for c in cols})
