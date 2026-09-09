# Spec Amendments and Data Findings — `intraday-strategy`

**Applies to:** Intraday Strategy — Decision Spec v0.1
**Amendment version:** 0.1b
**Date:** 2026-09-09
**Status:** Phases 0, 1 and 2 complete. Backtest engine, costs, benchmarks and
runner built. Phase 3 (design sweeps) not started. Holdout frozen, 0 looks.

> This records what changed from the spec and why, so the reasoning survives the
> chat log. Section 8 lists claims that later evidence contradicted — kept
> rather than deleted. Most are the assistant's.
>
> Upstream amendments v0.3f stand unchanged. Where this document disagrees with
> `docs/spec.md`, this document is later and was measured.

---

## 0. Where the project stands

| | Result |
| --- | --- |
| **Handover integrity** | 64 checks, 0 failures. `target` recomputes from prices at **exactly zero** error across all 17,902,858 rows |
| **Cross-project join** | handover `entry_price` is bit-for-bit the raw bar tree's open at *t+1*, 16,858 sampled rows, max relative error 0.0 |
| **Fill availability** | **Not the constraint.** The long tail is *more* liquid than the universe on all three measures |
| **Spread** | Still unmeasured. Bracketed 4.9 to ~23 bps against a 46 bps edge |
| **Baseline strategy** | Sharpe **11.63** (2023–24) and **11.21** (2020–22), per-trade 62.91 and 63.51 bps at 5 bps cost |
| **Benchmarks** | Random earns its cost and nothing else. 0 of 40 seeds beat the strategy. Bottom tranche loses hard. IWM intraday Sharpe ≈ 0 |
| **Holdout** | Truncated to 2025-09-02 … 2026-08-25. Structural. 0 looks |

Three structural changes from the spec (§1, §5, §7.1), and one result that
survived every attempt to break it (§6).

---

## 1. Holdout window truncated to 2026-08-25 — STRUCTURAL

### What the spec said

> `2025-09-01 .. 2026-08-31   FROZEN, never read`

### What it now says

**`2025-09-02 .. 2026-08-25`, 249 sessions.**

**Start.** 2025-09-01 was Labor Day. No session exists.

**End.** Upstream §12.5 records four panel end dates: `raw/` 2026-08-25, SPY
08-26, `raw_russell/` 08-27, retried files 08-31. The binding constraint is
`raw/`, **not** `raw_russell/`: the regime columns come from the context ETFs,
and a bar with Russell prices and no SPY context is not a scoreable bar.

Inventory scan confirms the Russell cliff is clean rather than graded — 100% of
symbols through 08-27, then 8% on 08-28 and 08-31 (the §5 `no_data` retries).
**No threshold we picked determines this**: any coverage floor between 0.09 and
1.00 returns the same date.

Recorded in `config/paths.yaml` and enforced by an invariant in
`src/data/paths.py` that fails at load if `common_end_date` and
`holdout.effective_end` are edited apart.

Phase 5 is larger than "a mechanical rerun": upstream §14 shows
`features_stage2_full/` covers 2020–2025 only, so the holdout needs a stage 2
**feature build** first, then scoring under the frozen §15 configuration.
Reading upstream `src/` is required for this and only this.

---

## 2. Russell split risk — inherited as CLOSED

Upstream §12.1 downgrades it: stage 2's target and every stage 2 feature are
within-session, so a between-session adjustment cannot reach them. Residual —
a scan for overnight returns beyond ±50% — is "eventually, not a blocker."

See §8.1 for the assistant's error in raising this as gating.

---

## 3. Volume adjustment — tested, NEGATIVE

§12.1's argument covers a *discontinuity*, not the *rescaling* present inside
every session on the wrong side of a split. All sizing columns are scale-free
except `ibar_log_dollar_volume = log(price x volume)`, which cancels *k* only
if volume was divided by it. Direction mattered: this failure inflates rather
than omits, so a distressed micro-cap would look maximally liquid, in the tail,
where the edge lives.

**Volume is adjusted correctly.** TNXP settles it: max adjusted price
$1,228,800, cumulative *k* ~10⁴, dollar volume at the **33rd percentile**.
Under the bug its true volume would be $6.56 per bar, which no listed equity
produces. DTIL (0.101), SAFE (0.263), UP (0.230) agree.

`scripts/01_check_volume_adjustment.py` is retained as a recorded negative.
**Its verdict string is wrong** — see §8.2.

---

## 4. Handover verification — PASSED

64 checks, 0 warnings, 0 failures.

**`target == exit_price / entry_price - 1`: max|diff| = 0.000e+00** across all
17,902,858 rows. Not within tolerance — exact.

**Cross-project join:** 16,858 sampled rows, handover `entry_price` equals the
bar tree's open at *t+1*, max relative error 0.0.

| | measured | recorded upstream |
| --- | --- | --- |
| dev 2023–24 universe drift | −2.87 bps, SD 139.2 | −2.87, SD 139 |
| validation 2025 drift | +3.84 bps, SD 170.7 | +3.84 |
| top 1% 2023–24 edge | 46.16 bps | +46.49 |
| top 0.1% 2023–24 edge | 92.17 bps | +92.17 |

### The look boundary, as drawn

Statistics of `target` **alone** are properties of the labels and spend
nothing (upstream §17 item 2 says so explicitly). Any statistic **joining
`score` to `target`** is an evaluation, and on validation that is a second look.

Enforced in code: `load_handover` raises on any non-development window unless
`allow_spent=True` is passed at the call site, and `scripts/04_backtest.py`
prints a look warning and sets `spends_a_look` in its summary. Holdout looks
remain **0**.

---

## 5. The tail is MORE liquid than the universe — inverts spec §6.1

### What the spec said

> **Fill feasibility — the largest unknown.** … the day's most extreme movers
> in small caps … a name can be selected and not tradeable.

### What it now says

**Inverted for the long tail.** Three independent measurements agreeing:

| measure | universe | top 1% | bottom 1% |
| --- | --- | --- | --- |
| median $ volume/bar (2023–24) | $114,012 | **$241,003** | $145,621 |
| median $ volume/bar (2020–22) | $121,985 | **$282,664** | $105,314 |
| 5-min bar range, bps | 19.02 | 46.23 | 25.55 |
| entry-bar print missing | 4.6% | **1.3%** | 6.9% |

The 10.1% figure describes *eligibility* — days that never entered the panel —
and was being read as though it described the tail.

**The tail is not cheap stocks either.** Median entry price $18–29,
`pct_under_5` 0.3–1.1%, so the tick floor is 3.4–5.4 bps against 46–64 bps of
edge. It does not bind.

---

## 6. Cost is bracketed, not measured

| bound | assumption | round trip |
| --- | --- | --- |
| optimistic | market is one tick wide | **4.9 bps** |
| pessimistic | quote as wide as the whole bar range | ~23 bps |

One crossing, not two: the exit is the 15:55 auction, 8.4× midday volume,
clearing at a single price with no spread to cross.

### Feasibility labelling — a sweep level can be impossible

Minimum round trip is one tick: `(0.01 / price) × 1e4`. A level below a
trade's own floor is not conservative, it is fictional.

For the baseline config the median floor is 4.3 bps but **p90 is 12.94** — the
distribution is skewed by a minority of low-priced names, and the book is not
uniformly tradeable until 20 bps. Only 3 of 10 sweep levels are clean:

| spread bps | fraction below tick floor | label |
| --- | --- | --- |
| 0–3 | 100% → 64% | IMPOSSIBLE |
| 5 | 44% | partly impossible |
| 10 | 18% | partly impossible |
| 15 | 5.7% | partly impossible |
| 20 | 0.3% | **feasible** |
| 30 | 0% | feasible |

**Read every result from the first feasible row.** The 5 bps figures quoted
throughout this document are `partly impossible` and are used because they are
the conventional midpoint, not because they are achievable.

### A discontinuity at 30 bps

Max drawdown across the sweep: −1.71, −1.74, −1.76, −1.79, −1.83, −1.90,
−1.95, −2.07, −2.27, then **−7.37** at 30 bps. Smooth to 20, then triples.

That is a regime change, not degradation: some sustained stretch that was
marginally positive turns negative and a previously-clipped drawdown runs.
It implies a period in 2023–24 where the edge is roughly 30 bps and no more —
plausibly 2023-04 to 2023-06, the weak months (8.6, 16.9, 5.9 bps mean daily).

**This sits uncomfortably close to the pessimistic bound of ~23 bps.** Locating
that stretch is an open item (§10).

### Borrow — weaker than the spec implies, on cost

Spec §6.2 and §13 treat borrow as a possible killer for the short leg. That
concern is about **overnight financing**, and this strategy holds nothing
overnight. Intraday short financing is close to nil.

What does not go away is **availability**: a locate is still required,
hard-to-borrow names may be untradeable at any price, and locate fees exist.
There is no borrow feed in either project, so availability cannot be modelled.
`borrow_bps` defaults to 0; a non-zero value is a proxy for an unmeasurable
constraint, not a measurement.

---

## 7. Findings that shaped the engine

### 7.1 The panel uses a next-print fill rule — STRUCTURAL

**Undocumented upstream.** Of 17,589 sampled rows, 731 (4.2%) have no bar at
*t+1*. **All 731 are entry-bar misses; zero are signal-bar misses.** For every
one, `entry_price` equals the open of the next bar that printed, matching to
1e-6 — 731 of 731. Delay: median 5 minutes, p90 15, max observed 20. Spread
evenly across bar times, no early-close involvement, all `r2000`.

**The rule is correct.** A name with no trade in the entry bar cannot be bought
there. Dropping those rows would bias the panel toward the more liquid half of
every cross-section.

**Consequence:** any engine that models fills must replicate next-print, not
assume a bar exists. The current engine consumes the panel's prices directly,
so it inherits the rule automatically — but a bar-tree-based fill model (stops,
limits, replacement) must implement it explicitly or it will disagree with the
panel on 4% of trades, invisibly.

### 7.2 Cost-adjusted edge peaks at 10:50–11:10, not at the open

| bar | edge bps | range bps | ratio |
| --- | --- | --- | --- |
| 09:50 | 106.45 | 92.07 | 1.16 |
| 10:30 | 91.78 | 64.05 | 1.43 |
| **10:50** | 105.03 | 50.90 | **2.06** |
| **11:10** | 101.29 | 48.90 | **2.07** |
| 14:30 | 3.31 | 32.98 | 0.10 |

Raw edge peaks at 09:50; cost-adjusted edge peaks at 10:50–11:10, where range
has collapsed 45% while edge is near maximum. The 09:50 bar has the most edge
and the worst execution environment simultaneously.

This is why the baseline config uses `bar_times=["10:50", "11:10"]`.

Bears on upstream §12.6 (09:35 never scored): if the pattern extends backwards,
09:35 is the widest bar of all and not scoring it may have cost nothing.

### 7.3 The afternoon fails on arithmetic

Top 1%, 2023–24, 15:30: edge 4.27 bps against a 5.3 bps tick floor. Negative
before any real spread. 15:10 and 14:50 marginal. **Not a threshold we chose.**

### 7.4 The 15:50 bar is a statistical artefact

$971,739 dollar volume (the auction), edge 5.52 bps, **t 5.28**. A one-bar
holding period collapses variance and inflates t mechanically. Not tradeable.

### 7.5 The short leg's liquidity disadvantage — bears on §5.2

Spec §5.2 argues the short leg is better risk-adjusted on SD grounds (158 vs
309). It is thinner on **every** liquidity measure: lower dollar volume, wider
ranges than the universe, 6.9% missing prints against the long's 1.3%. It also
turns over faster — 5,816 short candidates against 4,628 long in an all-day
config, because short-side names repeat across bars less — meaning more
crossings on the thinner side.

Against that, at the baseline config the long leg's gross edge is **92.10 bps
against the short's 46.48**, and both survive the pessimistic bound (18.2 and
12.6 ticks of headroom). §5.2 remains open, but the SD argument alone should
not decide it.

### 7.6 `score_pct` is not symmetric — cuts finer than 1/n are impossible

`score_pct` is rank/n spanning (0, 1]: minimum 0.0011 (2023–24), maximum
exactly 1.0. `score_pct >= 0.999` selects the top name; `score_pct <= 0.001`
selects **nothing**.

`src/portfolio/selection.py` therefore ranks within each section in both
directions and selects on the rank, so long and short cuts are genuinely
symmetric.

### 7.7 Appendix A.5's ~30% does not reproduce

| group | mean gap | edge | implied inflation |
| --- | --- | --- | --- |
| top | +0.52 bps | 51.18 | **1.0%** |
| bottom | −0.75 bps | −13.61 | 5.5% |

SE ≈ 0.25 bps; the interval excludes 15 bps by a wide margin. Direction matches
A.5's mechanism; magnitude is off by more than an order of magnitude.
Unresolved — Appendix A is not available to this project.

**The convention does not change.** Entry at the open of *t+1* is what the
label was built on. But a headline justification does not reproduce and should
not be cited again without this note.

Median gap is **exactly 0.00** in every group at every bar — a large point mass
where the next bar's first trade prints at the previous bar's last price.

### 7.8 The daily cap makes timing decisions silently — GUARDED

Measured on 2023–24: `k=5` long across all 19 bars yields 95 raw candidates a
day, 42 after dedupe, against a 20-slot cap. FCFS then fills slots in time
order, so the cap rather than the design decides timing:

| config | what it looks like | what it is |
| --- | --- | --- |
| `k=5, long, all bars` | all-day | 09:50 takes 2,175 of 8,700 (25%), decaying to single days by 14:30 |
| `k=5, both, balanced` | all-day | 09:50 takes **4,350 of 8,700 (50%)** — the book fills entirely at the first bar |

The second is a 09:50-only strategy labelled all-day, concentrated in the worst
execution bar of the session (§7.2).

`select()` now refuses when `bar_times is None` and the cap binds on most days.
**The guard must run before `balance_sides`**: that clamps each side to
`max_positions // 2`, so `per_day` comes out exactly equal to the cap and never
greater, and the guard measures 0% binding on a book that is entirely 09:50.
Measure candidate *supply*, not the capped result. This was caught only because
the both-sides config passed a guard that had already caught the long-only one.

### 7.9 Accounting: arithmetic returns, compounded drawdown

Returns are **summed, not compounded** — the book is rebuilt from cash each
morning, there is no reinvestment, and compounding would credit growth the
strategy does not have.

But arithmetic summation has **no floor**. The bottom-tranche benchmark first
reported −100.68% total with **−102.94% drawdown**, which is impossible for a
long-only book. Drawdown now runs on the compounded equity curve, bounded at
−100% by construction, while returns stay arithmetic elsewhere. Corrected
figures: −64.49% and −68.59%.

`total_return_pct` still has no floor and is flagged by
`metrics.total_exceeds_capital` when it passes −1.0.

### 7.10 `ann_return_pct` is not a result

It is a function of `max_positions`. The baseline fills 17 of 20 slots; halve
the cap and annualised return doubles, with vol doubling too. That is leverage,
not skill. **Sharpe is the invariant and the only figure comparable across
configurations.** Quoting 134.6% invites the wrong reading, and this caveat is
written into every `summary.json`.

---

## 8. Baseline results and benchmarks

**Config:** `k=5`, both sides, balanced, `bar_times=["10:50","11:10"]`,
`max_positions=20`, `unique_symbols=True`, cost 5 bps (*partly impossible* —
see §6).

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

### What the benchmarks establish

**The ranking carries the result.** Random selection — identical pipeline, same
cut, bars, cap, dedupe and costs, with `score` replaced by uniform noise —
earns exactly the cost it pays and nothing more. Survivorship and small-cap
beta would appear in random selection. They do not.

**The ranking is two-sided.** Buying the worst-ranked names loses 51 and 38 bps
per trade. Spec §7's test is that this should lose symmetrically; it loses more
than symmetrically. Against the ORB floor (Sharpe −1.25), bottom tranche is
−7.69 and −4.64.

**There is no intraday drift being harvested.** IWM open-to-15:55 has Sharpe
−0.15 and +0.14. The strategy is not riding a market that rises during the day.

**The two windows agree to within 1% on per-trade edge**, despite sharing no
dates and spanning completely different regimes — 2020–22 contains COVID, the
2021 meme period and the 2022 bear market; 2023–24 is calm. This was predicted
to be the discriminating test and the result went against the prediction (§9.5).

### What the benchmarks do NOT establish

Random controls for the ranking question, **not for the level question**. Both
arms draw from the same universe, which applies current Russell membership to
all dates (§6.3), so every name survived to today. The comparison is internally
valid; the absolute level may still be flattered. Uncorrectable without
point-in-time constituents.

The result also rests on two development windows, one of which upstream has
already spent ~20 looks on.

---

## 9. Corrections

**The assistant reasoned ahead of the data and was wrong seven times. Nearly
all were caught by measurement rather than by better reasoning.**

| # | Claim | Verdict |
| --- | --- | --- |
| 9.1 | "The Russell split audit is the highest-value diagnostic; put it at the top of Phase 1" | **Wrong, already closed upstream.** §12.1 downgraded it on a mechanism the assistant re-derived independently two messages later. |
| 9.2 | "12 symbols step by a clean split ratio — SUSPECT" (script 01's verdict string) | **Wrong, the test was broken.** `CLEAN_RATIOS` is dense between 5 and 25 and ±6% tolerance is ±0.12 in log space against gaps of ~0.20, so ~60% of that range counts as "clean" by construction. Hit rate 12/26 = 46%, *below* chance. Flagged dates are AMC 2021-05-21 and similar — retail mania, not adjustment. |
| 9.3 | "T2's Spearman +0.392 is the bug's signature" | **Wrong, confounded.** `price_span` is realised volatility, not cumulative *k*. |
| 9.4 | "Tail median price near $3–10; the tick floor may eat the edge" | **Wrong.** Actual $18–29. |
| 9.5 | "The morning has both the edge and the liquidity" | **Half wrong.** It has the edge and the widest ranges. Cost-adjusted peak is 10:50–11:10. |
| 9.6 | "Sharpe 11.6 is not plausible; treat it as a bug hunt. Long-only should show 25–40% vol" | **Wrong — forgot utilisation.** Long-only fills 8 of 20 slots, so gross exposure is 0.40, not 1.0. Observed 11.8% vol implies average pairwise correlation ≈ 0.27, entirely plausible. Both-sides at 73 bps daily SD is near-independence, which is what a market-neutral book should show. The arithmetic reconciles; nothing was broken. |
| 9.7 | "2020–22 should be much weaker if the edge is bias" | **Wrong.** 63.51 vs 62.91 per trade, Sharpe 11.21 vs 11.63. The prediction was the right test and the answer went the other way. |
| 9.8 | "Doubling up on persistent names should show lower per-trade edge (signal decay)" | **Wrong, and backwards.** `unique_symbols=False` gives **65.94** bps against 62.91. A name still top-5 twenty minutes later has *confirmed*, not decayed. See §10. |

### Process note

Five data-integrity or plausibility concerns were escalated and all five were
killed by measurement. **The assistant's priors on this dataset run
pessimistic**, and its next such flag should be weighted accordingly.

The findings that held — the missing-bar accounting (§7.1) and the timing guard
(§7.8) — both came from counters in output rather than from reasoning.
Consistent with the kickoff prompt's note that most upstream corrections came
from diagnostics built to catch them rather than from improved reasoning.

---

## 10. Configuration choices versus structural decisions

Per the kickoff: the first category accumulates optimism; the second does not.

**Structural — forced by a clean diagnostic, no parameter binds:**

- Holdout truncation to 2026-08-25 (§1)
- Next-print fill rule for any bar-tree fill model (§7.1)
- `rank()` rather than a threshold for cuts finer than 1/n (§7.6)
- Afternoon bars failing the tick floor (§7.3) — arithmetic
- Compounded drawdown; arithmetic total flagged past −100% (§7.9)
- The timing guard, and its position before `balance_sides` (§7.8)

**Configuration — chosen on development data, carries optimism:**

| choice | value | evidence | margin |
| --- | --- | --- | --- |
| `unique_symbols` | **True** | Sharpe 11.63 vs 11.04. Per-trade edge is *higher* without dedupe (65.94 vs 62.91), but vol rises 30% (15.05% vs 11.58%) against a 5% edge gain. Concentration costs more risk than the better signal repays. | **Small.** Revisit in Phase 3 across the full bar range — this was tested on two bars, where a name can at most double. Across 19 bars the repeat rate is 56% and a name could take four or five slots; the vol penalty should grow faster than the edge benefit, but that is a prediction, not a measurement. |
| `bar_times` | `["10:50","11:10"]` | §7.2 cost-adjusted peak | Chosen from a per-bar table, so it carries selection optimism |
| `max_positions` | 20 | none — a convention | Affects `ann_return_pct` only, not Sharpe (§7.10) |

**Assumptions, stated and not measured:**

- Spread. Bracketed 4.9–23 bps (§6), swept rather than chosen
- 1% participation cap in capacity figures — a convention
- Tick = $0.01 (0.3–1.1% of the tail quotes finer)
- `borrow_bps = 0` — correct on financing, silent on availability (§6)

---

## 11. Open items

1. **Spread.** The binding uncertainty. Not resolvable without quote data.
2. **Locate the 30 bps drawdown discontinuity** (§6). Cheap now that
   `by_month.csv` and `daily.csv` are written. If a multi-month stretch only
   works below 30 bps, that is close to the pessimistic bound.
3. **`dev_2020_2022` universe drift is −1.48 bps**; upstream §16 records
   development at −2.24 to −2.87. Passed only because the reconciliation band
   was wide. Small, unexplained.
4. **Appendix A.5's 30%** (§7.7) — needs the appendix.
5. **`raw/` end date 2026-08-25 is inherited, not measured.** One-line check,
   Phase 5 prerequisite.
6. **2026 `no_data` recovery completeness** — the bug was concentrated in the
   2026 pull (74%), and 2026 is the holdout year.
7. **The 1.33× validation overshoot.** Deprioritised. Two facts now free:
   `median_names_per_bar` is 686 / 769 / **900**, so k=5 out of 900 is a more
   extreme percentile than k=5 out of 769 — part of the overshoot is
   arithmetic. And target SD is 167.8 / 139.2 / 170.7, so validation's
   dispersion is 1.23× the 2023–24 window but only 1.02× the 2020–22 one. The
   overshoot is largely measured against an unusually quiet development period.
8. **Data-integrity section** to be inserted into `docs/spec.md` as §11, with
   Engineering Notes renumbered to §12.
9. **`00_verify_handover.py` conflates ERROR and FAIL.** A check that raises
   (missing import) reports identically to one that ran and disagreed, and
   triggers the §11.5 "find what produced the bad file" language, which is the
   wrong advice.
10. **Python 3.9 is end-of-life** (October 2025) and has cost two unrelated
    syntax limits already. Not urgent; worth doing at a natural break.

---

## 12. Deferred, with their shared dependency

Four things all need intrabar paths from the raw bar tree, which the handover
does not carry. They should be built together rather than separately:

- **Stops** (spec §5.4). Initial and trailing. The ORB precedent is the
  benchmark: 49.3% stop rate, mean R −0.554 against holds at +0.482.
- **Replacement rebalancing** (spec §5.5). Closing at 11:30 needs a price at
  11:30; the panel has only the *t+1* open and the 15:55 close.
- **Per-trade cost** — `max(tick_floor, λ × bar_range)` rather than a flat bps,
  which matters because bars differ nearly 2× through the session (§7.2).
- **Limit-fill modelling.** See below.

### On limit orders

A resting buy limit fills only when price comes down to it, so fills are
conditioned on the price having moved against you and the misses are
disproportionately the names that ran. For a signal that selects the day's most
extreme movers, that is close to worst case — you collect the reverters and
miss the runners. **And it is invisible in a backtest**, which reports the
limit price as an execution improvement.

It also cannot be computed from the handover at all: `target` is defined
against a market fill at the *t+1* open, so a limit model changes *which trades
exist*, not just their prices.

**The version that works** is a marketable limit used as a slippage cap: cross
the spread, but refuse to pay more than *x* bps through the reference. That is
protective rather than cost-saving, and it converts the sweep from an
unknowable market parameter into one that is chosen.

**Never on the exit.** The 15:55 auction is the best price of the session at
8.4× midday volume, and an unfilled exit means carrying a small-cap position
overnight — unmodelled risk far larger than the spread saved.

**The measurement that would settle it**, before any of the above: for tail
longs, split on whether the entry bar's low reached the open. Those are the
trades a passive limit would have filled; the rest are the ones it would have
missed. Compare realised edge. Script 03 already loads exactly those bars.

---

## Change log

| Version | Date | Change |
| --- | --- | --- |
| 0.1 | 2026-09-08 | Base spec |
| 0.1a | 2026-09-09 | Phases 0–1. Holdout truncated (structural). Handover verified 64/64, recompute exact at zero. Cross-project join confirmed. Volume adjustment negative. **Spec §6.1 inverted for the long side.** Cost bracketed 4.9–23 bps. **Next-print fill rule discovered.** Cost-adjusted edge peaks 10:50–11:10. Appendix A.5's 30% does not reproduce. |
| 0.1b | 2026-09-09 | Phase 2. Engine, costs, benchmarks, runner built. **Baseline Sharpe 11.63 / 11.21 across two windows, per-trade 62.91 / 63.51.** Random earns its cost and nothing else, 0 of 40 seeds beat it, z = 16.5 and 19.3. Bottom tranche −51 / −38 bps. IWM intraday Sharpe ≈ 0. Timing guard added and its ordering bug fixed (§7.8). Drawdown made compounded (§7.9). `ann_return_pct` marked as a leverage artefact (§7.10). First configuration choice recorded: `unique_symbols=True`, small margin. Corrections 9.6–9.8 added. Sweep discontinuity at 30 bps flagged. |
