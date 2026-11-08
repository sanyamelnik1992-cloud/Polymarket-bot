"""Token-move guard: a token whose book mid moved a lot recently is a moving market -> no quotes (off by default)."""
import time
from types import SimpleNamespace

from pmbot.bot.config import FilterCfg, StrategyCfg
from pmbot.bot.engine import Engine
from pmbot.bot.fair import Fair
from pmbot.bot.filters import check
from pmbot.bot.state import Book, State, Window


def calm_state(now_ms, w):
    st = State(["btc"])
    px = st.prices["btc"]
    px.cl_last = (100.0, now_ms, now_ms)
    px.bn_last_rx = now_ms
    st.feed_alive["clob"]["btc"] = now_ms
    st.books[w.token_up] = Book(bids={0.49: 10.0}, asks={0.51: 10.0})
    st.books[w.token_down] = Book(bids={0.49: 10.0}, asks={0.51: 10.0})
    return st


def window(now_ms):
    return Window(asset="btc", start=now_ms // 1000 - 60, slug="x", condition_id="c",
                  token_up="U", token_down="D", strike=100.0)


def reasons_for(token_move, max_token_move):
    now_ms = int(time.time() * 1000)
    w = window(now_ms)
    st = calm_state(now_ms, w)
    fv = Fair(p_up=0.5, spot=100.0, sigma=0.0001, rel=60.0, cl_age_ms=0, n_partial=60)
    fc = FilterCfg(max_token_move=max_token_move)
    return check(w, st, fv, now_ms, fc, StrategyCfg(), token_move=token_move)


def test_calm_market_passes():
    assert reasons_for(0.01, 0.03) == []


def test_moving_token_is_blocked():
    assert "token_moving" in reasons_for(0.05, 0.03)
    assert "token_moving" in reasons_for(-0.05, 0.03)        # a fall counts too


def test_guard_is_off_by_default():
    assert reasons_for(0.20, 0.0) == []                       # max_token_move = 0: no effect at all
    assert reasons_for(None, 0.03) == []                      # move not known yet: no effect


def test_move_is_measured_against_the_sample_one_window_back():
    """The move is mid(now) minus the newest sample at least one window old; None until such a sample exists."""
    stub = SimpleNamespace(_tok_hist={}, cfg=SimpleNamespace(filters=SimpleNamespace(token_move_window_ms=10000)))
    w = SimpleNamespace(token_up="U")
    for t in range(0, 10001, 1000):
        expected = 0.0 if t == 10000 else None
        assert Engine._token_move(stub, w, 0.50, t) == expected
    assert abs(Engine._token_move(stub, w, 0.56, 11000) - 0.06) < 1e-9


def test_history_stays_bounded():
    stub = SimpleNamespace(_tok_hist={}, cfg=SimpleNamespace(filters=SimpleNamespace(token_move_window_ms=10000)))
    w = SimpleNamespace(token_up="U")
    for t in range(0, 120001, 1000):                     # two minutes, one sample per second
        Engine._token_move(stub, w, 0.50, t)
    assert len(stub._tok_hist["U"]) <= 12                # about one window of samples plus one reference
