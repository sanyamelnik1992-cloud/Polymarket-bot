"""Weather markets: validate METAR reconstruction, find when trading happens, and test observation-based edges.

  * dead bucket: its upper bound is below the max already observed today -> cannot win (model-free)
  * leading bucket late in the local day: contains the max so far; how often does it win vs its price?

usage: python scripts/analyze_weather.py WEATHER_DIR
"""
import os
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.fees import fee_per_share  # noqa: E402
from pmbot.weather import STATION_TZ, parse_temp_c, qualifying  # noqa: E402

pd.set_option("display.width", 220)
pd.set_option("display.max_rows", 200)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
PUBLISH_DELAY_S = 300   # assume an observation is usable 5 minutes after its timestamp


def obs_table(metar):
    m = metar.copy()
    m["t"] = pd.to_datetime(m.valid, utc=True)
    m["temp_c"] = m.metar.map(parse_temp_c)
    m = m.dropna(subset=["temp_c"])
    m["station"] = m.station.where(m.station.str.len() == 4, "K" + m.station)  # IEM drops the K for US sites
    out = []
    for st, g in m.groupby("station"):
        if st not in STATION_TZ:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        g = g.sort_values("t").copy()
        lt = g.t.dt.tz_convert(tz)
        g["ldate"] = lt.dt.strftime("%Y-%m-%d")
        g["lhour"] = lt.dt.hour + lt.dt.minute / 60
        out.append(g)
    return pd.concat(out)


def event_date(s):
    mon, day, year = s.split("-")
    return f"{int(year):04d}-{MONTHS[mon]:02d}-{int(day):02d}"


def main(d):
    mk = pd.read_parquet(f"{d}/events.parquet")
    tr = pd.read_parquet(f"{d}/trades.parquet")
    met = pd.read_parquet(f"{d}/metar_routine.parquet" if os.path.exists(f"{d}/metar_routine.parquet")
                          else f"{d}/metar.parquet")
    ob = obs_table(met)
    mk = mk.dropna(subset=["station", "lo"])
    mk["ldate"] = mk.date.map(event_date)
    # ---------------------------------------------------------------- 1. reconstruction
    mx = ob.groupby(["station", "ldate"]).temp_c.max().rename("max_c").reset_index()
    ev = mk.merge(mx, on=["station", "ldate"], how="left")
    ev["q"] = [qualifying(c, u) if pd.notna(c) else np.nan for c, u in zip(ev.max_c, ev.unit)]
    ev["pred_win"] = (ev.lo <= ev.q) & (ev.q <= ev.hi)
    per_ev = ev.groupby("event").apply(lambda x: pd.Series({
        "has_obs": x.q.notna().all(), "agree": bool(((x.won == 1) == x.pred_win).all()),
        "winner": x.loc[x.won == 1, "bucket"].iloc[0] if (x.won == 1).any() else None}))
    ok = per_ev[per_ev.has_obs]
    print(f"== reconstruction: events {len(per_ev)}, with METAR {len(ok)}, winner bucket agrees {ok.agree.mean():.3f}")
    bad = ok[~ok.agree].index.tolist()
    print("   disagreements:", bad[:15])
    # ---------------------------------------------------------------- 2. when do people trade
    t = tr.merge(mk[["condition_id", "event", "station", "ldate", "lo", "hi", "unit", "won", "bucket", "end",
                     "token_yes"]], on="condition_id")
    # express every fill in YES terms: BUY NO @q == SELL YES @1-q, SELL NO @q == BUY YES @1-q
    is_no = t.token_id != t.token_yes
    t["price"] = np.where(is_no, 1 - t.price, t.price)
    t["side"] = np.where(is_no, np.where(t.side == "BUY", "SELL", "BUY"), t.side)
    t["ts"] = pd.to_datetime(t.timestamp, unit="s", utc=True)
    t["end_ts"] = pd.to_datetime(t.end, utc=True)
    t["h_after_end"] = (t.ts - t.end_ts).dt.total_seconds() / 3600
    t["notional"] = t.price * t["size"]
    t["lhour"] = [ts.tz_convert(ZoneInfo(STATION_TZ[s])).hour + ts.tz_convert(ZoneInfo(STATION_TZ[s])).minute / 60
                  for ts, s in zip(t.ts, t.station)]
    t["ldate_trade"] = [ts.tz_convert(ZoneInfo(STATION_TZ[s])).strftime("%Y-%m-%d") for ts, s in zip(t.ts, t.station)]
    t["phase"] = np.where(t.ldate_trade < t.ldate, "before_day", np.where(t.ldate_trade == t.ldate, "on_day", "after_day"))
    print("\n== volume by phase (local date of the trade vs observation date):")
    print((t.groupby("phase").notional.sum() / t.notional.sum()).round(3).to_dict())
    print("   share of notional traded after the stated end (12:00 UTC):", round(t[t.h_after_end > 0].notional.sum() / t.notional.sum(), 3))
    print("   hours after end of last trade, per event (median):", round(t.groupby("event").h_after_end.max().median(), 2))
    # ---------------------------------------------------------------- 3. state of each bucket at trade time
    ob_sorted = {k: g.sort_values("t") for k, g in ob.groupby(["station", "ldate"])}
    fl = []
    for (st, ld), g in t.groupby(["station", "ldate"]):
        o = ob_sorted.get((st, ld))
        g = g.sort_values("ts")
        if o is None:
            fl.append(pd.Series(np.nan, index=g.index))
            continue
        avail = o.t + pd.Timedelta(seconds=PUBLISH_DELAY_S)
        cm = o.temp_c.cummax().values
        idx = np.searchsorted(avail.values, g.ts.values, side="right") - 1
        so_far = np.where(idx >= 0, cm[np.maximum(idx, 0)], np.nan)
        fl.append(pd.Series(so_far, index=g.index))
    t["max_so_far_c"] = pd.concat(fl)
    t["q_so_far"] = [qualifying(c, u) if pd.notna(c) else np.nan for c, u in zip(t.max_so_far_c, t.unit)]
    t["state"] = np.select([t.q_so_far.isna(), t.hi < t.q_so_far, (t.lo <= t.q_so_far) & (t.q_so_far <= t.hi)],
                           ["no_obs", "dead", "leading"], "above")
    sign = np.where(t.side == "BUY", 1.0, -1.0)
    t["fee_ps"] = fee_per_share(t.price, 0.05)
    t["taker_pnl"] = (sign * (t.won - t.price) - t.fee_ps) * t["size"]
    print("\n== trades on the observation day by bucket state:")
    on = t[t.phase == "on_day"]
    print(on.groupby("state").apply(lambda x: pd.Series({
        "fills": len(x), "notional": x.notional.sum(), "win_rate": x.won.mean(), "avg_price": x.price.mean(),
        "taker_pnl": x.taker_pnl.sum(), "buy_share": (x.side == "BUY").mean()})).round(3).to_string())
    dead = on[on.state == "dead"]
    print(f"\n   dead-bucket check: {len(dead)} fills, of which won={int(dead.won.sum())} "
          f"(must be 0 if the floor logic is right)")
    dbuy = dead[dead.side == "BUY"]
    print(f"   taker BUYs of dead YES: {len(dbuy)} fills, ${dbuy.notional.sum():,.0f} paid for $0 "
          f"(the sellers' gain); by price: {dbuy.price.describe(percentiles=[.5, .9]).round(3).to_dict()}")
    # ---------------------------------------------------------------- 4. leading bucket by local hour
    lead = on[on.state == "leading"].copy()
    lead["hb"] = pd.cut(lead.lhour, [0, 9, 12, 14, 16, 18, 20, 24])
    print("\n== leading bucket (contains max so far): realised win rate vs price, by local hour of the trade")
    print(lead.groupby("hb", observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "events": x.event.nunique(), "avg_price": np.average(x.price, weights=x["size"]),
        "win_rate": np.average(x.won, weights=x["size"]),
        "buy_taker_roi": ((x.won - x.price - x.fee_ps) * x["size"])[x.side == "BUY"].sum() /
        max(x[x.side == "BUY"].notional.sum(), 1)})).round(3).to_string())
    # strategy: buy the leading bucket when local hour >= H and price <= P_max (taker at observed asks)
    print("\n== rule: BUY leading bucket at observed taker-buy prices, local hour >= H, price <= pmax")
    rows = []
    for H in (14, 15, 16, 17, 18):
        for pmax in (0.80, 0.90, 0.95, 0.98):
            s = lead[(lead.lhour >= H) & (lead.price <= pmax) & (lead.side == "BUY")]
            if s.empty:
                continue
            pnl = ((s.won - s.price - s.fee_ps) * s["size"])
            per_ev_pnl = pnl.groupby(s.event).sum()
            tt = per_ev_pnl.mean() / per_ev_pnl.std() * np.sqrt(len(per_ev_pnl)) if len(per_ev_pnl) > 2 else np.nan
            rows.append({"H": H, "pmax": pmax, "fills": len(s), "events": s.event.nunique(),
                         "notional": s.notional.sum(), "pnl": pnl.sum(), "roi": pnl.sum() / s.notional.sum(),
                         "win_rate": s.won.mean(), "t_by_event": tt})
    print(pd.DataFrame(rows).round(3).to_string())
    t.drop(columns=["ts", "end_ts"]).to_parquet(f"{d}/trades_enriched.parquet")


if __name__ == "__main__":
    main(sys.argv[1])
