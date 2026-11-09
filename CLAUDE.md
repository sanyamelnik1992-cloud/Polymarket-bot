# Polymarket bot — rules for Claude Code

Research + trading bots for Polymarket 5-minute crypto Up/Down markets. Docs: `PLAYBOOK.md` (what and why),
`RUNBOOK.md` (server), `STRATEGIES.md`, `reports/`. Docs and chat with the owner are in Russian.

## How code reaches the server
- The server (Dublin) follows branch **`main`**: `deploy/autoupdate.sh` runs every 2 minutes, fast-forwards to
  `origin/main`, runs `pytest -q -x`, restarts running bots. Failing tests → rollback + Telegram alert.
- So: **everything pushed to `main` goes live within ~2 minutes.** Work on a `claude/...` branch, run
  `python -m pytest -q`, and merge/push to `main` only when the owner asked for it and tests pass.
- Never rewrite `main` history (force push) — the server only fast-forwards.
- The owner works from the phone: when they say "залей в main" / "в main", merge your branch into `main` and push.
  Within ~2 min the server pulls, tests and restarts; Telegram reports "✅ код обновлён" or "🔴 откатено".
  Commits touching only `*.md`, `reports/` or `tests/` are tested but do not restart the bots.
- The server pushes an hourly report to branch **`server-reports`** (status, PnL by day, `settles.csv`,
  `runs/<unit>.txt`). Read it to see how the bots are doing: `git fetch origin server-reports` then
  `git show origin/server-reports:README.md`. Never commit code to that branch.

## Safety (real money)
- Secrets live only in `/etc/pmbot/pmbot.env` on the server. Never commit keys, tokens or that file.
- Live trading needs `execution.mode = "live"` in the config AND `--live` in the unit's `ExecStart`
  (`deploy/pmbot-5m.service`), plus `PMBOT_LIVE_CONFIRM`. Changing a unit from paper to live, or raising any
  `[risk]` limit, is a real-money decision: only when the owner explicitly asks, and say so in the commit message.
- Keep the risk layers (`pmbot/bot/risk.py`, `filters.py`, kill file `STOP`, heartbeat in `live.py`) intact.

## Layout
- `pmbot/bot/` — engine, strategy, filters, risk, paper/live brokers, replay; `scripts/run_bot.py` runs it.
- `config/*.toml` — bot configs (`colo_v2.toml` = main 5m bot A, `colo.toml` = experiment B; `*_strict.toml` = C/D,
  the same with `max_disagree` 0.04 for a paired test, see `reports/39`).
- **Live (real money)**: unit `pmbot-5m-live` = `config/live_v3.toml` (v3 since 8 Nov: bids at the best bid, opening
  legs only at 0.40–0.60, closing bids through opening filters; BTC only, 5 shares, $6 open risk, −$2 daily stop that
  stays stopped). Its paper twin `pmbot-5m-f` = `config/v3.toml` (must stay identical except execution; a test checks
  it). `pmbot-5m-e` = `config/live_d_twin.toml` is the twin of the old live config `config/live_d.toml`. Compare: `scripts/live_vs_paper.py`. Telegram: `/restart pmbot-5m-live` starts it,
  `/off pmbot-5m-live` stops it, `/stop` stops everything. Paper bots resume by themselves after a daily stop
  (`risk.resume_next_day`), live never does.
- **TWAP-lock bots** (`reports/44_twap_lock.md`, `pmbot/lockmodel.py`, `pmbot/bot/lock.py`, `scripts/run_lock.py`): in the
  last minute of each window buy the side whose closing Chainlink TWAP (prints `[T+238, T+297]`, strike `[T-62, T-3]`)
  is already decided, FAK, one entry per window, held to resolution. `pmbot-lock` = `config/lock.toml` (L, asks
  0.90–0.98, paper, candidate for live), `pmbot-lock-wide` = `config/lock_wide.toml` (L2, 0.50–0.98, paper),
  `pmbot-lock-live` = `config/lock_live.toml` (same rule as L — a test checks it; REAL MONEY: running since 9 Nov
  07:29 UTC on the owner's word "запусти на реальные деньги"; not enabled at boot). `pmbot-lock-15` =
  `config/lock15.toml` (L15: the L rule on the 15-minute markets, `lock.duration = 900`, paper; same resolution rule,
  30/30 windows checked). `pmbot-lock-maker` = `config/lock_maker.toml` (LM: the L rule with a resting bid <= 0.98
  for the decided side instead of a taker buy, `lock.entry = "maker"`, paper-only, PaperBroker queue model) and
  `pmbot-lock-maker2` = `config/lock_maker2.toml` (LM2, the brother's variant from `reports/46`: bid <= 0.96 and the
  Binance guard 1 bp / 3 s; a test checks that only these differ). Empirical tails: `config/lock_tables.npz` (rebuild:
  `scripts/build_lock_tables.py`); replay on a recorder extraction: `scripts/replay_lock.py`. Go-live rule: report 44
  section 8; L15/LM criteria: report 45 section 6.
- `pmbot-5m-g2` = `config/g2.toml` (brother, `reports/46`): G with `strategy.favorite_only = 0.02` (opening bids only on
  the side priced >= 0.52), paper, judged against G.
- Stopped and disabled 9 Nov (owner: "не нужное с сервера можешь удалить"): F (`pmbot-5m-f`) and H (`pmbot-5m-h`).
  G (`pmbot-5m-g`) stays paper only: the owner does not want the maker live while it has many losing windows, and in
  live our own bid would sit in the public book that `target_quotes` reads (G improves the best bid by 1c, so it would
  ratchet itself up to ask - 1c; the books of Up/Down are mirrored, so our Up bid is also a Down ask). Fix that before
  any maker goes live again.
- **Paper experiments vs F** (`reports/42` section 5): `pmbot-5m-g` = `config/g.toml` (front of the queue, no model
  filters), `pmbot-5m-h` = `config/h.toml` (imbalance guard 0.50/0.60). Judge by markout-60 in the hourly report
  (`ops.format_quality`), not by a few windows. B, D, E are stopped since 8 Nov evening (old one-tick-below scheme).
- Real wallet balance: `pmbot-wallet.timer` → `data/state/wallet.json` (numbers only) → `/status` and the report.
- The owner's brother works in `claude/*` branches (his own Claude Code session): read his newest reports and
  reconcile before changing strategy; his branches are merged only when the owner says so.
- `pmbot/ops.py`, `scripts/tg_bot.py` — Telegram control bot (`/status /chart /review /trades /server /log /stop /resume /update`);
  all Telegram texts (HTML) are built in `pmbot/notify.py`, bot labels A/B/C/D live in `notify.BOTS`.
- `deploy/` — systemd units and server scripts (`bootstrap.sh`, `setup.sh`, `autoupdate.sh`, `push_reports.sh`, `cleanup.sh`).
- Python ≥ 3.11 (`tomllib`). Tests: `python -m pytest -q`.
