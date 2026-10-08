# Do Hyperliquid leaderboard winners stay winners?

A pre-registered test on public Hyperliquid data, run on 25 September 2026. The answer was no. The best perp traders
of one month were not reliably the best of the next, and the rank correlation that did appear followed the market
phase. Verdict: **NO-GO**. The follower simulation never ran, because the rule written before the bulk data said stop.

For people weighing copy-trading, for Hyperliquid API users, and for anyone who wants a worked example of a frozen
protocol with a stop rule.

**Status: archived.** The study is finished. The code runs: one standard-library Python file and 28 offline tests.
The original API responses are not included (they hold third-party account data); a synthetic sample stands in for
them. Built with AI coding agents (Claude, Codex).

```text
$ python copytrade_hl.py --summary results/copytrade_hl_20260925.json
data: https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
verdict: NO-GO (persistence gate failed)
primary test, perp ROI, block 3 -> block 4, n = 99 traders
  rank correlation (Spearman)       +0.54   95% CI +0.34 .. +0.70
  top decile positive in block 4    6/9 vs 46/99 of all traders
  excess share positive, points     +20.2   95% CI -18.2 .. +51.5
rank correlation by block pair      1->2 +0.00   2->3 -0.44   3->4 +0.54
leaderboard top 200 positive next month 82.7% vs 78.1% of active rows, excess 95% CI -1.1 .. +10.1 points
follower simulation: not run: gate failed (pre-registered stop)
```

Quickstart (Python 3.8 or newer, no dependencies, no keys, no network):

```sh
python -m unittest                             # 28 tests, about 15 s
python make_sample.py demo                     # synthetic raw/ data, written by the study's own fetch code
python copytrade_hl.py --offline --root demo   # rebuild a report from demo/raw with network calls disabled
python copytrade_hl.py --offline --root demo   # run it again: it rebuilds, compares bytes, prints MATCH
```

Numbers below come from [`results/copytrade_hl_20260925.json`](results/copytrade_hl_20260925.json) unless they are
marked *(run notes)*: those come from the study's handoff notes, which are not part of this repository.

## The question

Can a small account copy the traders at the top of a public perp leaderboard? The idea was to follow slowly: if
leaders hold positions for hours or days, being seconds or minutes late should cost little. Hyperliquid was chosen
because every account's history is public.

The study asked three questions in order:

- Persistence (the gate): do this month's best traders, picked without looking at the next month, stay above the rest?
- Followability (the follower simulation, step 3 in the code): copy their fills 0 s, 10 s, 1 min, 5 min and 30 min
  late, pay the taker fee and funding. Is anything left? This runs only if the persistence gate passes.
- Scale: can a $500 or $5,000 account mirror the trades at all, given the $10 minimum order?

## Data

- Hyperliquid's public info API (`POST https://api.hyperliquid.xyz/info`): `subAccounts` and `portfolio` for the
  persistence test; `userFillsByTime`, `userFunding` and `candleSnapshot` for the follower simulation.
- The leaderboard file that the Hyperliquid website loads: `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`
  (46,869 rows, 39 MB, last modified 2026-09-25 13:51 UTC). It is not a documented API and may change.
- Limits from the documentation, saved with the run and quoted in the report: a 1,200 request-weight budget per
  minute per IP, a 0.045% taker fee at the base tier, a $10 minimum order, at most 2,000 fills per response and only
  the 10,000 most recent fills, only the 5,000 most recent candles (about 3.5 days of 1-minute candles).
- The run made 929 requests in 37 minutes (1 leaderboard, 400 `subAccounts`, 527 `portfolio`, 1 `candleSnapshot`),
  paced at 70% of the weight budget. Peak load was 56% of the limit *(run notes)*. No HTTP 429 responses, no failures.
  7 of the requests were exploratory and came before the protocol freeze.
- Read-only public data. No orders, wallets, keys or signatures were involved.

## Method

The protocol was written to `raw/protocol_frozen.json` at 14:37 UTC on 25 September 2026, before the bulk fetch. The
report compares the code's protocol with that copy (`protocol_matches_frozen_copy: true`), and the verdict rule was
written down before any result.

- **Trader.** A master account plus its sub-accounts. 10 of the top 200 had more than 10 sub-accounts and were left
  out: the protocol caps a trader at 10 sub-accounts to bound the number of requests.
- **Selection without look-ahead.** The top 200 by all-time PnL as it stood 30 days before the fetch (all-time PnL
  minus the last month's PnL). As a control, 200 accounts spread evenly over the whole ranking
  (positions `floor((i + 0.5) * N / 200)`). One account was in both groups: 399 traders, 389 fetched.
- **Blocks.** Four 30-day blocks back from the fetch time: 28 May to 27 June, 27 June to 27 July, 27 July to 26
  August, 26 August to 25 September 2026.
- **ROI per block.** PnL change divided by the time-weighted mean account value over the block, master plus
  sub-accounts, with at least $1,000 of capital. Primary: perp windows. Secondary: perp plus spot.
- **Primary test.** Rank traders on block 3, check them on block 4. 99 traders had valid perp ROI in both blocks.
  Statistics: Spearman rank correlation; the top decile's share of traders with positive ROI minus everyone's share
  ("excess", in percentage points); the top decile's mean ROI minus everyone's; a 10 x 10 decile transition matrix.
  Confidence intervals: bootstrap over traders, 2,000 draws, seed 20260925, 2.5 and 97.5 percentiles.
- **Gate.** At least 50 traders, and both lower bounds above zero: the rank correlation's and the top decile's
  excess. Pass: run the follower simulation. Fail: stop with NO-GO.

## Result

### Primary test: perp ROI, block 3 to block 4, n = 99

| Statistic | Value | 95% CI |
|---|---|---|
| Rank correlation (Spearman) | +0.54 | +0.34 to +0.70 |
| Top decile (9 traders) positive in block 4 | 6 of 9 | |
| All traders positive in block 4 | 46 of 99 | |
| Top decile excess share positive | +20 points | -18 to +52 |
| Top decile mean ROI minus everyone's (measured) | -11 points | -159 to +108 |
| Top decile still in the top decile | 1 of 9 | |
| Top decile fallen to the bottom decile | 3 of 9 | |
| Bottom decile still in the bottom decile | 5 of 10 | |

The rank correlation passed its half of the gate. The top decile did not: 6 of 9 positive against 46 of 99 is within
what chance gives at this sample size. Means are dominated by a few accounts with very large swings, which is why the
gate uses ranks and shares, not means.

### Skill or market phase?

A rank correlation of +0.54 looks like persistence. Two checks say it came from the market phase of that month.

- Pre-registered: the same correlation for the earlier block pairs was +0.00 (1 to 2, CI -0.23 to +0.23) and -0.44
  (2 to 3, CI -0.61 to -0.23). A month earlier, the ranking had reversed. The mean over the three pairs was +0.03
  (CI -0.09 to +0.16). Those two pairs share the universe selection with their test block, a mild look-ahead, so
  they count as secondary.
- Not pre-registered, added after the result: the same pairs split by group. The reversal shows in the control
  group as well as in the top group, and the control was drawn evenly from the whole ranking, so the reversal is not
  a side effect of how the top group was picked.

| Block pair | Top group: Spearman (n) | Control: Spearman (n) |
|---|---|---|
| 1 to 2 | -0.14 (60) | +0.01 (44) |
| 2 to 3 | -0.32 (58) | -0.51 (45) |
| 3 to 4 | +0.59 (51) | +0.52 (49) |

### Other pre-registered views

| Test | n | Spearman (95% CI) | Top decile positive vs all | Excess, points (95% CI) |
|---|---|---|---|---|
| Perp ROI, block 1 to 2 | 103 | +0.00 (-0.23 to +0.23) | 5/10 vs 51/103 | +0.5 (-30 to +32) |
| Perp ROI, block 2 to 3 | 102 | -0.44 (-0.61 to -0.23) | 3/10 vs 47/102 | -16 (-44 to +15) |
| Perp ROI, 3 to 4, top group only | 51 | +0.59 (+0.32 to +0.80) | 4/5 vs 20/51 | +41 (-5 to +71) |
| Perp ROI, 3 to 4, control only | 49 | +0.52 (+0.26 to +0.74) | 2/4 vs 26/49 | -3 (-36 to +51) |
| Perp plus spot ROI, 3 to 4 | 248 | +0.35 (+0.21 to +0.48) | 16/24 vs 196/248 | -12 (-26 to +11) |
| Dollar PnL instead of ROI, 3 to 4 | 99 | +0.72 (+0.55 to +0.84) | 9/9 vs 46/99 | +54 (+22 to +61) |

The dollar view is the one that looks strong. A post-hoc check (`diag_abs_posthoc.py`) ran the same statistic on
the two earlier block pairs and got 4 of 10 and 5 of 10 *(run notes)*. Dollar ranks mostly sort accounts by size, and
in a month when 78% of active leaderboard rows were positive, big winners staying positive says little about skill.

Whole leaderboard: the 200 accounts with the best all-time PnL a month earlier (179 of them active in the next month)
were positive in that month 82.7% of the time, against 78.1% of all 34,222 active rows: +4.5 points, CI -1.1 to
+10.1. By decile of the past ranking, the share positive fell from 88-89% in the top three deciles to 65-71% in the
bottom four. Past dollar winners were positive more often than past losers in that one month, but the top 200 were
not clearly above the base rate.

### Why only 99 of 389 traders

Most fetched accounts had less than $1,000 of perp capital in block 3 or 4. 114 of the 190 fetched leaders fell below
it *(run notes)*: many of the biggest all-time winners hold spot, not perps. The control group is mostly small
accounts: 19,547 of the 46,869 leaderboard rows hold under $100.

### Scale

Computed from account values only, because the follower simulation never ran. With proportional sizing, a $500
follower would mirror a top-decile leader at about 2% of the leader's size on average (mean capital ratio 0.023); at
$5,000, about 23%. Any leader order that shrinks below the $10 minimum after scaling cannot be copied, so a small
follower misses the leader's small orders entirely.

## Why it stopped

The gate needed both lower bounds above zero. The rank correlation's was +0.34 and passed; the top decile excess's was
-18 points and failed. The rule says stop, so the follower simulation (copy every fill of the followed traders late,
at the close of the candle that contains the delayed time, with the 0.045% taker fee and the leader's funding) never
ran on API data. It is implemented and unit-tested, and in this repository it runs end to end on a synthetic positive
control (below).

## Limits

- Small sample: 99 traders and a top decile of 9, so the confidence intervals are wide.
- Short history: four months, three transitions, in one market phase.
- Survivorship: the leaderboard file keeps 3,764 zero-balance rows, but its inclusion rule is undocumented. Accounts
  that are not in the file cannot be sampled. 33 of the past top 200 now hold under $1,000.
- Noisy ROI denominator: account value is sampled every 2 to 7 days *(run notes)*. Rank tests cope with that;
  means do not.
- Traders whose perps are margined by spot balances are under-represented in the perp windows *(run notes)*.
- The portfolios were fetched 40 to 75 minutes after the leaderboard file was last modified. Master plus
  sub-accounts matched the leaderboard's account value within 5% for 97% of traders without sub-accounts (94% with),
  but the month's PnL within 5% for only 68% (56%).
- The leaderboard's own ROI field has an undocumented formula. Only its PnL figures were used.
- The follower simulation was never run on real data.

## What to do differently

- Repeat the persistence test after more months. Three transitions in one market phase cannot separate skill from phase.
- Put the phase check into the gate in advance: earlier block pairs and the control group, not only block 3 to 4.
- Collect fills and 1-minute candles forward from the first day. The API keeps about 3.5 days of minute candles and
  the last 10,000 fills, so the follower step cannot be rebuilt for older periods.
- Fetch the leaderboard file and the portfolios in one pass.
- Several exchange copy-trading platforms publish the combined result of a leader's copiers (their help pages, read
  in September 2026; not tested here). Given this result, that is the number to judge a leader by, not the leader's
  own return.

## Chapter: copying Polymarket wallets (May and June 2026)

Before this study, copying wallets on Polymarket was tried and stopped. This chapter rests on the author's notes from
that time. The research scripts were never part of this repository, so nothing in it can be rerun here.

- Copying a market-maker bot failed. The copier pays the spread, and an early positive result came from an
  order-book bug, not from the strategy.
- A consensus version over several mid-tier wallets lost on its weak signals and fired its strong signals too rarely
  in the first 35 hours of data.
- A forward paper test of a short wallet list ended negative after about 50 closed trades. Lag and spread were not the
  cause: copy slippage was about zero. Most of the loss traced to one wallet that was set up with too wide a price
  band. The other wallets were near break-even over 33 trades, too few for a verdict.
- Out of sample, wallets picked for good past results regressed. Of 29 wallets picked on a training period, 5 stayed
  positive (17%, where a coin flip gives 50%). A wallet's rank in one period had almost no correlation with the next.
- Fifteen angles in two workflows (selection rules, fading the losers, following the flow) all failed out of sample.
  The closest two, a consensus filter and a big-bet filter, had confidence intervals that included zero and turned
  negative in the last two months.
- A re-check in June found two flaws in the earlier method: a data cap that left about 10 days of history for the two
  top wallets, and flat-size hold-to-expiry tests, which inflate the results of long shots.

The notes' conclusion: a copier who takes prices is on the wrong side; the gains go to market makers and large
wallets. Copy-trading on Polymarket was dropped. The Hyperliquid study tested a different variant: slow following,
where seconds should not matter. Different tests, same answer: past results did not predict the next period.

## Reproduce

**Tests.** `python -m unittest -v` runs 28 tests without network: the study's 17 unit tests (`test_copytrade_hl.py`)
and 11 end-to-end tests (`test_sample.py`) on both synthetic worlds and on the published result file. They pass on
Python 3.8, 3.9, 3.11, 3.12 and 3.13 on Linux.

**Synthetic sample.** `make_sample.py` builds a made-up world (1,000 leaderboard rows, sub-accounts, portfolio
histories, prices and fills) and answers the study's own fetch code through a fake API inside the process, with
sockets disabled. A virtual clock replaces the rate-limit pauses, so `raw/` gets the same layout as a real run in a few
seconds, and the same bytes every time. Nothing in it describes real traders or markets.

```text
$ python make_sample.py demo
...
wrote 1607 files to demo/raw (synthetic world 'null', seed 7)
$ python copytrade_hl.py --offline --root demo
data: https://synthetic.invalid/leaderboard
verdict: NO-GO (persistence gate failed)
primary test, perp ROI, block 3 -> block 4, n = 166 traders
  rank correlation (Spearman)       +0.28   95% CI +0.10 .. +0.46
  top decile positive in block 4    8/16 vs 83/166 of all traders
  excess share positive, points     +0.0   95% CI -25.8 .. +29.4
rank correlation by block pair      1->2 +0.02   2->3 -0.23   3->4 +0.28
leaderboard top 200 positive next month 74.0% vs 70.9% of active rows, excess 95% CI -2.9 .. +9.1 points
follower simulation: not run: gate failed (pre-registered stop)
wrote demo/copytrade_hl_report.json sha256 4c15622c...
```

The default world, `null`, has no lasting skill, a market factor that flips sign between blocks, and a minority of
accounts with huge monthly swings. The study stops there for the same reason as on real data: a significant rank
correlation, but no top-decile excess.

**Positive control.** `python make_sample.py demo2 --world skill` builds a world with lasting skill. On
`--offline --root demo2` the gate passes and the follower simulation runs on synthetic fills, funding and candles.
The follower's result falls as the delay grows from 0 s to 30 minutes, because the synthetic leaders trade 20-minute
moves. This shows that the pipeline can say yes; it is not evidence about any market.

**Live data.** `python copytrade_hl.py --fetch --root study` runs the study on today's data: about 900 read-only
requests over 30 to 40 minutes, paced at 70% of the documented weight budget, and a 39 MB leaderboard download. No
keys. Then `python copytrade_hl.py --offline --root study`. A new run tests a new period under the same frozen
protocol, so expect different numbers. `--fetch-docs` saves the cited documentation pages, and the report then
re-checks the quoted limits against them. Check Hyperliquid's terms of use before you run it; this repository has not.

Optional settings (`HL_STUDY_ROOT`, `HL_API_URL`, `HL_LEADERBOARD_URL`) are listed in [`.env.example`](.env.example).

## Code map

| File | What it is | Status |
|---|---|---|
| `copytrade_hl.py` | The study in one file: fetch, universe, blocks and ROI, statistics, gate, follower simulation, report. Standard library only. | Archived as run, with the changes listed below |
| `test_copytrade_hl.py` | The study's 17 unit tests, on synthetic inputs | Archived as run |
| `diag_abs_posthoc.py` | The post-hoc check of the dollar view by block pair | Archived; relative paths, no per-trader output |
| `make_sample.py` | Synthetic worlds and the fake API | New |
| `test_sample.py` | End-to-end tests and checks of the published result | New |
| `results/copytrade_hl_20260925.json` | The study's report, aggregates only | Data |

Reusable on its own, in `copytrade_hl.py`:

- `Store`: a write-once cache of raw API responses (gzip plus a meta file with the request, the attempts and a
  sha256), sequential fetching paced by documented request weights, a hard request cap and retries.
- `weight`: the documented weight of a Hyperliquid info request, including the extra weight per returned item.
- `paged`: time pagination for `userFillsByTime` and `userFunding`, with duplicates at page edges removed.
- `episodes`: fills to flat-to-flat position episodes, with flips, gaps in the record and positions opened before
  the window.
- `Prices` and `follow_episode`: a delayed follower priced at candle closes, with taker fees, in `Decimal`.
- `series`, `boundary` and `acct_block`: portfolio history to 30-day block PnL and time-weighted capital.
- `avg_ranks`, `spearman`, `deciles`, `transition`, `draws_idx`, `ci` and `pair_report`: rank statistics with a
  seeded bootstrap over traders.
- `block_network`: disables sockets in-process for offline runs.

Specific to this study, kept as it ran apart from the changes below: `PROTOCOL` (it must stay equal to the frozen
copy, or the report says it does not), `universe`, `persistence`, `followability`, `verdict` and `build_report`.

Changes from the version that ran on 25 September 2026:

- Removed from the report: per-account rows (addresses with their ROI, capital and PnL), dollar totals and means,
  account values, the leaderboard ROI means and a table of exchange copy-trading terms. The follower simulation
  reports how many traders it followed instead of who they were. The file in `results/` is the original report with
  the same cuts and a note field that says so.
- `--fetch` now downloads the leaderboard file when it is missing. In the run it was fetched before the protocol
  freeze, outside this script.
- Added `--summary`, the printed headline, the three environment variables, and the data source URL in the report.
  The default report name is now `copytrade_hl_report.json`.
- Unchanged: the protocol and the computations behind every reported number. A test checks the code's `PROTOCOL`
  against the one in the published result.

## License

MIT, see [LICENSE](LICENSE).
