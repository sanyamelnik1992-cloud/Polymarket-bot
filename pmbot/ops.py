"""Server operations helpers shared by the Telegram bot (scripts/tg_bot.py) and the GitHub report
(scripts/server_report.py): which bots run, their PnL from journals and risk state files, server health, git info.

Read-only except for `resume_after_stop()`. Never reads or prints secrets.
"""
import bisect
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOT_UNITS = ["pmbot-5m", "pmbot-5m-b", "pmbot-5m-c", "pmbot-5m-d", "pmbot-5m-e", "pmbot-5m-f", "pmbot-5m-live", "pmbot-hourly"]
ALL_UNITS = BOT_UNITS + ["pmbot-arb", "pmbot-recorder", "pmbot-tg"]
KILL_FILE = os.path.join(REPO, "STOP")
WALLET_FILE = "data/state/wallet.json"     # written by scripts/wallet_balance.py (pmbot-wallet.timer), numbers only


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def today_utc(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- units and configs
def unit_config(unit):
    """--config path from deploy/<unit>.service (the template the installed unit is rendered from)."""
    p = os.path.join(REPO, "deploy", unit + ".service")
    try:
        text = open(p).read()
    except OSError:
        return None
    m = re.search(r"^ExecStart=.*--config\s+(\S+)", text, re.M)
    return m.group(1) if m else None


def unit_is_live(unit):
    p = os.path.join(REPO, "deploy", unit + ".service")
    try:
        return bool(re.search(r"^ExecStart=.*--live\b", open(p).read(), re.M))
    except OSError:
        return False


def unit_state(unit):
    """('active'|'inactive'|'failed'|..., since_ts or None). 'n/a' when systemd is not available."""
    if not shutil.which("systemctl"):
        return "n/a", None
    state = _run(["systemctl", "is-active", unit]) or "unknown"
    since = None
    out = _run(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", unit])
    try:
        mono_us = int(out)
        if mono_us > 0:
            since = time.time() - (time.monotonic() - mono_us / 1e6)
    except ValueError:
        pass
    return state, since


def load_toml(path):
    import tomllib
    try:
        with open(os.path.join(REPO, path), "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


def bot_paths(unit):
    """(journal_dir, state_file, mode) for a bot unit, from its config file."""
    cfg_path = unit_config(unit)
    if not cfg_path:
        return None, None, None
    c = load_toml(cfg_path)
    journal = c.get("journal_dir") or (c.get("hourly") or {}).get("journal_dir")
    if not journal:
        journal = "data/journal-hourly" if "hourly" in unit else "data/journal"
    state = (c.get("risk") or {}).get("state_file") or (c.get("hourly") or {}).get("state_file")
    mode = (c.get("execution") or {}).get("mode") or (c.get("hourly") or {}).get("mode") or "paper"
    return journal, state, mode


# ---------------------------------------------------------------- journals
def run_dirs(journal_dir, max_age_s=None):
    """Run directories of a journal, oldest first (names are YYYYmmdd-HHMMSS)."""
    root = os.path.join(REPO, journal_dir)
    try:
        names = sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n, "events.jsonl")))
    except OSError:
        return []
    out = [os.path.join(root, n) for n in names]
    if max_age_s is not None:
        now = time.time()
        out = [d for d in out if now - os.path.getmtime(os.path.join(d, "events.jsonl")) < max_age_s]
    return out


_WANTED = ('"type":"settle"', '"type":"fill"', '"type":"kill"', '"type":"start"', '"type":"status"',
           '"type":"pause"', '"type":"order_rejected"', '"type":"stopped"')


def read_events(run_dir, since_ts=0.0):
    """Only the event types we report on (fv/filters/quote lines are skipped without parsing)."""
    out = []
    try:
        with open(os.path.join(run_dir, "events.jsonl")) as f:
            for line in f:
                if not any(w in line for w in _WANTED):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("t", 0) >= since_ts:
                    out.append(e)
    except OSError:
        pass
    return out


def events_since(journal_dir, since_ts):
    ev = []
    for d in run_dirs(journal_dir, max_age_s=time.time() - since_ts + 3600):
        ev.extend(read_events(d, since_ts))
    ev.sort(key=lambda e: e.get("t", 0))
    return ev


def day_start_ts(day=None):
    day = day or today_utc()
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def risk_state(state_file):
    if not state_file:
        return {}
    try:
        return json.load(open(os.path.join(REPO, state_file)))
    except (OSError, ValueError):
        return {}


def bot_summary(unit, since_ts=None):
    """Everything /status needs for one bot. PnL of the day comes from the risk state file (survives restarts)."""
    journal, state_file, mode = bot_paths(unit)
    state, since = unit_state(unit)
    since_ts = since_ts if since_ts is not None else day_start_ts()
    ev = events_since(journal, since_ts) if journal else []
    settles = [e for e in ev if e["type"] == "settle"]
    fills = [e for e in ev if e["type"] == "fill"]
    rs = risk_state(state_file)
    by_day = rs.get("realized_by_day", {})
    pnl_today = by_day.get(today_utc())
    if pnl_today is None:
        pnl_today = sum(e.get("pnl", 0) for e in settles if e["t"] >= day_start_ts())
    last_status = next((e for e in reversed(ev) if e["type"] == "status"), None)
    return {
        "unit": unit, "state": state, "since": since, "mode": mode, "live_flag": unit_is_live(unit),
        "config": unit_config(unit), "journal": journal,
        "pnl_today": pnl_today, "pnl_by_day": by_day, "pnl_all": sum(by_day.values()) if by_day else None,
        "killed": rs.get("killed_day") == today_utc(), "kill_reason": rs.get("kill_reason", ""),
        "settles": settles, "fills": fills,
        "wins": sum(1 for e in settles if e.get("pnl", 0) > 0), "losses": sum(1 for e in settles if e.get("pnl", 0) < 0),
        "open_worst": (last_status or {}).get("open_worst"),
        "rejected": sum(1 for e in ev if e["type"] == "order_rejected"),
    }


def resume_after_stop():
    """Remove the STOP file and clear a kill that was caused by it (daily-loss kills stay until the next UTC day)."""
    removed = False
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)
        removed = True
    cleared = []
    for unit in BOT_UNITS:
        _, state_file, _ = bot_paths(unit)
        if not state_file:
            continue
        p = os.path.join(REPO, state_file)
        rs = risk_state(state_file)
        if rs.get("killed_day") and str(rs.get("kill_reason", "")).startswith("kill file"):
            rs["killed_day"], rs["kill_reason"] = None, ""
            tmp = p + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rs, f)
            os.replace(tmp, p)
            cleared.append(unit)
    return removed, cleared


# ---------------------------------------------------------------- server and git
def server_health():
    h = {}
    try:
        h["load"] = os.getloadavg()
    except OSError:
        h["load"] = None
    h["cpus"] = os.cpu_count()
    try:
        mi = dict(line.split(":", 1) for line in open("/proc/meminfo"))
        kb = lambda k: int(mi[k].split()[0])  # noqa: E731
        h["mem_total_mb"] = kb("MemTotal") // 1024
        h["mem_avail_mb"] = kb("MemAvailable") // 1024
        h["swap_used_mb"] = (kb("SwapTotal") - kb("SwapFree")) // 1024
    except (OSError, KeyError, ValueError):
        pass
    du = shutil.disk_usage(REPO)
    h["disk_free_gb"] = du.free / 1e9
    h["disk_total_gb"] = du.total / 1e9
    try:
        h["uptime_s"] = float(open("/proc/uptime").read().split()[0])
    except OSError:
        pass
    rec = os.path.join(REPO, "data", "rec")
    if os.path.isdir(rec):
        h["rec_gb"] = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(rec) for f in fs) / 1e9
    return h


def git_info():
    return {
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "commit": _run(["git", "log", "-1", "--format=%h %s (%cr)"]),
    }


def journal_tail(unit, n=15):
    if not shutil.which("journalctl"):
        return "journalctl is not available here"
    return _run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "cat"], timeout=15)[-3500:]


# ---------------------------------------------------------------- formatting (plain text, Russian)
def fmt_money(x):
    return "—" if x is None else f"{x:+.2f}$"


def fmt_age(ts):
    if not ts:
        return "—"
    s = int(time.time() - ts)
    if s < 3600:
        return f"{s // 60} мин"
    if s < 86400:
        return f"{s // 3600} ч {s % 3600 // 60} мин"
    return f"{s // 86400} д {s % 86400 // 3600} ч"


STATE_RU = {"active": "🟢 работает", "inactive": "⚪ выключен", "failed": "🔴 упал", "activating": "🟡 запускается",
            "deactivating": "🟡 останавливается", "n/a": "n/a"}


def format_status():
    lines = []
    total = 0.0
    for unit in BOT_UNITS:
        s = bot_summary(unit)
        if s["state"] in ("inactive", "unknown", "n/a") and not s["settles"] and s["pnl_today"] in (None, 0):
            continue
        mode = "LIVE 💰" if s["mode"] == "live" else "paper"
        lines.append(f"{unit} [{mode}] {STATE_RU.get(s['state'], s['state'])} {fmt_age(s['since'])}")
        lines.append(f"  сегодня: {fmt_money(s['pnl_today'])}  окон: {len(s['settles'])} "
                     f"(+{s['wins']}/−{s['losses']})  исполнений: {len(s['fills'])}")
        if s["pnl_all"] is not None:
            lines.append(f"  всего: {fmt_money(s['pnl_all'])} за {len(s['pnl_by_day'])} дн.")
        if s["open_worst"]:
            lines.append(f"  открытый риск: {s['open_worst']:.2f}$")
        if s["killed"]:
            lines.append(f"  ⛔ kill switch: {s['kill_reason']}")
        total += s["pnl_today"] or 0
    if not lines:
        lines.append("ни один бот не запущен")
    for unit in ("pmbot-recorder",):
        st, since = unit_state(unit)
        if st not in ("inactive", "unknown"):
            lines.append(f"{unit}: {STATE_RU.get(st, st)} {fmt_age(since)}")
    if os.path.exists(KILL_FILE):
        lines.append("⛔ файл STOP есть: торговля остановлена (/resume — снять)")
    g = git_info()
    w = wallet_line()
    if w:
        lines.append(w)
    lines.append(f"\nсегодня всего: {fmt_money(total)}")
    lines.append(f"код: {g['branch']} {g['commit']}")
    return "\n".join(lines)


def wallet_line():
    """Last real wallet balance (pmbot-wallet.timer): the check of live PnL against money, not against journals."""
    try:
        w = json.load(open(os.path.join(REPO, WALLET_FILE)))
    except (OSError, ValueError):
        return ""
    when = datetime.fromtimestamp(w.get("t", 0), timezone.utc).strftime("%H:%M UTC")
    if "usd" not in w:
        return f"кошелёк: нет данных ({w.get('error', '?')}, {when})"
    pos = f" + позиции {w['positions_value']:.2f}$" if w.get("positions_value") else ""
    return f"кошелёк: {w['usd']:.2f}${pos} ({when})"


_Q_WANTED = ('"type":"fv"', '"type":"fill"', '"type":"settle"', '"type":"order_placed"', '"type":"order_cancelled"',
             '"type":"status"')


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def quality(journal_dir, since_ts):
    """Per bot since `since_ts`: markout 60 s after each fill (book mid of our token minus the price, cents per
    share: the main metric of reports/40), windows with a pair / one side only, order latency, event-loop lag."""
    fv, fills, settles, lat_p, lat_c = {}, [], [], [], []
    lag_max, lag_blocks = 0, 0
    for d in run_dirs(journal_dir, max_age_s=time.time() - since_ts + 3600):
        first_blocks = last_blocks = None
        try:
            f = open(os.path.join(d, "events.jsonl"))
        except OSError:
            continue
        with f:
            for line in f:
                if not any(w in line for w in _Q_WANTED):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("t", 0) < since_ts:
                    continue
                t = e["type"]
                if t == "fv":
                    if e.get("mid") is not None:
                        fv.setdefault(e["key"], []).append((e["t"] * 1000, e["mid"]))
                elif t == "fill":
                    fills.append(e)
                elif t == "settle":
                    settles.append(e)
                elif t == "order_placed" and e.get("latency_ms") is not None:
                    lat_p.append(e["latency_ms"])
                elif t == "order_cancelled" and e.get("latency_ms") is not None:
                    lat_c.append(e["latency_ms"])
                elif t == "status":
                    st = e.get("stats") or {}
                    lag_max = max(lag_max, st.get("lag_max_ms", 0) or 0)
                    if st.get("lag_blocks") is not None:
                        first_blocks = st["lag_blocks"] if first_blocks is None else first_blocks
                        last_blocks = st["lag_blocks"]
        if first_blocks is not None:
            lag_blocks += last_blocks - first_blocks
    num = den = 0.0
    for e in fills:
        tr = fv.get(e["key"], [])
        j = bisect.bisect_left([x[0] for x in tr], e.get("ts", e["t"] * 1000) + 60_000)
        if j < len(tr):
            mid = tr[j][1] if e["side"] == "U" else 1 - tr[j][1]
            num += (mid - e["price"]) * 100 * e["size"]
            den += e["size"]
    traded = {e["key"] for e in fills}
    settles = [e for e in settles if e["key"] in traded]    # not wallet leftovers settled again after a restart
    paired = sum(1 for e in settles if min(e.get("up", 0), e.get("down", 0)) >= 0.8 * max(e.get("up", 0), e.get("down", 0)) > 0)
    one = sum(1 for e in settles if min(e.get("up", 0), e.get("down", 0)) < 1e-9)
    return {"fills": len(fills), "markout60": num / den if den else None, "windows": len(settles), "paired": paired,
            "one_sided": one, "pnl": sum(e.get("pnl", 0) for e in settles),
            "place_ms": (_pct(lat_p, .5), _pct(lat_p, .95)), "cancel_ms": (_pct(lat_c, .5), _pct(lat_c, .95)),
            "lag_max_ms": lag_max, "lag_blocks": lag_blocks}


def format_quality(hours=24):
    """Quality table for the hourly report (reports/42 items 5-7): judge v3 by markout, not by a few windows."""
    since = time.time() - hours * 3600
    out = [f"за {hours:g} ч; маркаут-60 = цена нашей стороны через 60 с минус цена покупки, ¢ на акцию (> 0 хорошо)"]
    for unit in BOT_UNITS:
        journal, _, mode = bot_paths(unit)
        if not journal or not run_dirs(journal, max_age_s=hours * 3600 + 3600):
            continue
        q = quality(journal, since)
        if not q["fills"] and not q["windows"]:
            continue
        mk = f"{q['markout60']:+.1f}¢" if q["markout60"] is not None else "—"
        line = (f"{unit} [{mode}]: исполнений {q['fills']}, маркаут-60 {mk}, окон {q['windows']} "
                f"(пара {q['paired']}, одна сторона {q['one_sided']}), P&L {fmt_money(q['pnl'])}")
        if q["place_ms"][0] is not None:
            line += f"; заявка {q['place_ms'][0]:.0f}/{q['place_ms'][1]:.0f} мс (медиана/p95)"
        if q["cancel_ms"][0] is not None:
            line += f", отмена {q['cancel_ms'][0]:.0f}/{q['cancel_ms'][1]:.0f} мс"
        line += f"; лаг цикла до {q['lag_max_ms']} мс, блокировок {q['lag_blocks']}"
        out.append(line)
    w = wallet_line()
    if w:
        out.append(w)
    return "\n".join(out)


def format_pnl(days=7):
    out = []
    for unit in BOT_UNITS:
        _, state_file, mode = bot_paths(unit)
        by_day = risk_state(state_file).get("realized_by_day", {})
        if not by_day:
            continue
        out.append(f"{unit} [{mode}]")
        for d in sorted(by_day)[-days:]:
            out.append(f"  {d}: {by_day[d]:+.2f}$")
        out.append(f"  итого: {sum(by_day.values()):+.2f}$")
    return "\n".join(out) or "пока нет закрытых окон"


def format_trades(n=10):
    rows = []
    for unit in BOT_UNITS:
        journal, _, _ = bot_paths(unit)
        if not journal:
            continue
        for e in events_since(journal, time.time() - 3 * 86400):
            if e["type"] == "settle":
                rows.append((e["t"], unit, e))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return "сделок пока нет"
    out = []
    for t, unit, e in rows[-n:]:
        hhmm = datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")
        pos = f"U{e.get('up', 0):.0f}/D{e.get('down', 0):.0f}" if "up" in e else f"{e.get('side', '')}"
        out.append(f"{hhmm} {unit.replace('pmbot-', '')} {e.get('key', '')} → {e.get('winner')} {pos} "
                   f"{e.get('pnl', 0):+.2f}$")
    return "\n".join(out)


def format_server():
    h = server_health()
    out = []
    if h.get("load"):
        out.append(f"CPU load: {h['load'][0]:.2f} / {h['load'][1]:.2f} / {h['load'][2]:.2f} (ядер: {h['cpus']})")
    if "mem_total_mb" in h:
        out.append(f"RAM: свободно {h['mem_avail_mb']} из {h['mem_total_mb']} МБ, swap занят {h['swap_used_mb']} МБ")
    out.append(f"диск: свободно {h['disk_free_gb']:.1f} из {h['disk_total_gb']:.1f} ГБ")
    if "rec_gb" in h:
        out.append(f"записи рекордера: {h['rec_gb']:.1f} ГБ")
    if "uptime_s" in h:
        out.append(f"аптайм сервера: {fmt_age(time.time() - h['uptime_s'])}")
    return "\n".join(out)
