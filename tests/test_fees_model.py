import numpy as np
import pytest

from pmbot.fees import breakeven_prob, fee_per_share, taker_fee
from pmbot.model import prob_up


def test_fee_table_matches_docs():
    # docs.polymarket.com/trading/fees  (crypto, 100 shares)
    assert taker_fee(100, 0.50) == pytest.approx(1.75)
    assert taker_fee(100, 0.30) == pytest.approx(1.47)
    assert taker_fee(100, 0.90) == pytest.approx(0.63)
    assert taker_fee(100, 0.01) == pytest.approx(0.0693, abs=1e-4)


def test_fee_symmetric_and_breakeven():
    assert fee_per_share(0.3) == pytest.approx(fee_per_share(0.7))
    assert breakeven_prob(0.5) == pytest.approx(0.5175)


def test_prob_up_at_open_is_half_when_spot_equals_open():
    assert prob_up(0, 100.0, 100.0, 1e-4) == pytest.approx(0.5)


def test_prob_up_monotone_in_spot_and_time():
    s = 1e-4
    p1 = prob_up(100, 100.05, 100.0, s)
    p2 = prob_up(200, 100.05, 100.0, s)
    assert 0.5 < p1 < p2 < 1


def test_prob_up_inside_twap_window_uses_partial_sum():
    # 50 of 60 seconds already averaged at 101 vs open 100 -> almost certain Up even if spot drops to 99.5
    p = prob_up(290, 99.5, 100.0, 1e-4, partial_sum=50 * 101.0, n_partial=50)
    assert p > 0.999
    # partial window below open and spot below -> almost certain Down
    p = prob_up(290, 99.9, 100.0, 1e-4, partial_sum=50 * 99.9, n_partial=50)
    assert p < 0.001


def test_vectorised():
    p = prob_up(np.array([0, 100, 250]), np.array([100, 100.1, 100.1]), 100.0, 1e-4,
                partial_sum=np.array([0, 0, 10 * 100.1]), n_partial=np.array([0, 0, 10]))
    assert p.shape == (3,)
    assert np.all((p > 0) & (p < 1))


def test_walk_two_ladders():
    from pmbot.arb_scanner import walk
    # 0.40 + 0.50 + fees(0.0168+0.0175) = 0.9343 -> edge 6.6c for 10 shares, next level 0.45+0.50 -> 0.95+fees>1-min_edge
    shares, cost, edge = walk([(0.40, 10), (0.47, 100)], [(0.50, 50)], min_edge=0.01)
    assert shares == 10
    assert edge == pytest.approx(1 - (0.9 + 0.07 * 0.4 * 0.6 + 0.07 * 0.25), abs=1e-9)
    assert walk([(0.55, 10)], [(0.50, 10)], 0.0)[0] == 0


def test_weather_metar_parsing_and_rounding():
    from pmbot.weather import parse_temp_c, qualifying, round_half_up
    assert parse_temp_c("KLGA 030051Z 20006KT 10SM FEW150 16/06 A3033 RMK AO2 SLP269 T01610061 $") == 16.1
    assert parse_temp_c("EGLC 041250Z 22008KT 9999 SCT030 M02/M05 Q1012 RMK T10171022") == -1.7
    assert parse_temp_c("ZSPD 280000Z 33005MPS CAVOK 18/10 Q1028 NOSIG") == 18
    assert parse_temp_c("UUWW 041200Z 00000MPS CAVOK M05/M07 Q1030 NOSIG") == -5
    assert parse_temp_c("SPECI KLGA 031215Z 20006KT 16/06 A3033 RMK T01610061") is None
    # examples from the market rules
    assert round_half_up(13.9) == 14 and round_half_up(17.4) == 17 and round_half_up(16.5) == 17
    assert round_half_up(-1.7) == -2 and round_half_up(-8.5) == -8
    assert qualifying(18.3, "F") == 65 and qualifying(17.8, "F") == 64 and qualifying(19, "F") == 66
    assert qualifying(-22.5, "F") == -8
