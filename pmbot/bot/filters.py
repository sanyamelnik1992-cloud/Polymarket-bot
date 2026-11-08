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
    # --- calm market: fills there are the most toxic (only informed flow trades)
    if fc.min_vol_5m_bp > 0:
        v = px.cl_vol_5m_bp(now_ms // 1000)
        if v is None or v < fc.min_vol_5m_bp:
            reasons.append("calm_market")
    # --- our own process is late: the book / Binance we look at are older than they seem
    if fc.max_loop_lag_ms > 0 and now_ms < st.lag_block_until:
        reasons.append("loop_lag")
    # --- volatility burst: short-horizon vol far above the usual level
    if fc.vol_spike_ratio > 0:
        ss, sl = px.sigma_short(), px.sigma()
        if ss and sl and ss / sl > fc.vol_spike_ratio:
            reasons.append("vol_spike")
    # --- unstable fair value: the model itself is moving fast -> it is likely to be wrong right now
    if fc.max_fair_move > 0 and fv is not None:
        hist = st.fair_hist.get(w.key)
        if hist:
            recent = [p for t, p in hist if t >= now_ms - fc.fair_move_window_ms] + [fv.p_up]
            if max(recent) - min(recent) > fc.max_fair_move:
                reasons.append("fair_unstable")
    return reasons


def queue_imbalance(book: Book, own_size=0.0):
    """bid/(bid+ask) size at the best level of one token (own resting size at the best bid excluded)."""
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None:
        return None
    b = max(book.bids.get(bb, 0.0) - own_size, 0.0)
    a = book.asks.get(ba, 0.0)
    return b / (b + a) if b + a > 0 else None


def imbalance_blocks(w: Window, st: State, fc: FilterCfg, quoting: dict, own: dict) -> set:
    """Sides whose own token book has a thin best bid queue (about to tick down through our bid).
    quoting[side] = we rest a bid there now (hysteresis: leave below the threshold, re-enter 0.10 above it)."""
    out = set()
    if fc.min_queue_imbalance <= 0:
        return out
    for side, tok in (("U", w.token_up), ("D", w.token_down)):
        b = st.books.get(tok)
        imb = queue_imbalance(b, own.get(side, 0.0)) if b else None
        need = fc.min_queue_imbalance + (0.0 if quoting.get(side) else fc.queue_imbalance_hysteresis)
        if imb is None or imb < need:
            out.add(side)
    return out


def side_blocks(w: Window, st: State, now_ms: int, fc: FilterCfg) -> set:
    """Sides ('U'/'D') that must not be quoted: recently swept = toxic flow -> block for a cooldown."""
    out = set()
    for side in ("U", "D"):
        key = (w.key, side)
        if now_ms < st.side_block_until.get(key, 0):
            out.add(side)
            continue
        if fc.toxic_shares <= 0 and fc.toxic_fills <= 0:
            continue
        recent = [(t, q) for t, q in st.fill_hist.get(key, ()) if t >= now_ms - fc.toxic_window_ms]
        shares = sum(q for _, q in recent)
        if (fc.toxic_shares > 0 and shares >= fc.toxic_shares) or (fc.toxic_fills > 0 and len(recent) >= fc.toxic_fills):
            st.side_block_until[key] = now_ms + fc.toxic_cooldown_ms
            out.add(side)
    return out
