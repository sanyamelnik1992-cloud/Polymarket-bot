"""The live test of the rewards rule (pmbot/rewards/live.py): REAL MONEY code, so its gates and limits are pinned here."""
import asyncio
import json
import os
import time
import types

import pytest

from pmbot import notify, ops
from pmbot.bot.live import CONFIRM_PHRASE, LiveGateError
from pmbot.rewards import markets as mk
from pmbot.rewards.config import load_rewards_config
from pmbot.rewards.live import (LiveCfg, LiveFarmer, RewardsLiveBroker, choose_market, load_rewards_live_config)

ENV = {"PMBOT_LIVE_CONFIRM": CONFIRM_PHRASE, "PMBOT_PRIVATE_KEY": "0xabc"}
OK_GEO = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": False, "country": "IE"})  # noqa: E731
CFG = os.path.join(ops.REPO, "config", "rewards_live.toml")


def run(coro):
    return asyncio.run(coro)


class J:
    def __init__(self):
        self.ev = []

    def log(self, kind, **kw):
        self.ev.append({"type": kind, **kw})

    def of(self, kind):
        return [e for e in self.ev if e["type"] == kind]


class Pager:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        async def gen():
            for x in self.items:
                yield x
        return gen()


class FakeClient:
    """The exchange as the broker sees it. It has NO cancel_all on purpose: the wallet is shared with the lock bots,
    and a call to it would fail the test with an AttributeError."""

    def __init__(self, balance_usd=200.0):
        self.balance = int(balance_usd * 1e6)
        self.calls = []
        self.open = {}                   # order id -> kwargs
        self.n = 0
        self.pct = {}
        self.place_error = None
        self.earn = []
        self.matched = {}                # order id -> shares the exchange says were matched

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
        done = tuple(o for o in order_ids if self.open.pop(o, None) is not None)
        return types.SimpleNamespace(canceled=done, not_canceled={})

    async def cancel_market_orders(self, market=None, asset_id=None, token_id=None):
        self.calls.append(("sweep", asset_id))
        ids = [k for k, v in self.open.items() if v["asset_id"] == asset_id]
        for k in ids:
            del self.open[k]
        return types.SimpleNamespace(canceled=tuple(ids), not_canceled={})

    async def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return types.SimpleNamespace(size_matched=str(self.matched.get(order_id, 0)), status="LIVE")

    def list_open_orders(self, asset_id=None, **kw):
        return Pager([k for k, v in self.open.items() if v["asset_id"] == asset_id])

    async def get_reward_percentages(self):
        self.calls.append("pct")
        return dict(self.pct)

    async def get_orders_scoring(self, order_ids):
        return {o: True for o in order_ids}

    def list_user_earnings_for_day(self, date):
        return Pager([e for e in self.earn if e.day == date])

    async def close(self):
        self.calls.append("close")


def broker(client=None, journal=None, **lc):
    b = RewardsLiveBroker(LiveCfg(**lc), journal=journal or J(), client=client or FakeClient(), geoblock_get=OK_GEO,
                          env=ENV)
    b._user_stream = lambda: asyncio.sleep(3600)          # no websocket in tests
    b.stream_ok = True
    return b


# ---------------------------------------------------------------- gates
def test_config_needs_the_live_mode_and_the_flag_and_is_one_market():
    with pytest.raises(LiveGateError, match="--live"):
        load_rewards_live_config(CFG, live_flag=False)
    with pytest.raises(ValueError, match="live"):                      # a paper config never reaches the live broker
        load_rewards_live_config(os.path.join(ops.REPO, "config", "rewards.toml"), live_flag=True)
    with pytest.raises(ValueError, match="paper-only"):                # ... and the paper runner refuses the live one
        load_rewards_config(CFG)
    rc, lc = load_rewards_live_config(CFG, live_flag=True)
    assert rc.mode == "live" and rc.max_markets == 1 and rc.quote_budget_usd == lc.max_open_usd == 20
    assert rc.hedge is False and rc.reduce_only is False and rc.state_file == ""
    assert lc.kind == "alone" and lc.minutes == 180 and lc.max_shares == 20 and lc.reserve_usd == 40 and lc.stop_on_fill
    paper = load_rewards_config(os.path.join(ops.REPO, "config", "rewards.toml"))
    for k in ("dist_cents", "alone_frac", "size_mult", "max_competitiveness", "max_volume_24h", "price_lo", "price_hi",
              "requote_min_s", "min_order_age_s", "max_ref_disagree"):       # the test measures the paper bot's rule
        assert getattr(rc, k) == getattr(paper, k), k
    assert rc.min_days_left == 7 > paper.min_days_left                 # ... on a calmer market than paper would take


def test_config_limits_cannot_be_raised_into_a_bot(tmp_path):
    text = open(CFG).read()
    for old, new in (("max_open_usd = 20", "max_open_usd = 500"), ("max_shares = 20", "max_shares = 200"),
                     ("minutes = 180", "minutes = 100000"), ('kind = "alone"', 'kind = "all"')):
        p = tmp_path / "c.toml"
        p.write_text(text.replace(old, new))
        assert old in text
        with pytest.raises(ValueError):
            load_rewards_live_config(str(p), live_flag=True)


def test_broker_start_gates():
    with pytest.raises(LiveGateError):                                 # no confirmation in the environment
        run(RewardsLiveBroker(LiveCfg(), client=FakeClient(), geoblock_get=OK_GEO, env={}).start())
    with pytest.raises(LiveGateError, match="reserve"):                # $55 < $20 of orders + $40 for the lock bots
        run(broker(FakeClient(55)).start())
    blocked = lambda url, timeout: types.SimpleNamespace(json=lambda: {"blocked": True, "country": "US"})  # noqa: E731
    with pytest.raises(LiveGateError, match="geoblock"):
        run(RewardsLiveBroker(LiveCfg(), client=FakeClient(), geoblock_get=blocked, env=ENV).start())

    async def ok():
        b = broker()
        assert await b.start() and b.balance_usd == 200 and "hb" in b.client.calls
        for t in b._tasks:
            t.cancel()
    run(ok())


# ---------------------------------------------------------------- limits of the broker itself
def test_orders_are_post_only_gtd_buys_inside_the_limits():
    async def scenario():
        j = J()
        b = broker(journal=j)
        t0 = time.time()
        assert await b.place("Y", "m", "Y", 0.46, 20, 0) == "o1"
        kw = b.client.calls[-1][1]
        assert kw["side"] == "BUY" and kw["post_only"] is True and kw["price"] == "0.46" and kw["size"] == "20.00"
        assert t0 + 1190 <= kw["expiration"] <= t0 + 1210               # expires by itself in 20 minutes
        n = len(b.client.calls)
        assert await b.place("N", "m", "N", 0.46, 21, 0) is None        # more than max_shares
        assert await b.place("N", "m", "N", 0.97, 5, 0) is None         # price outside 0.05-0.95
        assert await b.place("N", "m", "N", 0.60, 20, 0) is None        # 9.2 + 12 > $20 resting
        assert await b.place("Y", "m", "Y", 0.40, 5, 0) is None         # a second order on the same token
        assert await b.place("N", "m", "X", 0.40, 5, 0) is None
        assert len(b.client.calls) == n                                  # none of them reached the exchange
        assert {e["why"] for e in j.of("rlive_refused")} == {"size", "price", "max_open_usd", "two_orders", "bad_side"}
        assert await b.place("N", "m", "N", 0.46, 20, 0) == "o2" and b.open_usd() == pytest.approx(18.4)
        assert await b.place("Z", "m", "Y", 0.10, 5, 0) is None         # never a third resting order
    run(scenario())


def test_order_count_is_capped_and_an_unanswered_order_stops_everything():
    async def scenario():
        b = broker(max_orders=2)
        for _ in range(2):
            oid = await b.place("Y", "m", "Y", 0.46, 20, 0)
            await b.cancel([oid], 0)
        assert await b.place("Y", "m", "Y", 0.46, 20, 0) is None and "max_orders" in b.fatal
        assert b.health() == (False, "broker_fatal")
        c = broker()
        c.client.place_error = TimeoutError("no answer")               # the order may or may not exist now
        assert await c.place("Y", "m", "Y", 0.46, 20, 0) is None and c.suspect
        assert c.health() == (False, "order_outcome_unknown") and c.tokens == {"Y"}
        assert await c.place("N", "m", "N", 0.46, 20, 0) is None        # nothing more is placed
        d = broker()
        err = RuntimeError("invalid post-only order: order crosses book")
        err.status = 400
        d.client.place_error = err                                     # a clean refusal: nothing exists, try later
        assert await d.place("Y", "m", "Y", 0.46, 20, 0) is None and not d.suspect and not d.fatal
        e401 = RuntimeError("unauthorized")
        e401.status = 401
        d.client.place_error = e401
        assert await d.place("Y", "m", "Y", 0.46, 20, 0) is None and d.fatal
    run(scenario())


def test_stop_cancels_its_own_orders_and_never_everything():
    async def scenario():
        j = J()
        b = broker(journal=j)
        await b.start()
        b.client.open["foreign"] = {"asset_id": "OTHER"}                # an order of another bot on the same wallet
        await b.place("Y", "m", "Y", 0.46, 20, 0)
        await b.place("N", "m", "N", 0.46, 20, 0)
        left = await b.stop()
        assert left == 0 and b.client.open == {"foreign": {"asset_id": "OTHER"}}
        assert ("cancel", ("o1", "o2")) in b.client.calls and ("sweep", "Y") in b.client.calls and ("sweep", "N") in b.client.calls
        assert await b.place("Y", "m", "Y", 0.46, 20, 0) is None        # nothing after the stop
    run(scenario())


def test_fills_come_from_the_account_stream_once():
    async def scenario():
        fills = []
        b = broker()
        b.on_fill = fills.append
        oid = await b.place("Y", "m", "Y", 0.46, 20, 0)
        mo = types.SimpleNamespace(order_id=oid, matched_amount="20", price="0.46")
        other = types.SimpleNamespace(order_id="foreign", matched_amount="5", price="0.91")
        pl = types.SimpleNamespace(id="t1", status="MATCHED", maker_orders=(mo, other))
        b.handle_trade(pl)
        b.handle_trade(pl)                                               # the same trade again (MINED, CONFIRMED)
        assert len(fills) == 1 and fills[0]["side"] == "Y" and fills[0]["size"] == 20 and fills[0]["key"] == "m"
        assert oid not in b.orders and oid in b.known
    run(scenario())


def test_a_fill_the_stream_missed_is_found_in_the_exchange_record_and_counted_once():
    async def scenario():
        fills = []
        b = broker()
        b.on_fill = fills.append
        oid = await b.place("Y", "m", "Y", 0.46, 20, 0)
        assert await b.reconcile() == 0 and not fills                   # nothing matched: nothing happens
        b.client.matched[oid] = 20                                      # filled while the stream was reconnecting
        assert await b.reconcile() == 20 and len(fills) == 1 and fills[0]["status"] == "RECONCILED"
        assert fills[0]["price"] == 0.46 and oid not in b.orders
        mo = types.SimpleNamespace(order_id=oid, matched_amount="20", price="0.46")
        b.handle_trade(types.SimpleNamespace(id="t1", status="MATCHED", maker_orders=(mo,)))     # the event, late
        assert len(fills) == 1
        # an order the exchange refuses to cancel because it matched: counted through the same path
        oid2 = await b.place("N", "m", "N", 0.46, 20, 0)
        b.client.matched[oid2] = 20

        async def nc(order_ids):
            return types.SimpleNamespace(canceled=(), not_canceled={order_ids[0]: "order already matched"})
        b.client.cancel_orders = nc
        await b.cancel([oid2], 0)
        assert len(fills) == 2 and fills[1]["side"] == "N" and oid2 not in b.orders
    run(scenario())


def test_reward_numbers_of_the_exchange_and_failures_are_not_zeros():
    async def scenario():
        j = J()
        b = broker(journal=j)
        b.client.pct = {"cid1": 100.0, "cid2": 0.0}
        st = await b.reward_state("cid1", ["o1"])
        assert st == {"pct": 100.0, "markets_with_pct": 1, "scoring": {"o1": True}}

        async def boom():
            raise RuntimeError("503")
        b.client.get_reward_percentages = boom
        st = await b.reward_state("cid1", [])
        assert "pct" not in st and j.of("rlive_error")[0]["where"] == "percentages"
        b.client.earn = [types.SimpleNamespace(day="2026-11-14", condition_id="cid1", earnings="1.25"),
                         types.SimpleNamespace(day="2026-11-14", condition_id="cid1", earnings="0.25")]
        assert await b.earnings("2026-11-14") == {"cid1": 1.5} and await b.earnings("2026-11-13") == {}
    run(scenario())


# ---------------------------------------------------------------- the market of the test
def rmarket(slug, empty=True, ref=0.5, rate=50.0, size=20, capital=18.4, tick=0.01, neg_risk=False):
    m = mk.RMarket(slug=slug, question="Will " + slug + "?", condition_id="c-" + slug, yes=slug + "-Y", no=slug + "-N",
                   rate=rate, v=4.5, min_size=20, end_ts=time.time() + 10 * 86400, price=ref, size=size, last_trade=ref)
    m.empty, m.capital, m.tick, m.neg_risk, m.extra = empty, capital, tick, neg_risk, {"ref": ref}
    return m


def test_choose_market_takes_the_first_that_fits_the_test():
    picks = [rmarket("big", size=50, capital=46), rmarket("cheap", ref=0.15), rmarket("small-pool", rate=10),
             rmarket("fine-tick", tick=0.001), rmarket("negrisk", neg_risk=True), rmarket("shared", empty=False),
             rmarket("alone")]
    assert choose_market(picks, LiveCfg(kind="alone")).slug == "alone"
    assert choose_market(picks, LiveCfg(kind="shared")).slug == "shared"
    assert choose_market(picks, LiveCfg(kind="any")).slug == "shared"
    assert choose_market(picks, LiveCfg(kind="any", slug="alone")).slug == "alone"
    assert choose_market(picks[:5], LiveCfg(kind="any")) is None
    assert choose_market(picks, LiveCfg(kind="any", slug="cheap")) is None       # a named market must still fit


# ---------------------------------------------------------------- the farmer on one market
def live_farmer(tmp_path, picks=None, **lc):
    rc, base = load_rewards_live_config(CFG, live_flag=True)
    lcfg = LiveCfg(**{**base.__dict__, **lc})
    j = J()
    b = broker(journal=j, **{k: getattr(lcfg, k) for k in ("max_open_usd", "max_shares", "max_orders")})
    picks = picks if picks is not None else [rmarket("alone")]
    f = LiveFarmer(rc, lcfg, j, b, select=lambda rc_, journal: picks)
    f.rc.kill_file = str(tmp_path / "STOP")

    async def no_feed():
        await asyncio.sleep(3600)
    f.feed.run = no_feed
    return f


def set_book(f, token="alone-Y", bids=None, asks=None):
    f.feed.set_tokens({token})
    b = f.feed.books[token]
    b.bids, b.asks, b.last_rx = dict(bids or {0.30: 100.0}), dict(asks or {0.70: 100.0}), 1
    f.feed.last_msg = time.time()


def test_the_book_is_read_without_our_own_orders(tmp_path):
    async def scenario():
        f = live_farmer(tmp_path)
        f.apply_selection([rmarket("alone")])
        set_book(f, bids={0.46: 70.0, 0.30: 100.0}, asks={0.54: 20.0, 0.70: 100.0})
        await f.broker.place("alone-Y", "alone", "Y", 0.46, 20, 0)       # our YES bid: 20 of the 70 at 0.46
        await f.broker.place("alone-N", "alone", "N", 0.46, 20, 0)       # our NO bid = the 20 offered at 0.54
        bids, asks = f.levels("alone-Y")
        assert bids == [(0.46, 50.0), (0.30, 100.0)] and asks == [(0.70, 100.0)]
    run(scenario())


def test_paper_only_parts_are_off_in_live(tmp_path):
    f = live_farmer(tmp_path)
    f.apply_selection([rmarket("alone")])
    ms = f.markets["alone"]
    assert f.cross_fills(ms, 0) == 0.0 and f.hedge_once() == 0 and f._next_hedge_in() is None
    set_book(f)
    assert set(f.targets(ms, time.time())) == {"Y", "N"}
    f.broker.stream_ok = False                                          # the fill stream is down: rest nothing
    assert f.targets(ms, time.time()) == {}
    f.broker.stream_ok = True
    f.broker.suspect = True
    assert f.targets(ms, time.time()) == {}


def test_live_test_end_to_end_quotes_reads_the_exchange_and_stops_on_a_fill(tmp_path, capsys):
    async def scenario():
        f = live_farmer(tmp_path, poll_s=1)
        f.broker.client.pct = {"c-alone": 100.0}
        set_book(f)
        task = asyncio.create_task(f.run(duration_s=30))
        for _ in range(60):                                             # the first quote pass after the selection
            await asyncio.sleep(0.1)
            f.feed.last_msg = time.time()
            if len(f.broker.orders) == 2:
                break
        assert {(o.token, o.price, o.size) for o in f.broker.orders.values()} == {("alone-Y", 0.46, 20), ("alone-N", 0.46, 20)}
        rec = await f.exchange_once()
        assert rec["exch_pct"] == 100.0 and rec["scoring"] == {"Y": True, "N": True} and rec["bids"] == {"Y": 0.46, "N": 0.46}
        oid = next(k for k, o in f.broker.orders.items() if o.side == "Y")
        f.broker.handle_trade(types.SimpleNamespace(id="t1", status="MATCHED", maker_orders=(
            types.SimpleNamespace(order_id=oid, matched_amount="20", price="0.46"),)))
        done = await asyncio.wait_for(task, 10)
        return f, done
    f, done = run(scenario())
    assert done["why"] == "fill" and done["fills"] == 1 and done["position"] == {"yes": 20.0, "no": 0.0, "cost": 9.2}
    assert done["orders_left_open"] == 0 and f.broker.client.open == {} and done["exch_pct_last"] == 100.0
    assert done["whole_pool"] == 50.0 and done["key"] == "alone" and done["cid"] == "c-alone"
    kinds = [e["type"] for e in f.journal.ev]
    assert kinds.index("start") < kinds.index("rlive_check") < kinds.index("rlive_market") < kinds.index("order_placed")
    assert "rlive_fill" in kinds and kinds[-1] == "rlive_done" and f.journal.of("start")[0]["mode"] == "live"
    assert not any(c == "cancel_all" for c in f.broker.client.calls)
    # the hourly report prints the test from its journal
    import importlib.util
    run_dir = tmp_path / "journal" / "20261114-160000"
    run_dir.mkdir(parents=True)
    with open(run_dir / "events.jsonl", "w") as out:
        for i, e in enumerate(f.journal.ev):
            out.write(json.dumps({"t": 1794672000 + i, **e}) + "\n")
    spec = importlib.util.spec_from_file_location("report_rewards", os.path.join(ops.REPO, "scripts", "report_rewards.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(str(tmp_path / "journal"))
    txt = capsys.readouterr().out
    assert "LIVE TEST (real money" in txt and "market: Will alone?" in txt and "DONE (fill)" in txt
    assert "orders placed 2, rejected 0, fills 1" in txt and "exch share 100.0%" in txt


def test_no_market_and_kill_file_place_nothing(tmp_path):
    async def scenario():
        f = live_farmer(tmp_path, picks=[rmarket("shared", empty=False)])       # the test wants a lone pool
        set_book(f)
        done = await asyncio.wait_for(f.run(duration_s=20), 15)
        g = live_farmer(tmp_path)
        set_book(g)
        open(g.rc.kill_file, "w").write("stop")
        done2 = await asyncio.wait_for(g.run(duration_s=20), 15)
        return f, done, g, done2
    f, done, g, done2 = run(scenario())
    assert done["why"] == "no_market" and f.broker.placed == 0 and f.journal.of("rlive_no_market")
    assert done2["why"] == "kill_file" and done2["orders_left_open"] in (0, None) and g.broker.client.open == {}


# ---------------------------------------------------------------- wiring: unit, Telegram, report
def test_unit_is_live_started_only_by_hand_and_never_restarted():
    text = open(os.path.join(ops.REPO, "deploy", "pmbot-rewards-live.service")).read()
    assert "run_rewards_live.py --config config/rewards_live.toml --live" in text and "Restart=no" in text
    assert "REAL MONEY" in text and "KillSignal=SIGINT" in text
    u = "pmbot-rewards-live"
    assert u in ops.ALL_UNITS and u in ops.REWARD_LIVE_UNITS and u not in ops.BOT_UNITS and u not in ops.REWARD_UNITS
    assert ops.unit_is_live(u) and ops.bot_paths(u) == ("data/journal-rewards-live", "", "live")
    assert f" {u} " in open(os.path.join(ops.REPO, "deploy", "install_units.sh")).read()          # Telegram may start it
    assert u not in open(os.path.join(ops.REPO, "deploy", "autoupdate.sh")).read()                # an update never does
    assert notify.unit_for_config("config/rewards_live.toml") == u
    assert "РЕАЛЬНЫЕ ДЕНЬГИ" in notify.label(u)


def test_telegram_texts_of_the_live_test():
    from pmbot.bot.alerts import ALERT_TYPES
    assert {"rlive_market", "rlive_fill", "rlive_done", "rlive_no_market"} <= ALERT_TYPES
    m = notify.event_message("rlive_market", {"question": "Will X?", "rate": 50, "empty": True, "size": 20, "capital": 18.4,
                                              "minutes": 180}, unit="pmbot-rewards-live", mode="live")
    assert "Will X?" in m and "мы одни" in m and "/rlive" in m and "180" in m
    fl = notify.event_message("rlive_fill", {"question": "Will X?", "side": "N", "price": 0.46, "size": 20},
                              unit="pmbot-rewards-live", mode="live")
    assert "исполнили" in fl and "«Нет» 20 по 0.46" in fl and "9.20$" in fl
    done = {"why": "time", "question": "Will X?", "quoted_min": 178.0, "rate": 50.0, "exch_pct_mean": 100.0,
            "paper_share_mean": 1.0, "scoring_all": 1.0, "whole_pool": 50.0, "by_time_exch": 6.18, "paper_reward": 6.2,
            "fills": 0, "position": None, "orders_left_open": 0}
    d = notify.event_message("rlive_done", done, unit="pmbot-rewards-live", mode="live")
    assert "100.0%" in d and "50.00$" in d and "6.18$" in d and "открытых не осталось" in d and "время вышло" in d
    assert "Осталось открытых заявок: 2" in notify.rewards_live_done({**done, "orders_left_open": 2})
    assert "нет ответа" in notify.rewards_live_done({**done, "exch_pct_mean": None})


def test_status_and_report_of_the_live_test(tmp_path, monkeypatch):
    now = time.time()
    market = {"type": "rlive_market", "t": now - 600, "key": "m", "question": "Will X?", "cid": "c1", "rate": 50.0,
              "comp": 0.0, "empty": True, "test_kind": "alone", "minutes": 180, "size": 20, "capital": 18.4}
    sample = {"type": "rlive", "t": now - 60, "key": "m", "paper_share": 1.0, "exch_pct": 100.0,
              "scoring": {"Y": True, "N": True}, "bids": {"Y": 0.46, "N": 0.46}}
    evs = [{"type": "start", "t": now - 700, "mode": "live"}, market, sample]
    monkeypatch.setattr(ops, "events_since", lambda j, since: list(evs))
    monkeypatch.setattr(ops, "unit_state", lambda u: ("active", now - 700))
    wallet = tmp_path / "wallet.json"
    wallet.write_text(json.dumps({"t": now, "usd": 199.0, "rewards": {"pct": {"c1": 100.0}, "earn": {"2026-11-14": {}}}}))
    monkeypatch.setattr(ops, "WALLET_FILE", str(wallet))
    r = ops.rewards_live_summary()
    assert r["market"]["cid"] == "c1" and len(r["samples"]) == 1 and r["earn"] == {} and r["pct_now"] == 100.0
    txt = notify.format_rewards_live()
    assert "Will X?" in txt and "доля по данным биржи: <b>100.0%</b>" in txt and "биржа считает заявки: да" in txt
    assert "пока нет" in txt
    plain = ops.format_rewards_live()
    assert "pmbot-rewards-live [LIVE]" in plain and "доля по бирже 100.0%" in plain
    evs.append({"type": "rlive_done", "t": now, "why": "time", "question": "Will X?", "quoted_min": 178.0, "rate": 50.0,
                "exch_pct_mean": 100.0, "paper_share_mean": 1.0, "scoring_all": 1.0, "whole_pool": 50.0,
                "by_time_exch": 6.18, "paper_reward": 6.2, "fills": 0, "position": None, "orders_left_open": 0})
    wallet.write_text(json.dumps({"t": now, "usd": 199.0, "rewards": {"pct": {}, "earn": {"2026-11-14": {"c1": 50.0}}}}))
    txt = notify.format_rewards_live()
    assert "Начислено биржей" in txt and "2026-11-14: <b>50.00$</b>" in txt and "если платят по минутам: 6.18$" in txt
    assert "НАЧИСЛЕНО БИРЖЕЙ" in ops.format_rewards_live()
    evs[:] = [{"type": "kill", "t": now, "reason": "live gate: collateral 30.00 < 60.00"}]
    assert "запуск отклонён" in notify.format_rewards_live() and "collateral" in ops.format_rewards_live()
    evs[:] = []
    assert ops.rewards_live_summary() is None and "ещё не запускался" in notify.format_rewards_live()
    assert ops.format_rewards_live() == ""


def test_wallet_script_reads_only_numbers_about_rewards():
    import importlib.util
    spec = importlib.util.spec_from_file_location("wallet_balance", os.path.join(ops.REPO, "scripts", "wallet_balance.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = FakeClient()
    c.pct = {"c1": 100.0, "c2": 0.0}
    now = 1794650000                                                    # 14 Nov 2026
    c.earn = [types.SimpleNamespace(day="2026-11-13", condition_id="c1", earnings="2.5")]
    out = run(mod.rewards(c, days=2, now=now))
    assert out == {"pct": {"c1": 100.0}, "earn": {"2026-11-14": {}, "2026-11-13": {"c1": 2.5}}}
