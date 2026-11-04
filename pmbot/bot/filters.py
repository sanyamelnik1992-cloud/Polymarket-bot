"""Pre-quote filters. Every filter that fails adds a reason; any reason = do not quote this window."""
from .config import FilterCfg, StrategyCfg
from .fair import Fair
from .state import Book, State, Window


def check(w: Window, st: State, fv: Fair | None, now_ms: int, fc: FilterCfg, sc: StrategyCfg) -> list:
    reasons = []
    px = st.prices[w.asset]
    rel = now_ms / 1000.0 - w.start
    # --- timing
    if rel < sc.start_s:
        reasons.append("too_early")
    if rel >= sc.stop_s:
        reasons.append("too_late")
    # --- strike
    if w.strike is None:
        reasons.append("no_strike")
    # --- feeds
    if px.cl_last is None or now_ms - px.cl_last[2] > fc.cl_max_age_ms:
        reasons.append("chainlink_stale")
    if now_ms - px.bn_last_rx > fc.bn_max_age_ms:
        reasons.append("binance_stale")
    clob_rx = st.feed_alive["clob"].get(w.asset, 0)
    if now_ms - clob_rx > fc.book_max_age_ms:
        reasons.append("clob_stale")
    bu: Book = st.books.get(w.token_up)
    bd: Book = st.books.get(w.token_down)
    if bu is None or bd is None or bu.best_bid() is None or bu.best_ask() is None \
            or bd.best_bid() is None or bd.best_ask() is None:
        reasons.append("no_book")
    else:
        if bu.best_ask() - bu.best_bid() > fc.max_spread + 1e-9 or bd.best_ask() - bd.best_bid() > fc.max_spread + 1e-9:
            reasons.append("wide_spread")
    # --- model
    if fv is None:
        reasons.append("no_fair_value")
    else:
        if not (sc.p_min <= fv.p_up <= sc.p_max):
            reasons.append("p_out_of_range")
        if fv.sigma * 1e4 > fc.max_sigma_bp:
            reasons.append("vol_too_high")
        if "no_book" not in reasons:
            mid = (bu.best_bid() + bu.best_ask()) / 2
            if abs(fv.p_up - mid) > fc.max_disagree:
                reasons.append("model_disagrees")
    # --- jumps: a fast Binance move pulls quotes for a cooldown
    sig = px.sigma()
    if sig:
        move = abs(px.bn_move_since(1000))
        if move > fc.jump_sigmas * sig:
            st.cooldown_until[w.asset] = now_ms + fc.jump_cooldown_ms
    if now_ms < st.cooldown_until.get(w.asset, 0):
        reasons.append("jump_cooldown")
    return reasons
