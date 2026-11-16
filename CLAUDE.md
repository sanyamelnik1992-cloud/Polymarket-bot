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
  **`reports/73_15m_strong_entries_10_shares.md` (15 Nov, owner: "выберем 15 минутки ... может увеличить деньги")**: 15m is the only
  live bot in profit (42 entries / 1 lost / +$27.49 in 3.6 days, ~$14 of it lucky fills far below the seen ask; twin 54 / 3 / +$8.44).
  Live + twin, 64 unique windows: entries from second 260 or with p >= 0.97 lost 0 of 23, the early weak ones 3 of 41 (+1.7 c a
  share). `lock15_live.toml` + twin `lock15_tier.toml`: `big_shares = 10` when p >= `big_shares_p` 0.97 OR the window second >=
  new `lock.big_late_rel` 260, `big_calm_s = 3`, 5 shares otherwise; risk `max_usd_per_entry` 10, `max_open_usd` 10,
  `max_open_entries` 1, `daily_loss_limit` 10 (REAL MONEY, raised on the owner's word). The entry rule is unchanged (four tiers from
  second 240). Next: 20 shares on the strong ones after 30 strong live entries with <= 1 lost; back to 5 at >= 3. Entries are
  limited by sellers leaving the book (1300 of ~1400 windows) and FAK misses (21 windows); 15m markets also exist for DOGE, BNB,
  HYPE (Chainlink already recorded, Binance lists them): paper unit `pmbot-lock-15-alt` = `config/lock15_alt.toml` (the twin's
  [lock] on `assets = ["doge", "bnb", "hype"]`; `lock.PAPER_ASSETS` are refused in a live config; no tails of their own, BTC's
  are used); to live after 50 paper entries with <= 5 % lost, on the owner's word. Tool: `reports/73_tools/feat15.py`.
  **`reports/74_insurance_small_entries_and_dead_ends.md` (15 Nov 17:30)**: `lock.hedge_min_shares = 5` in `lock_live.toml` + twins:
  no insurance for a position under 5 shares (the $1 exchange minimum made the insurance of a 2-share entry a 4-6 share counter-bet;
  both live insurance fills, 14 Nov 14:29 and 15 Nov 14:09, were on entries that WON: -$1.27, -$1.41; 0 of 7 losses were ever insured;
  fills are adversely selected: a true reversal runs away in 0.3 s, a false alarm fills). 5m since 14 Nov: 15 entries, 1 real loss.
  Checked and rejected: paying up after a FAK miss (15m: 21 missed windows, 2 lost = 9.5 %, next asks 0.94-0.99), maker LM3 (145 /
  18 lost / -$35 on paper), L4 (344 / 35 / +$13.98, uneven). Capacity: books hold 5-50 shares at our price, so the 15m rule is
  worth roughly $5-20 a day at 10-20 shares, not a strategy that scales with capital. Next candidates: size stages (report 73),
  alt coins after a paper day (DOGE / HYPE Binance ticks are 1.5-1.7 bp: the guards fire too often), last-minute entries on the
  hourly Up/Down markets. Hourly study (report 74 section 5, `reports/74_tools/hourly_lastmin.py`, public trades of 14 days, 672 BTC + ETH
  windows): the winner is Binance 1 h close >= open in 672 of 672; in the closing minute, with Binance >= 3 sigma of the time
  left from the open, the favoured side lost 0 of ~275 windows and still traded below 0.98 in ~4 windows a day (7300 shares,
  ~$40 a day taken by other bots): a speed question. Paper unit `pmbot-hourly-last` = `config/hourly_last.toml` (the old
  hourly engine, closing minute only, $10 an entry, ask <= 0.97, edge >= 2 c, paper fills on the live book 0.3 s later);
  verdict after 30 paper fills (<= 1 lost, > +2 c a share) or < 5 fills in 3 days = close it.
  **`reports/75_15m_bottom_tier_093.md` (16 Nov 06:50)**: 15m live lost 3 entries in 9 h (-$4.59, -$4.58, -$9.16 at 10 shares) and
  stopped itself on the -$10 day stop (16 Nov 03:15 UTC; it stays stopped until the owner restarts it). Every 15m live loss (4 of 56
  entries) was in the bottom tier, model p 0.90-0.93: by the journal states of 2503 windows (`reports/75_tools/state_calib.py`) that
  zone loses 8.0 % against a ~0.906 cost = no edge (p 0.93-0.95: 2.5 %, 0.95-0.97: 2.6 %, 0.97-0.99: 1.4 %, >= 0.99: 1 of 5622); late
  entries of that zone lose 8-10 % like early ones, so the "second >= 260 = strong" half of report 73 (23 / 0) was a small sample.
  Now in `lock15_live.toml` + twin + alt: bottom tier `[0.93, 0.91]` (theta stays 0.90: the journals keep the zone),
  `big_late_rel = 0` (10 shares only at p >= 0.97), `daily_loss_limit = 5`. States with a seller <= 0.95 lose more than all states
  at the same p (adverse selection; 5m p 0.95-0.97: 1.8 % -> 6.3 %). Real money to date: 15m +$15.66 (56 / 4 lost), 5m -$7.17
  (183 / 16), L99 +$0.72 (8 / 0): about zero on 247 entries, not a proven profit - say so to the owner. Review: p 0.93-0.95 tier
  <= 2 lost of 30 live entries, else drop it. Tools: `reports/75_tools/` (entries_by_tier, state_calib, tier_replay).
  **`reports/76_ten_shares_only_at_099.md` (16 Nov 12:45, owner: "нужно что-то менять или мы весь баланс просрём")**: 16 Nov real
  money -$19.95 (15m -$11.98, 5m -$8.06 incl. SOL 12:24 -$9.54 at 10 shares, p 0.974 on a flickering Binance nowcast); all real
  money to date +$0.69 on 252 entries = zero. On real fills (live + twins, `reports/76_tools/by_size.py`) only the p >= 0.99 tier
  earns (33 / 0 lost / +$9.91; L99 20 / 0); 10-share entries at p 0.97-0.99 were 43 / 5 / -$24.07, negative in every bot (a
  seller at <= 0.95 against a 0.97 model is adverse selection: 0.7 % lost on all states, 6-7 % where we can buy). Now
  `big_shares_p = 0.99` in the 5m live family and the 15m family, and 15m `shares = 2` (was 5): 10 shares only at p >= 0.99, 2
  below; the entry rules are unchanged. Do not raise a size anywhere without >= 100 real entries beating the price. Paper ideas
  next: book depth at <= 0.98 for the 0.99 tier. Section 6 (14:00): 5m live + twins `daily_loss_limit = 5` (was 10; owner: "не
  хочу чтобы мы теряли так много"); CHECKED AND REJECTED: "enter only on a fresh signal" - in the journals of 10-16 Nov entries on
  the first tick at a tier were 96 / 3 lost vs 96 / 11 for older ones (`reports/76_tools/fresh.py`), but dense 100 ms paths of the
  tapes 7-11 Nov (`fresh_paths.py build|sim`, tapes in /root/research on the server) show no difference by signal age (81 / 2 vs
  49 / 1). On those tapes the p >= 0.99 tier is 54 / 0 (+3.4..4.3 c a share); the whole rule was 72 / 0 on 7-9 Nov and 58 / 3 on
  10-11 Nov: the edge below 0.99 faded after 9 Nov. Never promise the owner "no losses".
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
  **`reports/72_rewards_live_probe.md` (14 Nov, owner: "запускай тест")**: first REAL-MONEY probe of R, `reports/72_tools/rewards_probe.py`
  (`plan` / `run --real-money` / `payout --date`; one pair of post-only minimum-size bids, <= $25, <= 30 min, exchange-side
  expiration). Malacca-ships market, pool $40 a day, $17.60 in bids for 10 min: no fill, the exchange accrued $0.052
  (`list_user_earnings_for_day`: accrual is by time, not the whole pool for presence); our share (`get_reward_percentages`) was
  75 % for ~5 min, then 1.7 %: other makers posted tighter and larger within ~6 min, competitiveness 0 -> 1.0. The paper bot
  would have booked $0.24-0.28 there ("alone" for hours): its "alone" rewards are overstated, one sample. Next: payout check on
  15 Nov, 3-5 more 30-min probes to time the arrival of other makers, then fix the paper reward estimate.
  **LIVE TEST of the rewards rule, REAL MONEY (report 68 section 12; owner 14 Nov: "давай все делай под реальные деньги, будем
  тестировать")**: unit `pmbot-rewards-live` = `config/rewards_live.toml`, `scripts/run_rewards_live.py --live`,
  `pmbot/rewards/live.py` (`RewardsLiveBroker`, `LiveFarmer`, `choose_market`, `recent_markets`), listed in
  `ops.REWARD_LIVE_UNITS`. A TEST, not a bot, and the longer, Telegram-started form of the probe of report 72: ONE market (price
  0.30-0.70, pool >= $20, no question about a date in the next 7 days, not quoted by a test or probe in the last 48 h), the
  paper rule's two bids of 20 shares (~$18 resting), 60 minutes, the first fill ends it (no hedge, no merge, the shares stay
  in the wallet). Every minute it reads the exchange's own numbers (`get_reward_percentages`, `get_orders_scoring`) next to
  the paper estimate, every 5th reading also the exchange's daily accrual counter of the market
  (`list_user_earnings_for_day`, it moves during the day); journal `rlive`, `rlive_market`, `rlive_fill`, `rlive_done`
  (`exch_earned` vs `paper_reward`, `exch_pct_max` / `_mean` / `_last`, `squeezed_after_min`). `scripts/wallet_balance.py` also
  writes the exchange's reward numbers into `data/state/wallet.json` (`rewards`: numbers and public condition ids only).
  Telegram: `/restart pmbot-rewards-live` starts one test, `/rlive` shows it. Never enabled at boot, `Restart=no`, NOT in the
  auto-update restart list. Rules to keep: it never calls cancel-all (the wallet is shared with the lock bots; orders are
  cancelled by id, then by the market's two tokens), it starts only with >= $60 free ($20 of orders + $40 reserve), the broker
  itself refuses more than 20 shares / $20 resting / a third order / 300 orders a run, orders are post-only GTD (20 min) with
  the order heartbeat, an unanswered order call or a 401/403 ends the test, fills the stream missed are found by `account()` /
  `reconcile()`. The config loader caps the limits (<= $60, <= 60 shares, <= 24 h): growing it into a bot is a separate decision
  of the owner and needs a hedge and merge that do not exist yet. Journal field names must not be `kind`
  (`Journal.log(kind, **kw)`: the first version crashed in a test on that). The probe script of report 72 cancels EVERY open
  order of the wallet when it ends: do not run it while this unit is quoting. Next: 3-5 tests on different markets, then
  correct the paper bot's "alone" rewards by the measured share.
  **Report 68 section 13 (15 Nov)**: both live test fills (Hormuz 14 Nov 20:30, Panama 15 Nov 08:19, 20 NO each, $15.60 held,
  ~-$7 at the mark; rewards from all live runs $0.054) came from the reference price: in a wide book it was the YES token's
  last trade only, while the NO token's last trade was up to a day newer and 16-37c away (the CLOB gives one last trade per
  token and no time). On 15 Nov 124 of 178 wide books of the paper selection disagreed by > 3c. Fix: `mk.reference_price(...,
  last_other)` returns None in a wide book when YES-last and 1 - NO-last differ by more than `max_ref_disagree`; the selection
  fetches both tokens' last trades (`RMarket.last_trade_no`); a trade seen live on either token since subscribing is used
  alone. The paper selection still fills 45 markets (96 of 200 candidates pass). A taker wallet sweeps every cheap quote on
  those markets on both sides: the fix removes a wrong price, not that risk.
  Second live run, 14 Nov 20:30 UTC (`pmbot-rewards-live`, owner: "запусти на тесты стратегию"; report 72 section 5): Hormuz-ships
  market, book 0.35 / 0.93 priced by the last trade 0.57; the NO bid 0.37 x 20 was FILLED 0.1 s after it was placed, test over,
  20 NO shares ($7.40) stay in the wallet until 23 Nov, probably mostly lost. The paper bot had the same fill there at 18:42.
  Fixed in the test's market choice: `recent_markets(..., marks=FILL_MARKS)` skips markets where the rule was filled (paper or
  live) in `fresh_hours`, `markets.date_near` skips a date or a week START within `calm_days` (both samples were "week of
  November 16" markets). Two live samples: $0.05 accrued, $7.40 at risk. Next test: `kind = "shared"`, on the owner's word.
  Insurance (report 71 section 6): live 84 windows / 6 lost / 0 insured / 1 false alarm (-$1.27); unchanged, review after 10 tries.
  **Report 68 section 14 (15 Nov, owner: "сделай пересчёт и заливай в main")**: paper R over 81 h = +$2575 as booked, +$464
  without the "alone" rewards, about -$5 on the markets where others already scored at selection (since 14 Nov, 178
  market-hours); trading -$443 (~-$100 a day). 84 % of the "not alone" rewards came from markets with official
  competitiveness 0 (our share 75-100 %), where the live probe was credited ~1/5 of paper. No proven plus without empty
  pools. Added: `rewards.honest_max_share = 0.5`, `shared_by_day` / `MState.shared_total` (rewards of samples where our
  share <= 0.5 and others score), status `shared_today` / `shared_total` / `honest_today` / `honest_pnl` (= those rewards +
  ALL trading), `rsample.shared`, `rday.shared`; report lines `HONEST`, by-day "others >= half" / "honest net" and a
  `RECOUNT` of all market rows by mean share; Telegram "✅ честный итог" in /status, /r, the digest, /rhow. Selection,
  quoting and the stop are unchanged. Decide after 1-2 days of the honest figure: <= 0 -> no live R, the paper unit can be
  switched off.
  Third live run (owner, Telegram, 15 Nov 08:17 UTC; report 72 section 6): Panama-canal market, NO bid 0.41 x 20 filled after 70 s
  ($8.20), share 0 %. Three "alone" samples: $0.054 accrued, $15.60 of filled bids worth ~$8 by the last trades. A lone quote
  in an empty pool loses real money; `config/rewards_live.toml` is now `kind = "shared"` with `max_share = 0.5` (other makers must hold
  at least half of the pool; on 15 Nov no market of the paper rule fits, so the test ends "no market" and places nothing):
  "alone" returns only by an explicit decision after the paper bot shows fewer fills with the brother's price fix
  (report 68 section 13: wide books whose YES and NO last trades disagree are not quoted). The paper bot's "alone" rewards are not money; judge R by the markets with other makers only.
  15 Nov 07:54 5m loss (BTC, second 287, lead 0.63 bp, -$1.87; our own TWAP said win, the exchange's window said loss): no rule
  change, report 71 section 7 (lead 0.5-1 bp: 18 of 19 won on tapes + live; `reports/71_tools/dist_calib.py`).
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
