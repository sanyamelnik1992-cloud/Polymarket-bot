"""End-to-end: a tiny synthetic recording through replay -> Engine.decide_once -> filters -> PaperBroker -> settle."""
import json

from pmbot.bot.config import load_config
from pmbot.bot.replay import run_replay

T0 = 1_800_000_000


def write(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def make_recording(d):
    write(d / "markets.jsonl", [{"slug": "btc-updown-5m-x", "asset": "btc", "window_start": T0, "condition_id": "c",
                                 "token_up": "tu", "token_down": "td"}])
    cl, bn, clob = [], [], []
    # Chainlink flat at 100 for 2 minutes before and during the window; Binance flat too
    for s in range(T0 - 120, T0 + 120):
        cl.append({"sym": "btc/usd", "ts": s * 1000, "v": 100.0, "rx": s * 1000 + 1400})
        bn.append({"sym": "BTCUSDT", "bid": 99.995, "ask": 100.005, "rx": s * 1000 + 50})
    k_u, k_d = f"btc-{T0}-U", f"btc-{T0}-D"
    # books from start-20s, refreshed every second so the feed stays alive: Up 0.49/0.51, Down 0.49/0.51
    for s in range(T0 - 20, T0 + 120):
        for k in (k_u, k_d):
            clob.append({"e": "book", "rx": s * 1000 + 10, "ts": s * 1000, "k": k,
                         "b": [[0.49, 100.0], [0.40, 100.0]], "a": [[0.51, 100.0], [0.60, 100.0]]})
    # a taker sweeps the Up bids down to 0.40 at start+60s (through our ~0.46 bid)
    clob.append({"e": "trade", "rx": (T0 + 60) * 1000 + 20, "ts": (T0 + 60) * 1000, "k": k_u, "p": 0.40,
                 "s": 50.0, "side": "SELL"})
    rows = sorted([(r["rx"], r) for r in clob], key=lambda x: x[0])
    write(d / "clob.jsonl", [r for _, r in rows])
    write(d / "chainlink.jsonl", cl)
    write(d / "binance.jsonl", bn)


def test_replay_end_to_end(tmp_path):
    make_recording(tmp_path)
    cfg = load_config("config/paper_v2.toml", {"strategy": {"assets": ["btc"]}})
    seed = {"btc": [100.0 * (1 + 0.00005 * ((i % 2) * 2 - 1)) for i in range(1800)]}
    eng = run_replay(str(tmp_path), cfg, str(tmp_path / "out"), {"btc-updown-5m-x": "Up"}, None, seed)
    assert eng.stats["placed"] > 0, "the bot should quote once filters pass"
    assert eng.stats["fills"] >= 1, "the sweep through our bid should fill us"
    pos = eng.risk.positions[f"btc-{T0}"]
    assert pos.up > 0 and pos.cost > 0
    ev = [json.loads(line) for line in open(tmp_path / "out" / "events.jsonl")]
    settle = [e for e in ev if e["type"] == "settle"]
    assert settle and settle[0]["winner"] == "Up" and settle[0]["pnl"] > 0
    assert not eng.risk.killed
