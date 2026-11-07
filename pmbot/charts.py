"""PNG charts for Telegram: cumulative PnL of every bot over the last days (from the settle events of the journals)."""
import io
import time
from datetime import datetime, timezone

from . import ops

# same colour for a bot and its strict twin; the twin is dashed
STYLE = {
    "pmbot-5m": ("A · основной", "#1f6feb", "-"),
    "pmbot-5m-c": ("C · A + строгий", "#1f6feb", "--"),
    "pmbot-5m-b": ("B · 1¢", "#e8590c", "-"),
    "pmbot-5m-d": ("D · B + строгий", "#e8590c", "--"),
    "pmbot-hourly": ("H · часовой", "#6e7781", "-"),
}


def equity_series(days=7):
    """unit -> ([datetime], [cumulative pnl]) over the last `days` days."""
    since = time.time() - days * 86400
    out = {}
    for unit in ops.BOT_UNITS:
        journal = ops.bot_paths(unit)[0]
        if not journal:
            continue
        ev = [e for e in ops.events_since(journal, since) if e["type"] == "settle"]
        if not ev:
            continue
        xs, ys, cum = [], [], 0.0
        for e in ev:
            cum += e.get("pnl", 0) or 0
            xs.append(datetime.fromtimestamp(e["t"], timezone.utc))
            ys.append(cum)
        out[unit] = (xs, ys)
    return out


def equity_png(days=7, title=None):
    """PNG bytes, or None when there is nothing to draw."""
    series = equity_series(days)
    if not series:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5), dpi=130)
    fig.patch.set_facecolor("white")
    for unit, (xs, ys) in series.items():
        name, color, ls = STYLE.get(unit, (unit, "#333333", "-"))
        ax.step([xs[0]] + xs, [0.0] + ys, where="post", color=color, ls=ls, lw=2.2 if ls == "-" else 1.8,
                label=f"{name}: {ys[-1]:+.2f}$")
    ax.axhline(0, color="#999999", lw=0.8)
    ax.set_title(title or f"Накопленный PnL за {days} дн. (UTC)", fontsize=13, loc="left")
    ax.set_ylabel("$")
    ax.grid(alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m\n%H:%M", tz=timezone.utc))
    ax.legend(loc="upper left", frameon=False, fontsize=10)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
