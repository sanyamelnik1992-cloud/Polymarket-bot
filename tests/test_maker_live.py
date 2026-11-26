"""The LIVE maker test (pmbot/bot/maker_live.py + fairmaker live mode, reports/94): REAL MONEY code, so its gates, limits and
failure handling are pinned here. The fake exchange has NO cancel_all: the wallet is shared with the lock bots."""
import asyncio
import dataclasses as dc
import inspect
import math
import os
import time
import types

import pytest

from pmbot import notify, ops
from pmbot.bot.fairmaker import (LIVE_CAPS, FairMaker, load_maker_config, load_maker_live_config)
from pmbot.bot.live import CONFIRM_PHRASE, LiveGateError
from pmbot.bot.lock import LockBot
from pmbot.bot.maker_live import MakerLiveBroker, MakerLiveCfg
from pmbot.bot.state import Book, Window

ROOT = os.path.join(os.path.dirname(__file__), "..")
LIVE = os.path.join(ROOT, "config", "maker_live.toml")
SAFE = os.path.join(ROOT, "config", "maker_safe.toml")
ENV = {"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0xabc"}
OK_GEO = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": False, "country": "IE"})  # noqa: E731
WALLET = "0xOurWallet"


def run(coro):
    return asyncio.run(coro)


class J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append((kind, kw))

    def of(self, kind):
        return [kw for k, kw in self.ev if k == kind]


class Pager:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        async def gen():
            for x in self.items:
                yield x
        return gen()


class FakeClient:
    """The exchange as the broker sees it. NO cancel_all on purpose: a call would fail the test with an AttributeError."""

    def __init__(self, balance_usd=300.0):
        self.balance = int(balance_usd * 1e6)
        self.wallet = WALLET
        self.calls = []
        self.open = {}                   # order id -> kwargs
        self.n = 0
        self.place_error = None
        self.cancel_error = None
        self.matched = {}                # order id -> shares the exchange says were matched
        self.status = {}
        self.refuse_cancel = set()       # ids the exchange answers "not canceled: matched"

    async def get_balance_allowance(self, asset_type):
        return types.SimpleNamespace(balance=self.balance)

    async def send_order_heartbeat(self, heartbeat_id=""):
        self.calls.append("hb")
        return types.SimpleNamespace(heartbeat_id="h1")

    async def place_limit_order(self, **kw):
        self.calls.append(("place", kw))
        if self.place_error:
            raise self.place_error
        self.n += 1
        self.open[f"o{self.n}"] = kw
        return types.SimpleNamespace(ok=True, order_id=f"o{self.n}", status="live")

    async def cancel_orders(self, order_ids):
        self.calls.append(("cancel", tuple(order_ids)))
        if self.cancel_error:
            raise self.cancel_error
        done = tuple(o for o in order_ids if o not in self.refuse_cancel and self.open.pop(o, None) is not None)
        nc = {o: "matched orders can't be canceled" for o in order_ids if o in self.refuse_cancel}
        for o in nc:
            self.open.pop(o, None)
        return types.SimpleNamespace(canceled=done, not_canceled=nc)

    async def cancel_market_orders(self, market=None, asset_id=None, token_id=None):
        self.calls.append(("sweep", asset_id))
        ids = [k for k, v in self.open.items() if v["asset_id"] == asset_id]
        for k in ids:
            del self.open[k]
        return types.SimpleNamespace(canceled=tuple(ids), not_canceled={})

    async def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return types.SimpleNamespace(size_matched=str(self.matched.get(order_id, 0)), status=self.status.get(order_id, "LIVE"))

    def list_open_orders(self, asset_id=None, **kw):
        return Pager([k for k, v in self.open.items() if v["asset_id"] == asset_id])

    async def get_current_rebates(self, date, maker_address):
        self.calls.append(("rebates", date, maker_address))
        return (types.SimpleNamespace(rebated_fees_usdc="0.42"),)

    async def close(self):
        self.calls.append("close")


def broker(client=None, journal=None, fills=None, unfills=None, **lc):
    fills = fills if fills is not None else []
    b = MakerLiveBroker(MakerLiveCfg(**lc), on_fill=fills.append, on_unfill=(unfills.append if unfills is not None else None),
                        journal=journal or J(), client=client or FakeClient(), geoblock_get=OK_GEO, env=ENV)
    b.stream_ok = True
    b.hb_ok_at = b.mono()
    b.balance_usd = 300.0
    b.wallet = WALLET.lower()
    return b


def trade(tid, status, *makers):
    return types.SimpleNamespace(id=tid, status=status, matched_at=None,
                                 maker_orders=[types.SimpleNamespace(order_id=o, matched_amount=str(q), price=str(px),
                                                                     maker_address=addr, asset_id=tok)
                                               for o, q, px, addr, tok in makers])


# ---------------------------------------------------------------- config: gates and caps
def test_live_config_needs_the_live_mode_and_the_flag_and_stays_a_test():
    cfg, mm, lc = load_maker_live_config(LIVE, live_flag=True)
    assert cfg.execution.mode == "live" and cfg.lock.assets == ["btc"] and mm.ref == "min"
    assert (mm.shares, mm.max_side_shares, mm.max_unpaired) == (5, 10, 5) and lc.max_shares == 5
    assert lc.max_resting_usd == 10.0 and cfg.risk.daily_loss_limit == 5.0 and cfg.risk.resume_next_day is False
    with pytest.raises(LiveGateError):
        load_maker_live_config(LIVE, live_flag=False)                     # the config says live, the flag is missing
    with pytest.raises(ValueError):
        load_maker_config(LIVE)                                           # the paper launcher refuses a live config
    with pytest.raises(ValueError):
        load_maker_live_config(SAFE, live_flag=True)                      # --live with a paper config
    with pytest.raises(ValueError):
        load_maker_config(SAFE, overrides={"live": {"max_shares": 5}})    # [live] in a paper config
    for bad in ({"maker": {"shares": 10, "max_side_shares": 20}, "live": {"max_shares": 10}},
                {"maker": {"max_side_shares": 20}}, {"maker": {"max_unpaired": 15, "max_side_shares": 15}},
                {"live": {"max_resting_usd": 20.0}}, {"live": {"max_resting_usd": 8.0}},
                {"risk": {"daily_loss_limit": 20.0}}, {"risk": {"max_open_usd": 50.0}}, {"risk": {"resume_next_day": True}},
                {"live": {"reserve_usd": 10.0}}, {"lock": {"assets": ["btc", "eth"]}}, {"live": {"max_orders_hour": 6000}},
                {"live": {"heartbeat_s": 10.0}}, {"live": {"ttl_s": 100}}, {"live": {"typo": 1}}, {"risk": {"kill_file": ""}}):
        with pytest.raises(ValueError):
            load_maker_live_config(LIVE, live_flag=True, overrides=bad)
    assert LIVE_CAPS["shares"] == 5.0 and LIVE_CAPS["max_resting_usd"] == 10.0          # the caps themselves


def test_live_config_runs_the_rule_of_its_paper_twin():
    """pmbot-maker-safe is the twin: the same [lock] and quoting rule; only the size caps of the test and the money differ."""
    live, lm, lc = load_maker_live_config(LIVE, live_flag=True)
    paper, pm = load_maker_config(SAFE)
    assert live.lock == paper.lock
    keep = ("max_side_shares", "max_unpaired")
    assert {k: v for k, v in dc.asdict(lm).items() if k not in keep} == {k: v for k, v in dc.asdict(pm).items() if k not in keep}
    assert live.journal_dir != paper.journal_dir and live.risk.state_file != paper.risk.state_file
    assert notify.TWINS["pmbot-maker-safe"] == "pmbot-maker-live" and "pmbot-maker-live" in notify.QUIET_SETTLES
    assert ops.bot_paths("pmbot-maker-live") == ("data/journal-maker-live", "data/state/maker_live.json", "live")
    assert ops.unit_is_live("pmbot-maker-live") and "5 акций" in notify.limits("pmbot-maker-live")


def test_unit_is_live_and_started_once_on_the_owners_word():
    """26 Nov, the owner: "запускай" -> started ONCE by install_units.sh (a marker in data/state; after its day stop or an /off
    it stays stopped), never enabled at boot."""
    text = open(os.path.join(ROOT, "deploy", "pmbot-maker-live.service")).read()
    assert "scripts/run_maker.py --config config/maker_live.toml --live" in text and "CPUWeight=500" in text
    assert "pmbot-maker-live" in ops.BOT_UNITS and "pmbot-maker-live" in notify.BOTS
    inst = open(os.path.join(ROOT, "deploy", "install_units.sh")).read()
    assert " pmbot-maker-live " in inst                                   # Telegram may start / stop it
    assert "pmbot-maker-live" in inst.split("START_ONCE=")[1].splitlines()[0].replace('"', " ").split()
    assert "pmbot-maker-live" not in inst.split("RETIRED=")[1].splitlines()[0].replace('"', " ").split()
    assert "systemctl enable " not in inst
    assert " pmbot-maker-live " in open(os.path.join(ROOT, "deploy", "autoupdate.sh")).read()


# ---------------------------------------------------------------- broker
def test_start_gates():
    b = broker()
    b._user_stream = lambda: asyncio.sleep(3600)
    b._upkeep_loop = lambda: asyncio.sleep(3600)

    async def go():
        assert await b.start()
        assert "hb" in b.client.calls and b.wallet == WALLET.lower()
        b._stopping = True
        for t in b._tasks:
            t.cancel()
    run(go())
    with pytest.raises(LiveGateError):
        run(MakerLiveBroker(MakerLiveCfg(), client=FakeClient(), geoblock_get=OK_GEO, env={}).start())
    with pytest.raises(LiveGateError):                                    # below bids + the reserve of the other live bots
        run(MakerLiveBroker(MakerLiveCfg(), client=FakeClient(balance_usd=50), geoblock_get=OK_GEO, env=ENV).start())
    blocked = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": True, "country": "US"})  # noqa: E731
    with pytest.raises(LiveGateError):
        run(MakerLiveBroker(MakerLiveCfg(), client=FakeClient(), geoblock_get=blocked, env=ENV).start())


def test_orders_are_post_only_gtd_buys_inside_the_limits():
    b = broker()
    end = int(time.time()) + 250

    async def go():
        oid = await b.place("U", "btc-1", "U", 0.47, 5, 0, end_s=end)
        kw = b.client.calls[-1][1]
        assert oid and kw["side"] == "BUY" and kw["post_only"] is True and kw["price"] == "0.47" and kw["size"] == "5.00"
        assert kw["expiration"] >= max(time.time() + 180, end + 60) - 1        # GTD: the SDK wants >= now + 180
        n = len(b.client.calls)
        assert await b.place("U", "btc-1", "U", 0.46, 5, 0, end_s=end) is None   # one resting order per token
        assert await b.place("D", "btc-1", "D", 0.50, 10, 0, end_s=end) is None  # more than 5 shares
        assert await b.place("D", "btc-1", "D", 0.97, 5, 0, end_s=end) is None   # price outside [0.05, 0.95]
        assert await b.place("D", "btc-1", "D", 0.50, 4.5, 0, end_s=end) is None  # not whole / under the minimum
        assert len(b.client.calls) == n                                   # none of these reached the exchange
        assert await b.place("D", "btc-1", "D", 0.52, 5, 0, end_s=end)
        assert await b.place("X", "btc-2", "U", 0.40, 5, 0, end_s=end) is None   # a third resting order
        assert b.open_usd() == pytest.approx(0.47 * 5 + 0.52 * 5)
    run(go())
    b2 = broker(max_resting_usd=4.0)                                      # $3.50 rests: another $1.50 would be $5.00
    assert run(b2.place("U", "k", "U", 0.70, 5, 0)) and run(b2.place("D", "k", "D", 0.30, 5, 0)) is None


def test_unhealthy_broker_places_nothing():
    for attr, val, why in (("stream_ok", False, "user_stream_down"), ("hb_ok_at", -100.0, "heartbeat_down"),
                           ("fatal", "HTTP 401", "broker_fatal"), ("balance_usd", 30.0, "low_balance")):
        b = broker()
        setattr(b, attr, val if attr != "hb_ok_at" else b.mono() + val)
        assert b.health() == (False, why)
        assert run(b.place("U", "k", "U", 0.5, 5, 0)) is None and not any(c[0] == "place" for c in b.client.calls
                                                                             if isinstance(c, tuple))
    b = broker()
    b.suspect.add("U")
    assert b.health() == (False, "order_outcome_unknown")


def test_a_cross_refusal_pauses_that_token_briefly_and_the_hour_cap_stops_the_run():
    b = broker(cross_pause_ms=300)
    e = RuntimeError("invalid post-only order: order crosses book")
    e.status = 400
    b.client.place_error = e
    assert run(b.place("U", "k", "U", 0.5, 5, 0)) is None and b.stats["crossed"] == 1 and not b.suspect
    b.client.place_error = None
    assert run(b.place("U", "k", "U", 0.5, 5, 0)) is None                 # paused
    b.pause["U"] = 0.0
    assert run(b.place("U", "k", "U", 0.5, 5, 0))
    b = broker(max_orders_hour=2)
    assert run(b.place("U", "k", "U", 0.5, 5, 0)) and run(b.place("D", "k", "D", 0.4, 5, 0))
    run(b.cancel(list(b.orders), 0))
    assert run(b.place("U", "k", "U", 0.5, 5, 0)) is None and b.fatal and b.health()[0] is False


def test_an_unanswered_order_stops_quoting_until_its_token_is_swept():
    b = broker()
    b.client.place_error = TimeoutError("read timeout")
    assert run(b.place("U", "btc-1", "U", 0.5, 5, 0, end_s=time.time() + 200)) is None
    assert b.suspect == {"U"} and b.health() == (False, "order_outcome_unknown")
    b.client.place_error = None
    b.client.open["ghost"] = {"asset_id": "U"}                            # it did land at the exchange
    b.backoff_until = 0.0

    async def go():
        await b.resolve_suspect()
    run(go())
    assert ("sweep", "U") in b.client.calls and "ghost" not in b.client.open and not b.suspect
    assert b.health() == (True, "")


def test_cancel_by_id_and_a_fill_that_beat_the_cancel_is_counted_at_once():
    fills = []
    b = broker(fills=fills)

    async def go():
        a = await b.place("U", "btc-1", "U", 0.47, 5, 0)
        d = await b.place("D", "btc-1", "D", 0.50, 5, 0)
        b.client.refuse_cancel.add(d)
        b.client.matched[d] = 5
        late = await b.cancel([a, d], 0)
        assert late == {d} and not b.orders
        assert ("cancel", (a, d)) in b.client.calls                       # one call for both sides
        assert ("get_order", d) in b.client.calls
        assert [(f["side"], f["size"], f["status"]) for f in fills] == [("D", 5.0, "RECONCILED")]
    run(go())


def test_stop_cancels_its_own_orders_and_never_everything():
    b = broker()
    end = time.time() + 200

    async def go():
        await b.place("U", "btc-1", "U", 0.47, 5, 0, end_s=end)
        b.client.open["ghost"] = {"asset_id": "D"}                        # unknown to us, on a token of ours
        b.tokens["D"] = ("btc-1", "D", end)
        b.client.open["lockbot"] = {"asset_id": "OTHER"}                  # not ours (another market): untouched
        await b.stop()
    run(go())
    assert not b.orders and set(b.client.open) == {"lockbot"}
    assert ("sweep", "U") in b.client.calls and ("sweep", "D") in b.client.calls and ("sweep", "OTHER") not in b.client.calls
    assert "close" in b.client.calls


def test_fills_come_from_the_stream_once_and_a_failed_trade_is_taken_back():
    fills, unfills = [], []
    b = broker(fills=fills, unfills=unfills)
    oid = run(b.place("U", "btc-1", "U", 0.47, 5, 0))
    b.handle_trade(trade("t1", "MATCHED", (oid, 2, 0.47, WALLET, "U")))
    b.handle_trade(trade("t1", "CONFIRMED", (oid, 2, 0.47, WALLET, "U")))   # the same trade again: not twice
    assert [f["size"] for f in fills] == [2.0] and b.orders[oid].remaining == pytest.approx(3.0)
    b.handle_trade(trade("t1", "FAILED", (oid, 2, 0.47, WALLET, "U")))
    assert [(u["order_id"], u["size"]) for u in unfills] == [(oid, 2.0)] and b.orders[oid].remaining == pytest.approx(5.0)
    b.handle_trade(trade("t2", "MATCHED", ("someone", 5, 0.53, "0xother", "U")))    # we were the taker: their order
    assert len(fills) == 1


def test_a_fill_of_an_order_we_lost_track_of_is_adopted_by_our_address():
    fills = []
    b = broker(fills=fills)
    b.tokens["D"] = ("btc-7", "D", time.time() + 100)                     # a token we quoted (the place call never answered)
    b.handle_trade(trade("t9", "MATCHED", ("lost", 5, 0.41, WALLET.upper(), "D")))
    b.handle_trade(trade("t9", "MINED", ("lost", 5, 0.41, WALLET, "D")))
    b.handle_trade(trade("t8", "MATCHED", ("lost2", 5, 0.41, WALLET, "NOT_OURS")))  # our address on a token we never quoted
    assert [(f["key"], f["side"], f["size"], f["price"]) for f in fills] == [("btc-7", "D", 5.0, 0.41)]
    assert b.stats["adopted"] == 1


def test_reconcile_counts_a_fill_the_stream_missed_once():
    fills = []
    b = broker(fills=fills)
    oid = run(b.place("U", "btc-1", "U", 0.47, 5, 0))
    b.client.matched[oid] = 5
    assert run(b.reconcile()) == pytest.approx(5.0) and oid not in b.orders
    b.handle_trade(trade("t1", "MATCHED", (oid, 5, 0.47, WALLET, "U")))   # the stream event arrives later
    assert [f["size"] for f in fills] == [5.0]


def test_rebate_of_the_day_is_journalled():
    j = J()
    b = broker(journal=j)
    assert run(b.rebates()) == pytest.approx(0.42)
    assert j.of("mm_rebate")[0]["usd"] == pytest.approx(0.42)


# ---------------------------------------------------------------- the maker on the live broker
class FakeBroker:
    """A live-like broker for the maker: records calls, answers what the test says."""
    mode = "live"

    def __init__(self):
        self.calls = []
        self.place_ret = "auto"
        self.late = set()
        self.ok = (True, "")
        self.n = 0
        self.quiesced = 0
        self.stats = {"placed": 0}

    def health(self):
        return self.ok

    async def place(self, token, key, side, price, size, now_ms, expiration_s=None, queue_ahead=None, end_s=None):
        self.calls.append(("place", token, side, price, size, end_s))
        if self.place_ret != "auto":
            return self.place_ret
        self.n += 1
        return f"x{self.n}"

    async def cancel(self, oids, now_ms):
        self.calls.append(("cancel", tuple(oids)))
        return {o for o in oids if o in self.late}

    async def quiesce(self, why="stop"):
        self.quiesced += 1
        return 0

    def gc(self, now_ms):
        pass

    def on_public_trade(self, *a, **kw):
        pass


def live_bot(tmp_path, lead_bp=10.0, rel=100):
    over = {"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": str(tmp_path / "STOP")}}
    cfg, mm, lc = load_maker_live_config(LIVE, live_flag=True, overrides=over)
    j = J()
    br = FakeBroker()
    bot = FairMaker(cfg, mm, j, broker=br)
    now = int(time.time() * 1000)
    start = now // 1000 - rel
    w = bot.st.add_window(Window(asset="btc", start=start, slug="s", condition_id="c", token_up="U", token_down="D"))
    px = bot.st.prices["btc"]
    for s in range(start - 70, start):
        px.bn_sec_mid[s] = 100.0
    px.bn_hist.append((now - 5000, 100.0 * math.exp(lead_bp * 1e-4)))
    bot.bsig["btc"].r2.extend([1e-8 * 10] * 360)
    bot.bsig["btc"].last_sec = now // 1000 - 2
    bot.st.books["U"] = Book(bids={0.55: 100.0}, asks={0.90: 50.0})
    bot.st.books["D"] = Book(bids={0.09: 100.0}, asks={0.45: 50.0})
    bot.st.feed_alive["clob"]["btc"] = now
    bot.st.feed_alive["binance"] = now
    return bot, j, br, w, now


def test_a_live_maker_needs_its_limits_and_builds_the_live_broker(tmp_path):
    over = {"risk": {"state_file": str(tmp_path / "s.json")}}
    cfg, mm, lc = load_maker_live_config(LIVE, live_flag=True, overrides=over)
    with pytest.raises(LiveGateError):
        FairMaker(cfg, mm, J())
    bot = FairMaker(cfg, mm, J(), live=lc)
    assert isinstance(bot.broker, MakerLiveBroker) and bot.broker.lc is lc and bot.live
    assert bot.broker.on_fill == bot._on_mm_fill and bot.broker.on_unfill == bot._on_mm_unfill


def test_a_refused_bid_is_not_believed_resting(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    br.place_ret = None
    run(bot.decide_once(now))
    assert [c[0] for c in br.calls].count("place") == 2 and not bot.mm_bids
    assert all(c[5] == w.end for c in br.calls if c[0] == "place")       # the window end goes with every order (GTD)


def test_an_unhealthy_broker_pulls_and_places_nothing(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    assert len(bot.mm_bids) == 2
    br.ok = (False, "user_stream_down")
    br.calls.clear()
    run(bot.decide_once(now + 200))
    assert br.calls == [("cancel", ("x1", "x2"))] and not bot.mm_bids
    assert bot.last_why[w.key] == "broker:user_stream_down"


def test_a_jump_pulls_both_sides_in_one_call(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    br.calls.clear()
    bot.st.prices["btc"].bn_hist.append((now + 100, 100.0 * math.exp(40e-4)))   # +30 bp in 2 s
    run(bot.decide_once(now + 150))
    assert br.calls[0] == ("cancel", ("x1", "x2")) and not bot.mm_bids and j.of("mm_pull")


def test_no_rebid_in_the_pass_where_the_cancel_found_a_fill(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    up = bot.mm_bids[(w.key, "U")]["oid"]
    br.late.add(up)
    bot.mm_bids[(w.key, "U")]["price"] = 0.30                            # far from the target: the next pass moves it
    br.calls.clear()
    run(bot.decide_once(now + 200))
    assert ("cancel", (up,)) in br.calls and not any(c[0] == "place" and c[2] == "U" for c in br.calls)
    br.calls.clear()
    run(bot.decide_once(now + 400))                                        # the pass after: bid again
    assert any(c[0] == "place" and c[2] == "U" for c in br.calls)


def test_a_failed_cancel_keeps_the_bid_and_tries_again(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))

    async def boom(oids, now_ms):
        br.calls.append(("cancel", tuple(oids)))
        raise RuntimeError("timeout")
    br.cancel = boom
    bot.mm_bids[(w.key, "U")]["price"] = 0.30
    br.calls.clear()
    run(bot.decide_once(now + 200))
    assert (w.key, "U") in bot.mm_bids and not any(c[0] == "place" for c in br.calls) and j.of("error")


def test_stopping_pulls_every_bid_before_the_settle_wait(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    run(bot._on_stopping())
    assert not bot.mm_bids and br.quiesced == 1 and ("cancel", ("x1", "x2")) in br.calls
    src = inspect.getsource(LockBot.run)
    assert src.index("_on_stopping") < src.index("wait_for(settle")       # not after the up-to-15-minute settle wait


def test_the_book_mid_of_ref_min_leaves_out_our_own_bids(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    bot.st.books["U"] = Book(bids={0.60: 5.0, 0.55: 100.0}, asks={0.70: 5.0, 0.72: 50.0})
    bot.mm_bids[(w.key, "U")] = {"oid": "x9", "price": 0.60, "size": 5.0, "token": "U", "p": 0.7, "t": now}
    bot.mm_bids[(w.key, "D")] = {"oid": "x8", "price": 0.30, "size": 5.0, "token": "D", "p": 0.3, "t": now}
    assert bot._mid_wo_own("U", "D") == pytest.approx((0.55 + 0.72) / 2)  # our 0.60 bid and our Down bid's 0.70 ask left out
    bot.live = False                                                      # paper bids are not in the public book
    assert bot._mid_wo_own("U", "D") == pytest.approx((0.60 + 0.70) / 2)


def test_a_failed_trade_removes_those_shares(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    bot._on_mm_fill({"order_id": "x1", "key": w.key, "side": "U", "price": 0.47, "size": 5.0, "ts": now})
    bot._on_mm_fill({"order_id": "x2", "key": w.key, "side": "D", "price": 0.50, "size": 5.0, "ts": now})
    assert bot.held(w.key) == (5.0, 5.0)
    bot._on_mm_unfill({"order_id": "x1", "key": w.key, "side": "U", "price": 0.47, "size": 5.0})
    assert bot.held(w.key) == (0.0, 5.0) and j.of("mm_unfill")[0]["not_found"] == 0.0


def test_one_fill_record_per_fill_and_a_failed_trade_sweeps_its_token(tmp_path):
    j = J()
    b = broker(journal=j)
    oid = run(b.place("U", "btc-1", "U", 0.47, 5, 0))
    b.handle_trade(trade("t1", "MATCHED", (oid, 5, 0.47, WALLET, "U")))
    assert not j.of("fill") and j.of("mm_exec")[0]["size"] == 5.0     # the maker journals "fill" itself: never twice
    b.handle_trade(trade("t1", "FAILED", (oid, 5, 0.47, WALLET, "U")))
    assert "U" in b.suspect and b.health() == (False, "order_outcome_unknown")


def test_a_fatal_broker_stops_the_maker(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    br.ok = (False, "broker_fatal")
    br.fatal = "place: HTTP 401 unauthorized"
    run(bot.decide_once(now))
    assert j.of("kill") and "broker:" in j.of("kill")[0]["reason"] and bot._stop and not bot.mm_bids
    assert "биржа отказала" in notify.event_message("kill", j.of("kill")[0], unit="pmbot-maker-live", mode="live")


def test_live_maker_alerts_are_rate_limited_texts():
    from pmbot.bot.alerts import ALERT_TYPES, RATE_LIMITED
    for k in ("mm_live_error", "fill_failed"):
        assert k in ALERT_TYPES and k in RATE_LIMITED and k in notify.ERROR_RU
    assert "mm_exec" not in ALERT_TYPES and "mm_balance" not in ALERT_TYPES    # routine records stay out of Telegram


def test_the_run_report_shows_what_the_exchange_did_with_our_bids(capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rj", os.path.join(ROOT, "scripts", "report_journal.py"))
    rj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rj)
    ev = [{"type": "mm_quote"},
          {"type": "order_placed", "order_id": "a", "latency_ms": 120.0}, {"type": "order_placed", "order_id": "b", "latency_ms": 180.0},
          {"type": "order_cancelled", "order_ids": ["b"], "latency_ms": 90.0, "not_canceled": {}},
          {"type": "mm_exec", "order_id": "a", "size": 5.0, "status": "MATCHED", "adopted": False},
          {"type": "order_rejected", "code": "post_only_would_cross"}, {"type": "mm_refused", "why": "paused"},
          {"type": "mm_rebate", "day": "2026-11-27", "usd": 0.31}]
    rj.maker_live_report(ev)
    out = capsys.readouterr().out
    assert "orders placed 2, filled at least partly 1 (50.0 %)" in out and "post_only_would_cross" in out
    assert "$0.3100" in out and "stream" in out


def test_a_bid_the_exchange_removed_is_placed_again(tmp_path):
    bot, j, br, w, now = live_bot(tmp_path)
    run(bot.decide_once(now))
    gone = bot.mm_bids[(w.key, "U")]["oid"]
    br.is_resting = lambda oid: oid != gone                               # a heartbeat lapse cancelled it at the exchange
    br.calls.clear()
    run(bot.decide_once(now + 200))
    assert any(c[0] == "place" and c[2] == "U" for c in br.calls) and bot.mm_bids[(w.key, "U")]["oid"] != gone
    assert any(x["why"] == "gone" for x in j.of("mm_cancel"))


def test_prewarm_signs_an_order_that_is_never_posted():
    class C(FakeClient):
        async def create_limit_order(self, **kw):
            self.calls.append(("sign", kw))
            return object()
    b = broker(client=C())
    ms = run(b.prewarm("U"))
    assert ms is not None and [c[0] for c in b.client.calls] == ["sign"] and not b.client.open
    assert b.client.calls[0][1]["post_only"] is True and b.client.calls[0][1]["expiration"] >= time.time() + 180


def test_an_adopted_order_counts_its_later_fills_and_old_windows_are_forgotten():
    fills = []
    b = broker(fills=fills)
    b.tokens["D"] = ("btc-7", "D", time.time() + 100)
    b.handle_trade(trade("t1", "MATCHED", ("lost", 2, 0.41, WALLET, "D")))
    b.handle_trade(trade("t2", "MATCHED", ("lost", 3, 0.41, WALLET, "D")))      # the rest of the same order, later
    assert [f["size"] for f in fills] == [2.0, 3.0]
    b.tokens["D"] = ("btc-7", "D", time.time() - 3700)                    # an hour after that window
    b.gc(0)
    assert "D" not in b.tokens and "lost" not in b.known and not any(u[1] == "lost" for u in b._counted)


def test_the_maker_on_the_real_live_broker_end_to_end(tmp_path):
    """FairMaker + MakerLiveBroker on the fake exchange: both bids, a fill from the stream, a jump pull in one call, the stop
    sweep. Catches interface mismatches between the maker and the live broker (paper never exercises them)."""
    over = {"risk": {"state_file": str(tmp_path / "s.json"), "kill_file": str(tmp_path / "STOP")}}
    cfg, mm, lc = load_maker_live_config(LIVE, live_flag=True, overrides=over)
    j = J()
    bot = FairMaker(cfg, mm, j, live=lc)
    b = bot.broker
    b.client, b._geoblock_get, b._env = FakeClient(), OK_GEO, ENV
    b.stream_ok, b.hb_ok_at, b.balance_usd, b.wallet = True, b.mono(), 300.0, WALLET.lower()
    now = int(time.time() * 1000)
    start = now // 1000 - 100
    w = bot.st.add_window(Window(asset="btc", start=start, slug="s", condition_id="c", token_up="U", token_down="D"))
    px = bot.st.prices["btc"]
    for s in range(start - 70, start):
        px.bn_sec_mid[s] = 100.0
    px.bn_hist.append((now - 5000, 100.0 * math.exp(10e-4)))
    bot.bsig["btc"].r2.extend([1e-8 * 10] * 360)
    bot.bsig["btc"].last_sec = now // 1000 - 2
    bot.st.books["U"] = Book(bids={0.55: 100.0}, asks={0.90: 50.0})
    bot.st.books["D"] = Book(bids={0.09: 100.0}, asks={0.45: 50.0})
    bot.st.feed_alive["clob"]["btc"] = now
    bot.st.feed_alive["binance"] = now

    async def go():
        await bot.decide_once(now)
        placed = [c for c in b.client.calls if isinstance(c, tuple) and c[0] == "place"]
        assert len(placed) == 2 and {c[1]["asset_id"] for c in placed} == {"U", "D"} and len(b.orders) == 2
        assert all(c[1]["post_only"] and c[1]["expiration"] >= w.end + 60 for c in placed)
        up = bot.mm_bids[(w.key, "U")]["oid"]
        b.handle_trade(trade("t1", "MATCHED", (up, 5, bot.mm_bids[(w.key, "U")]["price"], WALLET, "U")))
        assert bot.held(w.key) == (5.0, 0.0) and (w.key, "U") not in bot.mm_bids and len(j.of("fill")) == 1
        px.bn_hist.append((now + 100, 100.0 * math.exp(40e-4)))              # a 30 bp Binance jump: pull
        b.client.calls.clear()
        await bot.decide_once(now + 150)
        cancels = [c for c in b.client.calls if isinstance(c, tuple) and c[0] == "cancel"]
        assert len(cancels) == 1 and not bot.mm_bids and not b.orders
        b.client.open["ghost"] = {"asset_id": "U"}                       # an order of ours the stop must not leave behind
        await bot._on_stopping()
        assert not b.client.open
    run(go())
