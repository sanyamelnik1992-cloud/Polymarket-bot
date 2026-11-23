"""reports/87: the price-source race recorder (pmbot/feedrec.py, scripts/record_feeds.py) and its analysis (pmbot/feedrace.py,
scripts/feed_race.py)."""
import gzip
import importlib.util
import json
import os
import tomllib

import numpy as np
import pandas as pd
import pytest

from pmbot import feedrace as fr
from pmbot import feedrec as rec

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_parsers_read_every_venue():
    """Messages as the venues sent them on 23 Nov (OKX from its documentation: the host refuses US addresses)."""
    m = '{"stream":"btcusdt@bookTicker","data":{"u":1,"s":"BTCUSDT","b":"61307.90","B":"3.9","a":"61307.91","A":"3.5"}}'
    assert rec.parse_binance(m) == [("btc", 61307.90, 61307.91, None)]
    m = ('{"stream":"btcusdt@bookTicker","data":{"e":"bookTicker","u":1,"s":"BTCUSDT","b":"61280.80","B":"7.9","a":"61280.90",'
         '"A":"9.0","T":1795436876018,"E":1795436876019}}')
    assert rec.parse_binance(m, True) == [("btc", 61280.80, 61280.90, 1795436876019.0)]
    m = ('{"type":"ticker","product_id":"XRP-USD","price":"0.9381","best_bid":"0.9380","best_ask":"0.9381",'
         '"time":"2026-11-23T12:27:55.186727Z"}')
    (a, b, k, ts), = rec.parse_coinbase(m)
    assert (a, b, k) == ("xrp", 0.938, 0.9381) and ts == pytest.approx(1795436875186.727)
    assert rec.parse_coinbase('{"type":"subscriptions","channels":[]}') == []
    m = ('{"arg":{"channel":"bbo-tbt","instId":"SOL-USDT"},"data":[{"asks":[["72.31","55","0","2"]],"bids":[["72.30","57","0","2"]],'
         '"ts":"1670324386802","seqId":1}]}')
    assert rec.parse_okx(m) == [("sol", 72.30, 72.31, 1670324386802.0)]
    assert rec.parse_okx(m.replace('"SOL-USDT"', '"SOL-USDT-SWAP"'), rec.OKXF_SYM)[0][0] == "sol"
    assert rec.parse_okx("pong") == []
    bb = rec.BybitBook()
    m = ('{"topic":"orderbook.1.BTCUSDT","ts":1795436875964,"type":"snapshot","data":{"s":"BTCUSDT","b":[["61309.5","0.3"]],'
         '"a":[["61309.6","1.4"]],"u":1},"cts":1795436875961}')
    assert bb.parse(m) == [("btc", 61309.5, 61309.6, 1795436875961.0)]
    m2 = '{"topic":"orderbook.1.BTCUSDT","ts":1795436876000,"type":"delta","data":{"s":"BTCUSDT","b":[],"a":[["61309.7","1"]]}}'
    assert bb.parse(m2) == [("btc", 61309.5, 61309.7, 1795436876000.0)]          # an empty side keeps the last one
    m = ('{"channel":"ticker","type":"update","data":[{"symbol":"BTC/USD","bid":61242.5,"ask":61242.6,'
         '"timestamp":"2026-11-23T12:27:55.979994Z"}]}')
    assert rec.parse_kraken(m)[0][:3] == ("btc", 61242.5, 61242.6)
    assert rec.parse_kraken('{"channel":"heartbeat"}') == []
    m = ('{"jsonrpc":"2.0","method":"subscription","params":{"channel":"quote.SOL_USDC-PERPETUAL","data":{"timestamp":1795436875620,'
         '"instrument_name":"SOL_USDC-PERPETUAL","best_ask_price":72.234,"best_bid_price":72.233}}}')
    assert rec.parse_deribit(m) == [("sol", 72.233, 72.234, 1795436875620.0)]
    assert rec.parse_deribit('{"jsonrpc":"2.0","id":1,"result":["quote.BTC-PERPETUAL"]}') == []
    m = '{"channel":"bbo","data":{"coin":"BTC","time":1795436875583,"bbo":[{"px":"61292.0","sz":"0.1","n":1},{"px":"61293.0"}]}}'
    assert rec.parse_hyperliquid(m) == [("btc", 61292.0, 61293.0, 1795436875583.0)]
    m = '{"topic":"crypto_prices_chainlink","payload":{"symbol":"eth/usd","value":1721.5,"timestamp":1795436875000}}'
    assert rec.parse_chainlink(m) == [("eth", 1721.5, 1721.5, 1795436875000.0)]
    assert rec.parse_chainlink("") == []


def test_every_source_has_a_subscription_for_the_four_coins():
    s = rec.sources()
    assert set(s) == {"bn", "bnc", "bnf", "bnft", "cb", "okx", "okxf", "bybit", "bybitf", "kraken", "deribit", "hl"}
    for name, (url, subs, make, app_ping, proto_ping) in s.items():
        assert url.startswith("wss://") and callable(make())
        text = url + json.dumps(subs)
        for coin in ("BTC", "ETH", "SOL", "XRP"):
            assert coin in text.upper(), (name, coin)
    cfg = tomllib.load(open(os.path.join(ROOT, "config", "feedrec.toml"), "rb"))
    assert set(cfg["sources"]) <= set(s) | {"pm", "cl"} and {"bn", "pm"} <= set(cfg["sources"]) and cfg["until"].endswith("Z")
    assert "bnf" not in cfg["sources"] and "bnft" in cfg["sources"] and cfg["assets"] == ["btc", "eth"]   # the light set (23 Nov)


def test_writer_keeps_moves_heartbeats_and_every_polymarket_change(tmp_path):
    w = rec.Writer(str(tmp_path), min_bp=0.2, heartbeat_ms=1000)
    assert w.add(0.0, "bn", "btc", 60000.00, 60000.02)
    assert not w.add(10.0, "bn", "btc", 60000.00, 60000.02)            # unchanged
    assert not w.add(20.0, "bn", "btc", 60000.00, 60000.50)            # 0.04 bp: below the filter
    assert w.add(30.0, "bn", "btc", 60000.00, 60002.50)                # 0.21 bp from the last kept row
    assert w.add(1100.0, "bn", "btc", 60000.00, 60002.60)              # a second later any change is kept
    assert w.add(40.0, "pm", "btc", 0.51, 0.52, 39.0, 1795437000)
    assert w.add(41.0, "pm", "btc", 0.51, 0.53, 40.0, 1795437000)      # Polymarket: every change of the top
    assert not w.add(42.0, "pm", "btc", 0.51, None)
    w.mark(50.0, "lag", "", "23.9")
    w.mark(51.0, "sys", "okx", "", "error ConnectionResetError, reset")
    assert w.flush(now_ms=1795437000000.0) == 7
    assert [r.split(",")[0] for r in open(tmp_path / "20261123-12.csv").read().splitlines()][:3] == ["0.0", "30.0", "1100.0"]
    w.close()
    w.compress_old(None, True)
    (f,) = list(tmp_path.glob("*.csv.gz"))
    rows = gzip.open(f, "rt").read().splitlines()
    assert rows[0] == "0.0,bn,btc,60000,60000.02,," and rows[3] == "40.0,pm,btc,0.51,0.52,39,1795437000"
    assert rows[5] == "50.0,lag,,23.9,,," and rows[6].count(",") == 6                 # a comma in a message cannot shift columns
    df = fr.read(str(f))
    assert len(df) == 7 and fr.stalls(df) == [(50.0, 83.9)]


def _synthetic(lead_ms=80.0, pm_after_ms=400.0, t0=1795437000000.0):
    """Three venues and the Polymarket Up book around one 3 bp BTC jump: cb first, bnf + 30 ms, bn + lead_ms; the book moves
    pm_after_ms after cb."""
    rows = []
    for src, d in (("cb", 0.0), ("bnf", 30.0), ("bn", lead_ms)):
        for k in range(20):                                  # flat 100.00 for 2 s, then the jump, then flat again
            t = t0 - 2000 + 100 * k
            rows.append((t, src, "btc", 99.99, 100.01, None, ""))
        rows.append((t0 + d, src, "btc", 100.02, 100.04, None, ""))
        rows.append((t0 + d + 500, src, "btc", 100.02, 100.04, None, ""))
    for k in range(40):                                      # the Up book: 0.505 until pm_after_ms, then 0.525, with flicker
        t = t0 - 1500 + 100 * k
        mid = 0.505 if t < t0 + pm_after_ms else 0.525
        if k % 7 == 3:
            mid += 0.005                                     # a one-tick flicker the event's way is not a reaction
        rows.append((t, "pm", "btc", mid - 0.005, mid + 0.005, None, "1795436900"))
    return pd.DataFrame(rows, columns=fr.COLS).sort_values("rx")


def test_analysis_finds_the_first_source_the_lead_and_the_makers_reaction():
    ev = fr.analyse(_synthetic())
    assert len(ev) == 1
    e = ev.iloc[0]
    assert e["first"] == "cb" and e["n"] == 3 and e["sign"] == 1 and not e["stall"]
    assert (e["d_cb"], e["d_bnf"], e["d_bn"], e["pm"]) == (0.0, 30.0, 80.0, 400.0)
    text = fr.summary(ev, fr.feed_stats(_synthetic()), 0.0, 3.6e6)
    table = text.split("btc: 1 events")[1].splitlines()
    assert "settled >= 1 c the same way in 1)" in table[0]
    line = next(x for x in table if x.strip().startswith("cb "))
    assert "+80 ms" in line and "100 % of 1" in line                     # 80 ms ahead of bn; 400 - 0 > 185: beats the makers
    assert "100 % of 1" in next(x for x in table if x.strip().startswith("bn "))     # 400 - 80 = 320 > 185 as well
    assert "half-way to its new level 400 ms after the first source" in text
    late = fr.analyse(_synthetic(pm_after_ms=50.0))                   # the book settles 100 ms after cb: too late for a taker
    table = fr.summary(late, {}, 0.0, 3.6e6).split("btc: 1 events")[1].splitlines()
    assert "  0 % of 1" in next(x for x in table if x.strip().startswith("cb "))


def test_jumps_need_the_move_within_the_window_and_respect_the_quiet_time():
    t = np.array([-500.0, 0.0, 100.0, 200.0, 2000.0, 2100.0, 9000.0])
    mid = np.array([100.0, 100.0, 100.0, 100.03, 100.03, 100.0, 100.0])
    js = fr.jumps(t, mid, 2.0)
    assert [(x[0], x[1]) for x in js] == [(200.0, 1)]                    # the drop at 2100 comes inside the 3 s quiet time
    slow = fr.jumps(np.array([0.0, 400.0, 800.0]), np.array([100.0, 100.01, 100.02]), 2.0)
    assert slow == []                                                     # 1 bp per 400 ms: no jump within 300 ms
    # an opposite blip 60 ms into an event is not a new event (GAP_MS); one 5 s later is
    assert fr.events({"a": [(10.0, 1, 3.0)], "b": [(60.0, 1, 3.0), (5000.0, -1, -3.0)], "c": [(70.0, -1, -3.0)]}) == [
        (10.0, 1, {"a": 10.0, "b": 60.0}), (5000.0, -1, {"b": 5000.0})]


def test_feed_race_script_caches_finished_hours_and_writes_the_summary(tmp_path):
    spec = importlib.util.spec_from_file_location("feed_race", os.path.join(ROOT, "scripts", "feed_race.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data, out = tmp_path / "feeds", tmp_path / "out"
    data.mkdir()
    df = _synthetic()
    with gzip.open(data / "20261123-12.csv.gz", "wt") as f:
        df.to_csv(f, header=False, index=False)
    df.to_csv(data / "20261123-13.csv", header=False, index=False)      # the open hour stays plain CSV
    assert mod.main(str(data), str(out)) == 0
    assert (data / "events" / "20261123-12.csv").exists() and not (data / "events" / "20261123-13.csv").exists()
    text = (out / "summary.txt").read_text()
    assert "btc: 2 events" in text
    assert len(pd.read_csv(out / "events.csv.gz")) == 2
    assert mod.main(str(data), str(out)) == 0                            # the second run reads the cached hour
    assert "btc: 2 events" in (out / "summary.txt").read_text()


def test_recorder_unit_is_data_only_and_wired():
    from pmbot import notify, ops
    text = open(os.path.join(ROOT, "deploy", "pmbot-feedrec.service")).read()
    assert "record_feeds.py config/feedrec.toml" in text and "--live" not in text and "CPUWeight=100" in text
    assert "[Install]" not in text                                       # never enabled at boot
    assert "pmbot-feedrec" in ops.ALL_UNITS and "pmbot-feedrec" in ops.RESEARCH_UNITS and "pmbot-feedrec" in notify.BOTS
    inst = open(os.path.join(ROOT, "deploy", "install_units.sh")).read()
    assert " pmbot-feedrec " in inst and "pmbot-feedrec" in inst.split("START_ONCE=")[1].splitlines()[0]
    assert " pmbot-feedrec " in open(os.path.join(ROOT, "deploy", "autoupdate.sh")).read()
    assert "scripts/feed_race.py data/feeds" in open(os.path.join(ROOT, "deploy", "push_reports.sh")).read()


def test_feeds_note_counts_the_recorded_hours(tmp_path, monkeypatch):
    from pmbot import ops
    monkeypatch.setattr(ops, "REPO", str(tmp_path))
    assert ops.feeds_note() == ""
    d = tmp_path / "data" / "feeds"
    d.mkdir(parents=True)
    (d / "20261123-12.csv.gz").write_bytes(b"x" * 3_000_000)
    (d / "20261123-13.csv").write_bytes(b"x" * 1_000_000)
    assert ops.feeds_note() == "записано 4 МБ (2 ч)" and ops.rec_note("pmbot-feedrec") == ops.feeds_note()


def test_price_gate_skips_size_only_updates_and_the_book_feed_follows_the_running_window():
    g = rec.PriceGate(rec.parse_binance, rec.BN_KEYS)
    m = '{"stream":"btcusdt@bookTicker","data":{"u":1,"s":"BTCUSDT","b":"61307.90","B":"3.9","a":"61307.91","A":"3.5"}}'
    assert g(m) and g(m.replace('"B":"3.9"', '"B":"4.1"')) == []          # only the size changed: no JSON, no row
    assert g(m.replace('"b":"61307.90"', '"b":"61307.80"'))[0][1] == 61307.80
    assert g(m.replace("btcusdt", "ethusdt").replace("BTCUSDT", "ETHUSDT"))[0][0] == "eth"   # per stream
    import time as _t
    from pmbot.bot.state import State, Window
    spec = importlib.util.spec_from_file_location("record_feeds", os.path.join(ROOT, "scripts", "record_feeds.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    st = State(["btc"])
    now = int(_t.time())
    cur = now - now % 300
    for s0 in (cur, cur + 300):
        st.add_window(Window(asset="btc", start=s0, slug="s", condition_id=str(s0), token_up=f"u{s0}", token_down=f"d{s0}"))
    f = mod.RecClobFeed(st, "btc", rec.Writer.__new__(rec.Writer), lambda: True)
    want = f.wanted()
    assert f"u{cur}" in want and not any(t.startswith("d") for t in want)
    assert (f"u{cur + 300}" in want) == (now >= cur + 297)               # the next window only from 3 s before its start


def test_futures_trades_and_the_overload_guard():
    m = '{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1795440000100,"s":"BTCUSDT","p":"61280.80","q":"0.01","T":1795440000098}}'
    g = rec.PriceGate(rec.parse_binance_trade, ('"p":"',))
    assert g(m) == [("btc", 61280.8, 61280.8, 1795440000098.0)] and g(m.replace('"q":"0.01"', '"q":"0.02"')) == []
    assert rec.sources()["bnft"][0].startswith("wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade")
    spec = importlib.util.spec_from_file_location("record_feeds", os.path.join(ROOT, "scripts", "record_feeds.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    o = mod.Overload(window_s=60, max_share=0.25)
    assert not any(o.add(1000.0 * k, 100.0) for k in range(60))        # 100 ms stalls once a second: 10 % of the minute
    assert o.add(61_000.0, 10_000.0)                                     # + a 10 s stall: 15.9 s of the last minute = stop
