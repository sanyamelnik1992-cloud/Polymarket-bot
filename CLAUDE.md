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
  is already decided, FAK, one entry per window, held to resolution. `pmbot-lock` = `config/lock.toml` (L, p >= 0.99,
  asks 0.90–0.98, paper; was the live rule until `reports/50`), `pmbot-lock-wide` = `config/lock_wide.toml` (L2, 0.50–0.98, paper),
  `pmbot-lock-live` = `config/lock_live.toml` (REAL MONEY since 9 Nov 07:29 UTC on the owner's word "запусти на реальные
  деньги"; not enabled at boot; the rule is L3 = price tiers since 9 Nov 16:27 UTC, `reports/50` + `reports/51`, on the owner's
  "выбери ... и запусти ... зарабатывали а не теряли" (L made 0 live entries; theta 0.98 lost money in replay), same rule as its
  paper twin `pmbot-lock-tier` — a test checks it; limits: 5 shares, $5 open = one entry at a time (`reports/51`), −$5/day). `pmbot-lock-15` =
  `config/lock15.toml` (L15: the L rule on the 15-minute markets, `lock.duration = 900`, paper; same resolution rule,
  30/30 windows checked). `pmbot-lock-maker` = `config/lock_maker.toml` (LM: the L rule with a resting bid <= 0.98
  for the decided side instead of a taker buy, `lock.entry = "maker"`, paper-only, PaperBroker queue model) and
  `pmbot-lock-maker2` = `config/lock_maker2.toml` (LM2, the brother's variant from `reports/46`: bid <= 0.96 and the
  Binance guard 1 bp / 3 s; a test checks that only these differ). Empirical tails: `config/lock_tables.npz` (rebuild:
  `scripts/build_lock_tables.py`); replay on a recorder extraction: `scripts/replay_lock.py`. Go-live rule: report 44
  section 8; L15/LM criteria: report 45 section 6.
  Price tiers (`reports/47_lock_price_tiers.md`, `lock.tiers` + `tier_limit` in `pmbot/bot/lock.py`): `pmbot-lock-tier` = `config/lock_tier.toml`
  (L3, a lower model probability is allowed only at a cheaper ask; paper twin of live) and `pmbot-lock-tier-wide` = `config/lock_tier_wide.toml` (L4, L3 with
  the price floor 0.50; paper since 9 Nov 16:24, riskier: 7.6 % losses in replay). Go-live rule for L3/L4: report 47 section 6; why live moved
  to L3 early and when to stop it: report 50 section 5. Analysis tools: `reports/46_tools`, `reports/47_tools`, `reports/50_tools` (replay of a
  fresh day vs the paper bots' settles), `reports/51_tools` (recorder replay of rule variants with the live limits).
  **Since 10 Nov (`reports/52_live_gap_and_sigma_floor.md`)**: live L3 and its twins (`lock_live.toml`, `lock_tier.toml`, `lock15_tier.toml`)
  run with `lock.sigma_floor_bp = 0.5` (the model never uses a 1 s sigma below 0.5 bp: no entries on a 1-2 bp lead in a calm
  market; only stricter than plain L3; back to plain L3 = 0). L4 (`lock_tier_wide.toml`) stays without the floor as the reference.
  The brother's `claude/keen-tesla-ns5ptk` is merged: a live FAK that matched nothing is a miss and the window stays open (it used
  to block the window: 30 of 65 live orders that night, 11 paper winners missed), prewarm, in-flight orders count in `max_open_usd`,
  `lock_entry` / `lock_miss` are journalled at last; paper units `pmbot-lock-15-tier` (L3 on 15m) and `pmbot-lock-maker3` (LM3) exist,
  not started. Facts to remember: the exchange averages [T-62, T-3] / [T+238, T+297] in 2/3 of the windows and ONE SECOND LATER in
  1/3 (806 windows; flips ~0.2 % of outcomes when the price moves fast), so recorder extractions labelled by our own rule
  (`prep_day.py`) must take close windows from Gamma (`reports/52_tools/gamma_outcomes.py`); Gamma publishes `priceToBeat` only after
  the window ended, so `strike_check_loop` never fires. Rule research on recordings: `reports/52_tools` (`grid.py`, `sim.py`,
  `floor_scan.py`); review rule after 50 live entries: report 52 section 6.
- `pmbot-5m-g2` = `config/g2.toml` (brother, `reports/46`): G with `strategy.favorite_only = 0.02` (opening bids only on
  the side priced >= 0.52), paper, judged against G.
- Stopped and disabled 9 Nov (owner: "не нужное с сервера можешь удалить"): F (`pmbot-5m-f`) and H (`pmbot-5m-h`);
  at 12:55 UTC also G, G2 (makers with a 10 ms tick, 20 % of a core each) and `pmbot-arb` (11 %, no set in 24 h) to
  free the CPU for live L (owner: "с сервером беда ... мы не успеваем", `reports/49_server_diagnosis.md`).
  Makers stay paper only: the owner does not want a maker live while it has many losing windows, and in
  live our own bid would sit in the public book that `target_quotes` reads (G improves the best bid by 1c, so it would
  ratchet itself up to ask - 1c; the books of Up/Down are mirrored, so our Up bid is also a Down ask). Fix that before
  any maker goes live again.
- **Server facts** (9 Nov): is*hosting KVM VPS in Dublin, 2 vCPU Xeon Gold 6154, 2 GB — NOT AWS (no CPU credits, no
  IMDS). The VPS host steals 7-32 % of the CPU time (`vmstat` column `st`); keep the process count low, live first
  (`CPUWeight=500` vs 20 for paper/recorder). Latency from here: CLOB book updates ~20 ms after the exchange stamp,
  Binance ~130 ms (exchange in Tokyo), Chainlink RTDS ~1.4 s (inherent). `/server` and the hourly report show
  busy / disk wait / steal / pressure lines (`ops.load_report`).
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
