"""scripts/polybolt_probe.py: the statistics (the sockets themselves need the server and its credentials)."""
import importlib.util
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location("polybolt_probe", os.path.join(ROOT, "scripts", "polybolt_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def feeds(n=200, t0=1_794_000_000):
    px = {s: 100.0 + 0.01 * (s - t0) for s in range(t0, t0 + n)}
    pb = {("btc", s): (v, s * 1000 + 300) for s, v in px.items()}
    rt = {("btc", s): (v, s * 1000 + 1400) for s, v in px.items() if s != t0 + 50}       # RTDS dropped one second
    # the published TWAP stamped t averages the 60 prints that end 2 s earlier
    tw = {("btc", t): (sum(px[s] for s in range(t - 2 - 59, t - 2 + 1)) / 60, t * 1000 + 350) for t in range(t0 + 70, t0 + n)}
    return pb, rt, tw


def test_latency_lead_and_missing_seconds():
    pb, rt, tw = feeds()
    lat = probe.latency_stats(rt)["btc"]
    assert lat["median"] == 1400 and lat["missing"] == 1 and lat["n"] == 199
    assert probe.latency_stats(pb)["btc"]["median"] == 300 and probe.latency_stats(pb)["btc"]["missing"] == 0
    lead = probe.lead_stats(pb, rt)["btc"]
    assert lead["median"] == 1100 and lead["n"] == 199 and lead["same_value"] == 199


def test_twap_alignment_finds_the_averaging_window():
    pb, rt, tw = feeds()
    best = probe.twap_alignment(pb, tw)["btc"][0]
    assert best[0] == 2 and best[1] < 1e-6
    text = probe.report(pb, rt, tw)
    assert "d = 2" in text and "PolyBolt ahead of RTDS" in text and "seconds missing 1" in text
    assert probe.report({}, {}, {}).count("no prints received") == 3


def test_asset_of_symbol_forms():
    assert [probe.asset_of(s) for s in ("btcusd", "BTC/USD", "solusd", "xrp/usd")] == ["btc", "btc", "sol", "xrp"]
