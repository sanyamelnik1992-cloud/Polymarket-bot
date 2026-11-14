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
  **`reports/63_two_losses_market_doubt.md` (11 Nov)**: two live losses in the p 0.95-0.97 tier. Key fact: judge a tier by the windows
  where the bot BUYS (market-conditioned calibration, `reports/63_tools/market_calib.py`): p 0.95-0.97 with the market selling at
  0.90-0.93 lost 5.9 % (8 % on 10-11 Nov, +1.4 c a share), while the same p with the ask >= 0.97 lost 1.6 %. Removing the tier was
  proposed and REJECTED by the owner ("мы не должны их убирать, мы должны как-то по-другому страховаться"): the tiers stay.
  **`reports/64_reversal_insurance.md` (11 Nov)**: insurance on a reversal, `lock.hedge_p` / `hedge_margin` / `hedge_slip` +
  `LockBot._hedge_once` (on in `lock_live.toml` and the twins `lock_tier`, `lock_tier_pb`, `lock_tier_late`; off in the 15m bots,
  L4, L, L2, L99, makers). After an entry, when the model (no entry filters) favours the other side with p >= 0.65 and it sells at
  <= p - 0.20, buy as many whole shares of it once (cap: seen ask + 2 c, the live FAK is a dollar budget): both sides pay `shares`,
  the window ends at a known -$1..-3 instead of -$4.7. Not held back by max_open / the day stop (it only shrinks the worst case),
  journal `lock_hedge` (worst / best) and `lock_hedge_miss`, Telegram "🛡 страховка" in live. Evidence: real entries 10-11 Nov live
  -$1.55 -> +$0.67, three paper bots +$22.5; live-rule replay 7-11 Nov +$16.81 -> +$18.22 (no trigger at all on 7-9 Nov); the
  unfiltered rules lose ~$10 on it in the calm 7-9 Nov. Right after a reversal the model is over-sure (0.80 vs 63 % won), hence
  the 20 c margin. Full-path grids: `reports/64_tools/grid_pmin.py` with `GRID_P_MIN=0.5`. Review after 10 live insurances.
  **`reports/65_15m_live_more_trades.md` (11 Nov)**: a THIRD real-money unit `pmbot-lock-15-live` = `config/lock15_live.toml` (the rule
  of its paper twin `pmbot-lock-15-tier`, four tiers, 15m windows, no insurance; 5 shares, one entry at a time, -$5/day stop, no
  auto-resume, not enabled at boot: the owner starts it). Evidence: paper 15 entries / 15 won / +$6.00 in 28 h; replay 7-11 Nov on
  the recorder's Chainlink + Binance with fills from the exchange's own 15m trades (`reports/65_tools/dl_15m.py`, `cands15.py`):
  54 entries, 1 lost, +$10.76 one at a time; with live 5m (+ insurance) every UTC day of 7-11 Nov positive, +$28.98 vs +$16.81.
  The 15m bottom tier (p 0.90-0.95 at <= 0.91) did NOT lose there (22 / 0) unlike on 5m. The recorder does not write 15m books yet.
  **`reports/66_5m_faster_prices.md` (11 Nov 21:00)**: live 5m (`lock_live.toml`) runs `execution.cl_feed = "both"` (PolyBolt first,
  RTDS fallback) after the paper A/B since 10 Nov 16:10: PolyBolt twin 33 windows / -$0.49 / 0.09 FAK misses per entry vs RTDS twin
  27 / -$4.98 / 0.26. Its rule twin is still `pmbot-lock-tier` (RTDS), its feed twin `pmbot-lock-tier-pb`. 15m live and L99 stay on
  RTDS. Not taken: min of the two windows' p (fewer trades, less money).
  **`reports/67_ten_shares_upper_tiers.md` (11 Nov 21:30, owner: "давай 10")**: live 5m and its twins buy `lock.big_shares = 10` when
  the model p >= `big_shares_p` 0.97, 5 below; risk: `max_usd_per_entry` 10, `max_open_usd` 10, `max_open_entries` 1 (still one
  window at a time), `daily_loss_limit` 10 (a second loss stops it, not the first). Replay 7-11 Nov with the insurance and these
  limits: +$28.12 vs +$18.22 at 5 shares, every UTC day positive, worst trade -$4.44; without the insurance the 10 shares would have
  had a -$9.53 loss. 15m live and L99 keep 5 shares. Back to 5 (`big_shares = 0`) if the upper tiers lose > 3 % over 30 entries.
  **`reports/69_insurance_min_order_two_feeds.md` (12 Nov)**: the exchange refuses a marketable BUY under $1 ("invalid amount ...
  min size: 1"): on 12 Nov 07:24 the insurance of a SOL loss (Up at 0.09, 5 shares = $0.55) was refused and the window lost -$4.68
  instead of ~+$4. `MIN_BUY_USD = 1.05`: a cheap insurance buys ceil(1.05 / price) whole shares. All live bots and their twins now
  read Chainlink from PolyBolt + RTDS (`cl_feed = "both"`; RTDS alone timed out ~10 times that night); `pmbot-lock-tier` stays on
  RTDS as the A/B reference. Binance had no outage. Live since 11 Nov 20:50: 5m 24 / 2 lost / +$0.27 (10-share 10 / 0 / +$4.71,
  bottom tier 14 / 2 / -$4.00), 15m 9 / 0 / +$17.28.
  **`reports/71_early_entries_calm_size.md` (14 Nov, owner: "можем как-то исправить ... на сервер заливай")**: 13 Nov 5m live
  26 / 3 lost / -$7.71. Every lost window of live + twins since 10 Nov 18:52 (9 of 120) was bought before second 254. Live 5m
  and its twins (`lock_live`, `lock_tier`, `lock_tier_pb`, `lock_tier_late`) now run `late_tier_rel = 260` with `late_tier_p = 0.97`
  (the p 0.95-0.97 tier only from second 260: 47 windows / 7 lost before, 32 / 0 after) and `lock.big_calm_s = 3` (10 shares only
  if no Binance guard, `GUARD_WHYS`, blocked the window in the last 3 s, else 2; `lock_entry` gets `calm: false`: both 10-share
  losses came 1.2-1.3 s after a `binance_against` block; evidence thin, 2 of 12 vs 0 of 29, hence smaller size, not a ban).
  Tapes 7-11 Nov: +$24.60 -> +$25.67, worst trade -$9.53 -> -$1.91, 130 -> 108 entries; costs ~$2 a day in a loss-free period.
  The insurance has never filled live (FAK 250-330 ms behind the reversal). 15m unchanged (32 entries / 1 lost / +$22.86 since 12 Nov). Tools:
  `reports/71_tools` (`paths.py` state paths of the live rule, `variants.py`, `journal_feat.py` for the bots' journals).
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
- **Liquidity-rewards farmer R, "Награды"** (`reports/68_liquidity_rewards.md`, `pmbot/rewards/`, `scripts/run_rewards.py`,
  `scripts/report_rewards.py`; unit `pmbot-rewards` = `config/rewards.toml`): PAPER ONLY (the config loader refuses anything else),
  not enabled at boot (`/restart pmbot-rewards`), listed in `ops.REWARD_UNITS` (its own journal: `rquote`, `rfill`, `rhedge`,
  `rsample`, `rstatus`, `rmarkets`), shown in `/status` and in the hourly report apart from the bots' totals. Polymarket pays
  ~$115k a day in USDC to makers resting two-sided quotes near the price in ~11 000 markets. Facts (11 Nov):
  `market_competitiveness` x `rate_per_day` = the total qualifying score of all makers (our share = our score / (that + ours));
  an order scores only after resting `r.moas` = 30 s (GET `/clob-markets/{cid}`: also the fee rate `fd.r`, tick, neg-risk); what
  any wallet is paid is public (Data API `/v2/user-pnl`: `reward_income`, `maker_rebate`, `position_pnl`; `/v2/activity?type=REWARD`).
  1400 wallets get >= $1 a day; the 498 "pure farmers" lose 45 % of their rewards on positions, the median one keeps 28 %, a
  third lose money. The two largest (each ~$7k a day of rewards, ~$2.8k net) rest the minimum size on both outcomes in 1000+
  markets and exit 97 % of their fills within 5 s by buying the other outcome and merging (1.3 c a share + fee). The bot copies
  that: minimum size, `dist_cents` 2 when others score in the zone, `alone_frac` 0.85 of the max spread when nobody does, orders
  kept 30 s, hedge from the live book if the pair costs <= $1.06, 10 min cooldown, 2 fills per market per 24 h, budget = bids +
  positions; not quoted: deadline questions (`deadline_regex`: "... by November 30?", "before 2027": kept 1 % of their rewards
  in the replay), markets with > $500 of 24 h volume, expected reward < $2 a day; held markets rank `hold_bonus` 1.5x (the first
  paper run went through 163 markets in 1.9 h). Paper fills: trade-through, the queue at our price, and A REAL ORDER AT OR
  THROUGH OUR PRICE IS A FILL (without it an empty market looks free: the informed trader moves the book, there is no print).
  Replay on 1440 markets x 7 days: contested markets (competitiveness > 1) pay nothing to a minimum-size quote; the money is in
  pools nobody scores in yet, and those are temporary (35 % get makers within 5 h, who bring 10-15x our score; Polymarket moves
  the pools within hours). First paper run on live data (1.9 h, before the filters): 35 fills, trading -$53 against $116 of
  estimated rewards of which $88 in empty pools. NOT known and not measurable on paper: whether a lone maker is paid the whole
  pool, whether a pool is split by the minute or over the day (the docs' formula says the day: then two minutes of presence
  take an empty pool, and 77 visits with a median of 100 s were seen in 29 of 200 empty pools in one hour), how often informed
  traders hit a lone quote. Read `cautious_pnl` (the lower reward estimate without the part earned alone, plus trading), not
  `total_pnl`. Next steps (report 68 section 8): paper on the server, then a live "10 minutes in one empty pool" test ($20 in
  orders) that answers the open questions; live needs the owner's word and a separately reviewed live broker (maker orders,
  FAK hedge, merge, the wallet is shared with the lock bots).
  First server day 12 Nov (report 68 section 9): rewards estimate +$359 ($280 alone), trading -$93; the trading-only
  stop fired at ~11:14 and was LOST by the 11:46 update restart (cause not found; the stop and the day base are now
  kept per date, a missing base counts every open mark, `start` journals what was loaded), so it fired again later.
  Fixes: a wide book is priced by its LAST TRADE (`mk.reference_price`, CLOB `/last-trades-prices`), never by its middle
  (Tennessee 2028: book 0.21/0.89 shown at 0.54, trades at 0.88: 5 such fills cost ~$31); questions whose dates are all
  past are skipped (`mk.date_passed`); the stop compares `cautious_today` (trading + rewards from markets with other
  makers; `stop_counts_rewards`).
  13 Nov (report 68 section 10, owner: "подумай как улучшить ... заливай, без реальных денег"): markets where others
  score paid $1.28 and cost $1.50 per quoted hour (alone ones +$2.33 / -$0.33) -> `max_competitiveness = 1`; unpaired
  inventory ($350, marked -25 %) is worked out by `reduce_only` (only the bid that pairs it rests, also in the cooldown,
  after deselection and after a restart: positions keep `RM_KEYS` of their market in the state), pairs are merged at
  once (`merge_pairs`, journal `rmerge`), the exit bid ignores the budget. Telegram: `/r` = R's money in detail (today, total, by day
  from `rday`, positions, stop), `/rhow` = plain explanation (`notify.REWARDS_HOW`), R line in the digest. Nearly all income is now from (almost) empty
  pools: the part only a live test can confirm.
  14 Nov (report 68 section 11, `reports/68_tools/server_hist.py` + `server_markout.py` rebuild the bot's history, fills and
  post-fill prices from the hourly reports): 49 h on the server = rewards estimate +$2030 ($1406 alone), trading -$334, cautious
  +$291, unpaired inventory $378 at cost ($280 at the mark). The rules of 13 Nov did NOT cut the fills (8.5/h vs 7.5/h) or the
  inventory; rewards per hour fell by a third (cause not visible in the old report). Facts: an opening fill is 3.5c a share
  against us at once and it stays (adverse selection, ~$0.70 a fill, the same alone or not); a taker hedge costs 3.8c (holding
  the same positions: 1.7c); the EXIT BID closed 33 positions at -11.2c while holding them was -3.6c (exits within 30 min: -12c
  vs +2c; after 3 h: -9c vs -8c; t = -3.3): right after a fill the reference price is the sweep that filled us. Hence
  `exit_patience_h = 3` (for 3 h after the fill the exit bid pays at most what makes the pair cost 1 + `max_hedge_cost`, then
  it follows the price; `Position.t`, `Farmer.exit_cap`). The journal now has `rmk` (a market's totals when it is dropped) and
  market features in `rmarkets` (comp, vol, days, price, pool), `exit` on `rquote`, `age_s` on `rmerge`; the hourly report
  prints "by kind of market" and "pairs closed by our own bids". Not changed for lack of data: the 0.80-0.90 price zone, market
  kinds, the competitiveness cap. In a selection of 14 Nov 41 of 45 markets had official competitiveness 0: the "cautious" part
  also rests on nearly empty pools, so the live $20 test (report 68 section 8, step 2) is the next real step; REWARD payouts in
  the public Data API are one sum per wallet per day, no per-market breakdown, so public data cannot answer it.
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
