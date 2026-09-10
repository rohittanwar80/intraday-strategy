# Spec Amendments and Data Findings — `intraday-strategy`

**Applies to:** Intraday Strategy — Decision Spec v0.1
**Amendment version:** 0.1d
**Date:** 2026-09-10
**Status:** Phases 0–3 complete. Execution bundle complete: stops, limit orders
and the gap filter all tested and all NEGATIVE. **The strategy is frozen.**
Holdout unspent, 0 looks, blocked on a feature build.

> Section 9 lists claims that later evidence contradicted — kept rather than
> deleted. Most are the assistant's.
>
> Upstream amendments v0.3f stand unchanged. Where this document disagrees with
> `docs/spec.md`, this document is later and was measured.

---

## 0. THE FROZEN CONFIGURATION

```
bar_times       09:50, 10:10, 10:30, 10:50, 11:10, 11:30   (wide_am)
k               2 names per cross-section per side
sides           both, balanced
max_positions   20 slots, equal weight
allocation      fcfs
unique_symbols  True
stops           NONE
entry           market, open of bar t+1 (next print if absent)
exit            close of the 15:55 bar
```

| | search 2020–22 | confirm 2023–24 |
| --- | --- | --- |
| Sharpe @ 20 bps | 10.699 | **9.949** |
| per-trade net | 70.15 bps | 62.28 bps |
| trades/day | 17.1 | 17.3 |
| annualised vol | 14.10% | 13.68% |
| max drawdown | −3.38% | −3.16% |
| utilisation | 0.853 | 0.867 |
| **confirm / search ratio** | | **0.930** |

Chosen at 17 trades/day over a higher-Sharpe all-day alternative
(`all bars / k=2 / cap=50`, confirm Sharpe 11.15 at 6.59% vol) for execution
load. That trade-off is recorded in §8.5 and is worth revisiting.

**Nothing in this configuration has been seen by an untouched period.**

---

## 1. Holdout window truncated to 2026-08-25 — STRUCTURAL

**`2025-09-02 .. 2026-08-25`**, 249 sessions. Start moved off Labor Day; end
set by the earliest panel end date — upstream §12.5 records `raw/` at
2026-08-25, SPY 08-26, `raw_russell/` 08-27, retried files 08-31.

The binding constraint is `raw/`, not `raw_russell/`: the regime columns come
from the context ETFs, and a bar with Russell prices and no SPY context is not
scoreable. Inventory confirms the Russell cliff is clean — 100% of symbols
through 08-27, then 8%. **No threshold we picked determines this.**

Recorded in `config/paths.yaml`, enforced by an invariant in
`src/data/paths.py` that fails at load if the two dates are edited apart.

---

## 2. Russell split risk — inherited as CLOSED

Upstream §12.1: stage 2's target and features are within-session. See §9.1.

---

## 3. Volume adjustment — tested, NEGATIVE

TNXP settles it: cumulative *k* ~10⁴, dollar volume at the **33rd percentile**.
Under the bug its true volume would be $6.56 per bar. DTIL (0.101), SAFE
(0.263), UP (0.230) agree. `scripts/01` is retained as a recorded negative;
**its verdict string is wrong** (§9.2).

---

## 4. Handover verification — PASSED

64 checks, 0 failures. **`target == exit_price / entry_price - 1`:
max|diff| = 0.000e+00** across all 17,902,858 rows.

Two independent cross-project joins, both exact:

| check | result |
| --- | --- |
| handover `entry_price` vs bar tree open at *t+1* (script 03, sampled) | max rel err **0.0** on 16,858 rows |
| path first-bar open vs `entry_price` (`src/data/bars.py`, full) | max rel err **0.0** on 7,546 trades |
| path last-bar close vs panel `exit_price` (script 12, full) | max rel err **0.0** on 11,775 trades |

Reconciliation against upstream: dev 2023–24 drift −2.87 bps (SD 139.2) vs
−2.87 (SD 139); validation +3.84 vs +3.84; top 1% edge 46.16 vs 46.49; top
0.1% 92.17 vs 92.17.

### The look boundary, as drawn and as enforced

Statistics of `target` **alone** spend nothing. Any statistic **joining `score`
to `target`** is an evaluation.

In code: `load_handover` raises on any non-development window without
`allow_spent=True`; scripts 05, 07, 08 and 10 refuse any window but the search
one; `04_backtest.py` prints a look warning. Holdout looks remain **0**.

---

## 5. The tail is MORE liquid than the universe — inverts spec §6.1

| measure | universe | top 1% | bottom 1% |
| --- | --- | --- | --- |
| median $ volume/bar (2023–24) | $114,012 | **$241,003** | $145,621 |
| 5-min bar range, bps | 19.02 | 46.23 | 25.55 |
| entry-bar print missing | 4.6% | **1.3%** | 6.9% |

The 10.1% no-print figure describes *eligibility*, not the tail. Median tail
entry price is $18–29, so the tick floor is 3.4–5.4 bps against 46–64 bps of
edge and does not bind.

---

## 6. Cost is bracketed, not measured

| bound | assumption | round trip |
| --- | --- | --- |
| optimistic | market one tick wide | **4.9 bps** |
| pessimistic | quote as wide as the whole bar range | ~23 bps |

One crossing, not two — the exit is the 15:55 auction, 8.4× midday volume.

Median tick floor 4.3 bps, **p90 12.94**: the book is not uniformly tradeable
until 20 bps, where only 0.3% of trades sit below their own floor. **All
Phase 3+ numbers are quoted at 20 bps.** Phase 2 quoted 5 bps, which is
*partly impossible* — that is why the same config reads 11.63 in §8.1 and
10.699 in §0.

A drawdown discontinuity sits at 30 bps: −1.71 … −2.27 through 20 bps, then
**−7.37**. Uncomfortably close to the pessimistic bound. Open item.

**Borrow** (§6.2, §13) is about overnight financing, and nothing is held
overnight. What remains is **availability** — a locate is required and there is
no borrow feed in either project. `borrow_bps = 0` is correct on financing and
silent on availability.

---

## 7. Findings that shaped the engine

### 7.1 The panel uses a next-print fill rule — STRUCTURAL, undocumented upstream

731 of 17,589 sampled rows (4.2%) have no bar at *t+1*. **All are entry-bar
misses; zero are signal-bar.** For every one, `entry_price` equals the open of
the next bar that printed, to 1e-6 — 731 of 731.

`src/data/bars.py` implements the same rule and reproduces the panel's entry
price at **zero error on every trade**, confirming it independently. On the
frozen config the rule fires on 3.4% of trades (lower than 4.2% because
`wide_am` at `k=2` selects more liquid names in the more liquid part of the
session). **Maximum observed delay is 60 minutes**, longer than the 20 minutes
the original sample showed — a position that opens at 12:30 has a much shorter
path to 15:55, which matters for anything measured in bars rather than time.

### 7.2 Cost-adjusted edge peaks at 10:50–11:10 — but the reasoning was too narrow

| bar | edge bps | range bps | ratio |
| --- | --- | --- | --- |
| 09:50 | 106.45 | 92.07 | 1.16 |
| **10:50** | 105.03 | 50.90 | **2.06** |
| 14:30 | 3.31 | 32.98 | 0.10 |

This drove the Phase 2 baseline. **Superseded** — see §9.10 and §8.5: picking
bars by edge-to-range optimises per-trade quality and ignores both idle capital
and temporal diversification.

### 7.3 The afternoon fails on arithmetic

15:30, top 1%: edge 4.27 bps against a 5.3 bps tick floor. Not a chosen
threshold. Note this is a per-trade statement and does not transfer directly to
portfolio context — the all-day config includes those bars and beats the
morning-only one (§8.5).

### 7.4 The 15:50 bar is a statistical artefact

Edge 5.52 bps, **t 5.28** — a one-bar hold collapses variance and inflates t.

### 7.5 The short leg is a hedge, not an alpha source

| measure | long | short |
| --- | --- | --- |
| gross per-trade, frozen config | **144.14 bps** | 43.63 bps |
| median Sharpe standalone, 665 sweep configs | 6.03 | **1.84** |
| missing entry-bar prints | 1.3% | 6.9% |
| adverse gap > 20 bps | 10.7% | 14.0% |

The long leg earns **3.3×** what the short does and is better on every
liquidity measure. **But `both` beats `long` in 28 of 30 regime buckets** and
lifts Sharpe from 6.22 to 8.55 at the same capital.

Spec §5.2's conclusion (use long/short) survives; its *reasoning* (short is the
better risk-adjusted side) does not.

### 7.6 `score_pct` is not symmetric

rank/n spanning (0, 1]. `>= 0.999` selects the top name; `<= 0.001` selects
**nothing**. `selection.py` ranks in both directions and selects on the rank.

### 7.7 Appendix A.5's ~30% does not reproduce

Mean gap +0.52 bps (top) and −0.75 (bottom), implying 1.0% and 5.5% inflation
against A.5's ~30%. SE ≈ 0.25 bps. Direction matches, magnitude off by more
than an order of magnitude. Unresolved — Appendix A is unavailable.

**The convention does not change.** Median gap is **exactly 0.00** in every
group at every bar: ~30% of trades have no gap at all.

### 7.8 The daily cap makes timing decisions silently — GUARDED

`k=5` long across 19 bars: 95 raw candidates a day, 42 after dedupe, against a
20-slot cap. `k=5, both, balanced` fills **50% of positions at 09:50** while
looking like an all-day config.

`select()` refuses when `bar_times is None` and the cap binds on most days.
**The guard must run before `balance_sides`**, which clamps each side to
`max_positions // 2` so `per_day` never exceeds the cap and the guard measures
0% binding on a book that is entirely 09:50. In the 756-config sweep it refused
91 configs, concentrated in `all` bars — which is why that bucket's median came
from 17 self-selected survivors and had to be re-tested properly (§8.5).

### 7.9 Accounting: arithmetic returns, compounded drawdown

Returns are summed — the book is rebuilt from cash each morning. But
arithmetic summation has **no floor**: bottom tranche first reported −100.68%
total with **−102.94% drawdown**. Drawdown now runs on the compounded equity
curve. `total_return_pct` is flagged by `metrics.total_exceeds_capital`.

### 7.10 `ann_return_pct` is not a result

A function of `max_positions`. **Sharpe is the invariant.**

---

## 8. Results

### 8.1 Benchmarks (Phase 2, at 5 bps — *partly impossible*, see §6)

| | 2023–24 | 2020–22 |
| --- | --- | --- |
| **strategy per-trade** | **+62.91** | **+63.51** |
| random per-trade (mean of 20) | −5.14 | −5.25 |
| bottom tranche per-trade | −51.36 | −37.94 |
| **strategy Sharpe** | **11.63** | **11.21** |
| random Sharpe (best of 20) | −0.70 | −1.18 |
| bottom tranche Sharpe | −7.69 | −4.64 |
| IWM buy and hold Sharpe | 0.71 | 0.79 |
| IWM intraday Sharpe | −0.15 | +0.14 |
| z vs random | 16.5 | 19.3 |
| fraction of random beating strategy | **0 / 20** | **0 / 20** |

**The ranking carries the result.** Random selection — identical pipeline with
`score` replaced by uniform noise — earns exactly the cost it pays.
Survivorship and small-cap beta would show up there. They do not.

**The ranking is two-sided.** Buying the worst names loses 51 and 38 bps.

**No intraday drift is harvested.** IWM open-to-15:55 Sharpe −0.15/+0.14.

### 8.2 The design sweep — the distribution is the result

756 configurations on the search window, 91 refused, 665 evaluated at 20 bps.

| statistic | min | p25 | **median** | p75 | p90 | max |
| --- | --- | --- | --- | --- | --- | --- |
| Sharpe | −0.054 | 2.501 | **5.930** | 7.236 | 8.429 | 11.582 |

**99.4% of trials profitable.** This is the shape of a real signal, not an
overfit search: when a grid fits noise the median sits near zero and only the
argmax looks good. The argmax is the least interesting number in the table.

Per-dimension medians: **sides** both 7.57 / long 6.03 / short 1.84;
**k** 1→5.34, 2→**6.63**, 3→6.41, 5→6.23, 8→5.71, 15→4.67;
**max_positions** 5.93 / 5.94 / 5.85; **unique_symbols** 5.94 / 5.86.

`k=1` holds the grid's best single trial (11.58) and a **below-median** median
— a textbook best-of-N artefact, later confirmed by its confirm ratio of 0.776
against 0.93–0.95 for everything else (§8.5).

### 8.3 Regime — NOTHING (12 rules, best gain 0.000)

Tested as a **switch**, not a filter: trade every day, change only which side.
Days traded is constant, so the mechanism that killed upstream's eight filters
cannot apply.

**`both` wins 28 of 30 buckets.** Ten of twelve rules select `both` everywhere
and are identical to always-both. The two that switch are **worse on Sharpe**:
−0.306 and −0.475.

**For a switch, TOTAL is the wrong selector.** Both switching rules were chosen
because long's *total* beat both's in one bucket, and both reduced Sharpe.
Upstream's discipline is right for filters and wrong here.

**A consistent counter-intuitive pattern:** short is strongest in risk-on
buckets (SPY above 200SMA 5.23, low VXX 2.77) and weakest in stressed ones
(high VXX 0.28, low breadth 0.31). Plausibly because in calm markets extreme
down-movers are idiosyncratic and revert cleanly, while in stress everything
falls together. Consistent across three unrelated variables.

**Known bug:** tercile-splitting a binary variable (`spy_above_200sma`)
produces a meaningless "mid" bucket. One switching rule came from it.

### 8.4 Consistency checks (2 ledger entries)

| config | search | confirm | **ratio** |
| --- | --- | --- | --- |
| all / k=2 / cap=50 | 11.774 | 11.153 | **0.947** |
| wide_am / k=3 / cap=20 | 10.429 | 9.745 | 0.934 |
| **wide_am / k=2 / cap=20** | 10.699 | 9.949 | **0.930** |
| morning / k=5 / cap=40 | 10.282 | 9.433 | 0.917 |
| morning / k=5 / cap=20 | 9.703 | 7.814 | 0.805 |
| **all / k=1 / cap=30** | 11.579 | 8.983 | **0.776** |

Per-trade edge decays ~10% consistently while Sharpe decays less.

**`development_2023_2024` is CONTAMINATED** — used throughout Phase 2 before
the split was declared. Ratios measure *consistency across two exposed
windows*, not out-of-sample performance. Recorded in `docs/confirm_ledger.json`.

### 8.5 The all-day result, and why timing was re-tested

The main sweep under-sampled all-bars configs (91 of 108 refused) and fixed
`allocation=fcfs`, which front-loads to the morning. A focused run (216 configs,
non-binding caps, `fcfs` and `reserve`) found:

| | wide_am k=2 cap20 | all k=2 cap50 |
| --- | --- | --- |
| confirm Sharpe | 9.95 | **11.15** |
| confirm vol | 13.68% | **6.59%** |
| confirm max DD | −3.16% | **−1.74%** |
| trades/day | **17.3** | 41.6 |
| effective bars | 5.81 | **18.26** |

`effective_bars` = exp(entropy of the bar distribution). All-day configs run
**18.5 of 19** — genuinely uniform, not morning-concentrated. `wide_am` runs
5.8 despite its six-bar label.

**The mechanism is temporal diversification.** 42 positions opened across 19
points in the session share far less common variance than 17 opened mostly in
the first hour. Per-trade edge is worse (38 vs 70 bps) and it does not matter.

`all / k=2 / cap=50` is the better book on every risk measure and holds the
best confirm ratio (0.947). **`wide_am / k=2` was chosen anyway, for execution
load** — 17 trades a day against 42. That is a preference, not a measurement,
and it costs ~1.2 Sharpe and roughly half the volatility.

The sparse-but-spread middle ground (every 2nd/3rd bar across the session,
~11–22 trades/day) was built but **never run**. It is the obvious place to
recover most of the difference.

Also: `unique_symbols=False` collapses all-day configs (8.44 vs 11.58) because
across 19 bars a name takes many slots. The main sweep found this dimension
flat; across all bars it is not.

### 8.6 The execution bundle — three negatives

**Limit orders and the gap filter — NEGATIVE.**

Hypothesis: decline trades that gapped adversely between the signal close and
the entry open, because the premise has expired. A gap filter avoids the
adverse-selection problem of a passive limit (which fills only when price comes
to you, collecting reverters and missing runners) because it *removes* trades
rather than selectively filling them.

Deciles of adverse gap, net bps: 58.8, 80.6, 46.2, 63.1, 87.5, 79.5, 87.0,
62.4, **87.8**, 48.7. **No gradient.** The second-most-adverse decile has the
highest edge in the table.

The placebo settles it. Every threshold was run three ways — skip on adverse
gap (the hypothesis), skip on *favourable* gap (no theory), skip on |gap|
(volatility only):

| threshold | adverse | favourable | absolute |
| --- | --- | --- | --- |
| none | **10.699** | | |
| 20 | 10.210 | 10.382 | 9.853 |
| 10 | 9.317 | 9.803 | 8.186 |
| 5 | 8.625 | 9.487 | 7.151 |

**Adverse never beats favourable.** The nonsense filter does better than the
one with a theory, at every threshold that removes a meaningful number of
trades. Direction carries no information.

Why all three lose: per-trade edge barely moves (70.15 → 70.95 at 10 bps), so
the filter is not finding bad trades. Utilisation falls 0.853 → 0.644, leaving
capital idle. **A filter must remove trades that are worse by enough to pay for
the capital it idles.** This one removes average trades.

**Stops — NEGATIVE, and monotone.**

| stop | 0.5× | 1× | 1.5× | 2× | 3× | 4× | none |
| --- | --- | --- | --- | --- | --- | --- | --- |
| initial | 8.58 | 9.28 | 9.71 | 10.26 | 10.80 | 10.99 | **10.699** |
| trailing | 7.52 | 8.13 | 8.84 | 9.68 | 10.21 | 10.93 | |

Monotone toward no stop. The two rows nominally above baseline stop only 6.5%
and 10.2% of trades — approximately the no-stop strategy, inside noise.

**Max drawdown gets WORSE with every stop**: −3.38% unstopped against −4.00%
to −6.73%. A stop makes the drawdown worse, which is the one thing it is for.

R, against ORB's −0.554 stopped / +0.482 held at a 49.3% stop rate:

| | mean R stopped | mean R held | stop rate | gap-through |
| --- | --- | --- | --- | --- |
| initial 0.5× | **−1.431** | **+8.362** | 30.4% | 10.5% |
| initial 1× | −1.227 | +3.731 | 24.8% | 12.9% |
| initial 2× | −1.119 | +1.509 | 16.1% | 11.8% |

Stopped trades lose **1.4× the risked amount**, not 0.55×, because 10–14% gap
*through* the level and fill worse, plus 10 bps of stop slippage. Held trades
earn **+8.36 R** at 0.5×: the return distribution is heavily right-skewed and a
stop truncates exactly that tail. At 0.5× the median stop fires on **bar 1**.

Trailing is worse than initial at every distance — it ratchets into normal
intrabar noise on names whose bar ranges are ~46 bps, stopping out 38% of
trades with 26% gapping through.

**Why this differs from ORB and reaches the same conclusion.** ORB was
concentrated and rules-only, where a stop was the only risk control. This book
holds 17 positions to a fixed 15:55 exit; **the diversification already does
what the stop is for**, so the stop only removes upside.

---

## 9. Corrections

**The assistant reasoned ahead of the data and was wrong twelve times.**

| # | Claim | Verdict |
| --- | --- | --- |
| 9.1 | "The split audit is the highest-value diagnostic" | **Wrong, already closed upstream.** |
| 9.2 | "12 symbols step by a clean split ratio — SUSPECT" | **Wrong, the test was broken.** ±6% tolerance on a dense ratio list made ~60% of the range "clean"; hit rate 46%, *below* chance. |
| 9.3 | "Spearman +0.392 is the bug's signature" | **Wrong, confounded.** `price_span` is volatility, not cumulative *k*. |
| 9.4 | "Tail median price near $3–10" | **Wrong.** Actual $18–29. |
| 9.5 | "The morning has both the edge and the liquidity" | **Half wrong.** It has the edge and the widest ranges. |
| 9.6 | "Sharpe 11.6 is implausible; long-only should show 25–40% vol" | **Wrong — forgot utilisation.** 8 of 20 slots filled, gross exposure 0.40. |
| 9.7 | "2020–22 should be much weaker if the edge is bias" | **Wrong.** 63.51 vs 62.91. |
| 9.8 | "Doubling up should show signal decay" | **Wrong, backwards.** A name still top-ranked twenty minutes later has *confirmed*. |
| 9.9 | "`k=1` will look good and should be demoted" | **Right.** Confirm ratio 0.776 against 0.93–0.95. |
| 9.10 | "Pick bars by edge-to-range ratio" | **Too narrow.** Ignores idle capital and temporal diversification. |
| 9.11 | "Flat plateaus decay less out of sample" | **Wrong, inverted.** The flattest config (gap 0.000) decayed **most** (0.805); the mild peaks decayed least (0.930, 0.934). |
| 9.12 | "All-day configs will show effective_bars ≈ 4 — morning strategies in disguise" | **Wrong.** 18.5 of 19, and it holds on the confirm window (18.26). All-day is genuinely all-day, and it is the better book. |

### Process note

Seven data-integrity or plausibility concerns were escalated and **all seven
were killed by measurement**. The assistant's priors on this dataset run
pessimistic.

The findings that held — the missing-bar accounting (§7.1), the timing guard
(§7.8), the utilisation confound (§8.4), `effective_bars` (§8.5) — came from
counters and columns in output, not from reasoning.

Three well-formed hypotheses in the execution bundle (§8.6) all came back
negative with working controls. That is the diagnostics doing their job: each
would otherwise have become a feature built on an assumption that does not
hold.

---

## 10. Configuration choices versus structural decisions

**Structural — forced by a clean diagnostic:**

- Holdout truncation to 2026-08-25 (§1)
- Next-print fill rule (§7.1)
- `rank()` rather than a threshold for cuts finer than 1/n (§7.6)
- Compounded drawdown; arithmetic total flagged past −100% (§7.9)
- The timing guard and its position before `balance_sides` (§7.8)
- **Sides = both** (§8.3) — 28 of 30 regime buckets
- **No regime conditioning** (§8.3) — 12 rules, best gain 0.000
- **No stops** (§8.6) — monotone, and drawdown worsens with every stop
- **No gap filter or limit orders** (§8.6) — placebo beats the hypothesis

**Configuration — chosen on development data:**

| choice | value | margin |
| --- | --- | --- |
| `bar_times` | `wide_am` | **A preference, not a measurement.** `all` is better on Sharpe (11.15 vs 9.95), vol (6.59% vs 13.68%) and drawdown, and was chosen against for execution load. Costs ~1.2 Sharpe |
| `k` | 2 | k=2 and k=3 indistinguishable (10.70/10.43 search, 9.95/9.75 confirm) |
| `max_positions` | 20 | **Does not matter** — 5.93/5.94/5.85 across 665 configs. Sets utilisation, not performance |
| `unique_symbols` | True | **Does not matter at `wide_am`** (5.86 vs 5.94). Matters a lot at `all` bars (11.58 vs 8.44) |

**Assumptions, stated and not measured:** spread (bracketed 4.9–23 bps, swept);
1% participation in capacity figures; tick = $0.01; `borrow_bps = 0`.

---

## 11. Open items

1. **Spread.** The binding uncertainty. Not resolvable without quote data.
2. **Survivorship.** Random controls for the ranking, not the level. Both arms
   draw from the same survivor-only universe (spec §6.3). Uncorrectable, and it
   sits under every number in §8.
3. **The sparse-but-spread timing option** (§8.5). `scripts/10_sparse.py` is
   built and unrun. The obvious way to recover most of the all-day advantage
   at acceptable turnover.
4. **The 30 bps drawdown discontinuity** (§6).
5. **`dev_2020_2022` universe drift is −1.48 bps** against upstream's −2.24 to
   −2.87.
6. **Appendix A.5's 30%** (§7.7).
7. **The 1.33× validation overshoot.** Deprioritised. `median_names_per_bar` is
   686/769/**900**, so part is arithmetic; target SD is 167.8/139.2/170.7, so
   validation's dispersion is 1.23× the 2023–24 window but only 1.02× 2020–22.
8. **Data-integrity section** to be inserted into `docs/spec.md` as §11.
9. **`00_verify_handover.py` conflates ERROR and FAIL.**
10. **Tercile-splitting a binary variable** produces a meaningless bucket.
11. **Python 3.9 is end-of-life** and has cost three unrelated syntax limits.
12. **Per-trade cost and vol-scaled sizing** — the two bundle items not run.
    Refinements, not structural.

---

## 12. Phase 5 — the holdout, and what blocks it

**The strategy is frozen (§0), so the holdout can be spent on the thing that
would actually be traded.** There is no gap between validated and traded, which
is why stops were tested first.

### Prerequisites, in order

1. **Verify the `raw/` end date.** 2026-08-25 is inherited from upstream §12.5
   and has never been measured — the inventory scan walked `raw_russell` only.
   One line, and a gap here produces a holdout that looks scoreable and is not.
2. **Verify the 2026 `no_data` recovery.** Upstream §5 records the spurious bug
   as concentrated in the 2026 pull (74% of that year against 0.6% elsewhere).
   99 files were recovered; completeness is unverified. 2026 **is** the holdout
   year.
3. **Build stage 2 features for 2025-09 → 2026-08.**
   `features_stage2_full/` stops at 2025. This needs upstream `src/` — the one
   exception to the do-not-read rule — and the frozen configuration in upstream
   §15. At 434 bytes/row with `--bar-stride 4`.
4. **Score** under that frozen configuration to produce a handover-equivalent
   panel.
5. **Run the frozen config once.** Record it.

### Beyond the holdout

Paper trading needs things deliberately not built: live scoring, an order path,
and **an intraday data feed that no longer exists** — the subscription lapsed,
and the strategy scores every 20 minutes off five-minute bars, which yfinance
cannot supply reliably. That is the binding constraint on ever trading this,
and it should be sized before the holdout comes back rather than after.

---

## Change log

| Version | Date | Change |
| --- | --- | --- |
| 0.1 | 2026-09-08 | Base spec |
| 0.1a | 2026-09-09 | Phases 0–1. Holdout truncated. Handover verified 64/64. **Spec §6.1 inverted.** **Next-print fill rule discovered.** |
| 0.1b | 2026-09-09 | Phase 2. Engine, costs, benchmarks, runner. Random earns its cost; 0 of 40 seeds beat the strategy. |
| 0.1c | 2026-09-10 | Phase 3. **756-config sweep: median Sharpe 5.93, 99.4% profitable.** Sides = both, unconditionally. **Regime: nothing.** Corrections 9.9–9.11. |
| 0.1d | 2026-09-10 | **Configuration frozen (§0).** All-day re-tested and found genuinely all-day (§8.5) — better book, rejected on turnover. Execution bundle complete: **gap filter, limit orders and stops all NEGATIVE** (§8.6), stops monotone with drawdown worsening. Bar path loader validates against the panel at zero error. Correction 9.12. Phase 5 prerequisites written (§12). |
