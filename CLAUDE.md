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
  **Full review (`reports/53_full_review.md`, tapes of 1-9 Nov in `reports/53_tools`; the LM3 / L3-15 config comments call it reports/52)**:
  the L3 edge is thin and lives in fresh signals (a signal of the same second +4.5c a share and 2.2 % lost, one already 1 s old
  +2.1c and 5.4 %; 1-5 Nov about zero), so order latency is money; more edge sits at asks 0.80-0.90 (L4's zone, 8 % lost, +5c)
  and on the maker side of the decided side (2.2 % lost vs 4.2 % for takers, no fee; the queue is the open question, BTC's
  spread is one tick with ~850 shares ahead, SOL / XRP leave room). One entry is ~31 % of a $15 wallet: grow the wallet, not
  the size, and only after 50-100 live entries match the paper twin. Live FAK orders ask for whole shares (`fak_budget` in
  `pmbot/bot/lock.py`: the fee goes on top of the notional; with the budget equal to the notional the SDK made 4.97-share
  orders with 5 decimals on a 0.001-tick market, the likely cause of "invalid amounts"). The Chainlink feed of every lock bot
  is the legacy RTDS socket, announced for removal: run `scripts/polybolt_probe.py` on the server and move to PolyBolt.
  **`reports/58`**: `lock.shock_sigmas` (3.0 in the L3 family, 0 = off in L99): the shock pause threshold is
  max(shock_bp, shock_sigmas x sigma x sqrt(3)). The fixed 2.5 bp was on 50-95 % of the time once sigma reached ~2 bp
  (10 Nov from 13:00 UTC: live and its twin made 1 entry in 2.8 h); on the tapes the entries at sigma >= 1 bp are the best ones.
  **`reports/59_loss_18utc_bottom_tier.md` (10 Nov 19:00)**: live L3 and its twins (`lock_live.toml`, `lock_tier.toml`,
  `lock_tier_pb.toml`) run THREE tiers, `theta = 0.95`: the p 0.90-0.95 tier (ask <= 0.91) is removed. With the floor and the
  pause every loss sat there (live 13 entries / 2 lost / -$3.58 vs 10 / 0 / +$3.25 above; replay 7-10 Nov 102 / 4 vs 91 / 0);
  a win is ~+$0.45, a loss ~-$4.58. L4, L3-15 and LM3 keep four tiers on paper (`FOUR_TIERS` in `tests/test_lock.py`). Not
  proven and not live: "bottom tier from p >= 0.93" (fitted on 3 losses) and a "price moved against the side in the last
  20 s" filter (no effect on the unfiltered rule's 21 losses). Tools: `reports/59_tools` (`feat.py`, `base.py`, `tiers.py`).
  **`reports/60_late_bottom_tier.md` + `reports/61_rare_losses_and_capital.md` (brother, 10 Nov 19:30; analysis, nothing of it is
  live)**: `lock.late_tier_rel` / `late_tier_p` exist in the code and are OFF in every config but the paper experiment below (tiers below `late_tier_p` 0.95 would
  count only from window second `late_tier_rel`; `why = tier_early`). Evidence: 4 of the 5 live L3 losses of 9-10 Nov were entries
  at <= 0.91 before second 260 (34 entries, -$4.25; the other 22: 1 lost, +$2.82); tapes 1-9 Nov, bottom tier before second 260:
  189 entries, 14 lost, +1.5c; from 260 on: 75, 2 lost, +6c. It is the candidate for bringing the bottom tier back: paper unit
  `pmbot-lock-tier-late` = `config/lock_tier_late.toml` (the twin's rule + the fourth tier from second 260, daily limit $50 so a
  loss does not end the day; not enabled at boot; exam: 100 entries with p < 0.95, <= 4 lost, then the owner decides). Report 61: one loss costs 10 wins at 0.90, 20 at 0.95, 106 at 0.99, so "no losses"
  is the wrong target (the target is a loss rate 2-3x below the one priced in); selling an entry when the model falls to 0.70 cut
  7 of 11 losers to about -$2 and sold 6 winners (same total, smaller drawdowns; no sell code in the lock bot yet); capacity is
  50-100 shares an entry (BTC; ETH <= 50, SOL / XRP 5-10), i.e. a $1000-2000 wallet; grow by stages, each after ~200 live entries.
  **`reports/62_night_loss_11nov.md` (11 Nov)**: live loss BTC 01:10 (p 0.952 at 0.93, market-wide spike-and-fade, lost by
  0.17 bp); the night was +$0.59 real money. NO rule change: a sigma floor of 0.6 shows 0 losses on 7-11 Nov only because it
  removes that night's two losers (costs ~$1.7/day in the other 7 periods); on all ~1000 windows the floored model is not
  overconfident in a calm market (p 0.95-0.97: 1.8 % lost vs 4.4 % promised). Floor 0.6 = first candidate, re-check by replay
  on new recordings (`reports/62_tools/floor_calib.py`). The hourly report now shows real money and paper totals apart.
  **`reports/54_speed_server_feeds.md` (10 Nov)**: the server is 25-30 ms from the exchange on a warm connection and signs an order
  in 7-12 ms, the rest of an order's ~240 ms is the exchange: do not move or upsize the server for speed. The SDK pool drops a
  connection idle for 30 s, so the live lock bot keeps it warm in the closing minute (`TakerLiveBroker.ping`, `LockBot.warm_loop`,
  `stats.ping_ms`). `polymarket.com/api/crypto/crypto-price` `openPrice` is NOT the resolution strike (0.01-8 bp off). PolyBolt
  (`scripts/polybolt_probe.py`) works with our credentials: prints 0.25 s earlier than RTDS, no dropped seconds, its 60 s TWAP =
  mean of [t-62, t-3]: moving the bots to it is the next speed step. `deploy/cleanup.sh` rolls the recorder at 4 GB and gzips
  finished `clob.jsonl` files (the disk hit 91 % on 10 Nov).
  **`reports/55_loss_10nov_competitor.md` (10 Nov 13:00)**: live L3 + twins also run `floor_free_p = 0.99` (a side the UNFLOORED
  model gives >= 0.99 is judged by it = the original L rule; the floor alone had removed those entries) and `shock_bp = 2.5`,
  `shock_s = 10` (no entry within 10 s of a 3 s Binance move > 2.5 bp either way: the live loss of 10 Nov 10:25 came 3 s after a
  2.9 bp jump). Replay 7-10 Nov, Gamma outcomes, no limits: 157 entries, 2 lost, +$46.7 (floor only: 155 / 6 / +$37.9; plain L3:
  371 / 22 / +$50.8). `pmbot-lock-99` = `config/lock_99.toml` (L99, paper): p >= 0.99 at asks 0.96-0.99, the competitor's
  zone (his "100 % wins" table leaves out a lost $73.72 stake); 91 entries / 0 lost in replay, break-even is 1 loss in 107: paper
  until ~1000 entries. Tools: `reports/55_tools/union.py`, shock columns in `reports/52_tools/grid.py`.
  **`reports/56` + `reports/57` (10 Nov 13:00-13:40)**: a SECOND REAL-MONEY unit `pmbot-lock-99-live` = `config/lock_99_live.toml`
  (L99 "Снайпер": twin of `pmbot-lock-99`, 5 shares, one entry at a time, daily limit $4.50 so a loss stops it; owner's word
  "запусти его еще на реальную тоже"; do not raise the stake before ~300 live entries; the owner wants both live rules merged
  into one bot later). Telegram names: Ступени = L3, Снайпер = L99, Ступени без фильтров = L4, Мейкер = LM3; `/status` and the
  digest show real money / paper twins / paper experiments with separate totals (`notify.GROUPS`, `TWINS`). `execution.cl_feed` =
  `rtds` | `polybolt` | `both` (`feeds.polybolt_feed`, first arrival of a print wins); `pmbot-lock-tier-pb` =
  `config/lock_tier_pb.toml` is the paper A/B of PolyBolt against `pmbot-lock-tier` (`stats.cl_lag_ms` in `status`); live stays
  on RTDS until that A/B has 2-3 days. LM and LM2 units are stopped and disabled.
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
