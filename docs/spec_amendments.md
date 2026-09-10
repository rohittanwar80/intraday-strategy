# Spec Amendments and Data Findings — `intraday-strategy`

**Applies to:** Intraday Strategy — Decision Spec v0.1
**Amendment version:** 0.1c
**Date:** 2026-09-10
**Status:** Phases 0–3 complete. §5.1, §5.2, §5.3 and §5.5 answered. §5.4
(sizing, stops) deferred with the bar-tree bundle. Holdout frozen, 0 looks.

> Section 9 lists claims that later evidence contradicted — kept rather than
> deleted. Most are the assistant's.
>
> Upstream amendments v0.3f stand unchanged. Where this document disagrees with
> `docs/spec.md`, this document is later and was measured.

---

## 0. Where the project stands

| | Result |
| --- | --- |
| **Handover integrity** | 64 checks, 0 failures. `target` recomputes at **exactly zero** error across 17,902,858 rows |
| **Cross-project join** | handover `entry_price` is bit-for-bit the bar tree's open at *t+1*, max relative error 0.0 |
| **Fill availability** | **Not the constraint.** The long tail is *more* liquid than the universe |
| **Spread** | Unmeasured. Bracketed 4.9–23 bps. First fully feasible sweep level is 20 bps |
| **Benchmarks** | Random earns its cost and nothing else, 0 of 40 seeds beat the strategy. Bottom tranche −51/−38 bps. IWM intraday Sharpe ≈ 0 |
| **Design sweep** | 756 configs on the search window. Median Sharpe 5.93 at 20 bps, **99.4% of trials profitable** |
| **Regime** | **Nothing.** 12 rules, best gain 0.000 |
| **Selected config** | `wide_am`, k=2–3, both sides, cap 20. Confirm ratio 0.93 |
| **Holdout** | 2025-09-02 … 2026-08-25. **0 looks** |

---

## 1. Holdout window truncated to 2026-08-25 — STRUCTURAL

**`2025-09-02 .. 2026-08-25`**, 249 sessions.

Start moved off Labor Day. End set by the earliest panel end date — upstream
§12.5 records `raw/` at 2026-08-25, SPY 08-26, `raw_russell/` 08-27, retried
files 08-31. The binding constraint is `raw/`, not `raw_russell/`: the regime
columns come from the context ETFs, and a bar with Russell prices and no SPY
context is not scoreable.

Inventory confirms the Russell cliff is clean — 100% of symbols through 08-27,
then 8%. **No threshold we picked determines this**: any coverage floor between
0.09 and 1.00 returns the same date.

Recorded in `config/paths.yaml`, enforced by an invariant in
`src/data/paths.py` that fails at load if the two dates are edited apart.

Phase 5 needs a stage 2 **feature build** for 2025-09 → 2026-08 first
(`features_stage2_full/` stops at 2025), then scoring under the frozen §15
configuration. Reading upstream `src/` is required for this and only this.

---

## 2. Russell split risk — inherited as CLOSED

Upstream §12.1: stage 2's target and features are within-session, so a
between-session adjustment cannot reach them. See §9.1 for the assistant's
error in raising this as gating.

---

## 3. Volume adjustment — tested, NEGATIVE

`ibar_log_dollar_volume` is the only sizing column that is not scale-free, and
the failure would inflate rather than omit — a distressed micro-cap looking
maximally liquid, in the tail.

**Volume is adjusted correctly.** TNXP: cumulative *k* ~10⁴, dollar volume at
the **33rd percentile**. Under the bug its true volume would be $6.56 per bar.
DTIL (0.101), SAFE (0.263), UP (0.230) agree.

`scripts/01_check_volume_adjustment.py` is retained as a recorded negative.
**Its verdict string is wrong** — see §9.2.

---

## 4. Handover verification — PASSED

64 checks, 0 failures. **`target == exit_price / entry_price - 1`:
max|diff| = 0.000e+00** across all 17,902,858 rows.

Cross-project join: 16,858 sampled rows, max relative error 0.0.

Reconciliation: dev 2023–24 drift −2.87 bps (SD 139.2) against upstream's
−2.87 (SD 139); validation +3.84 against +3.84; top 1% edge 46.16 against
46.49; top 0.1% 92.17 against 92.17.

### The look boundary, as drawn and as enforced

Statistics of `target` **alone** spend nothing. Any statistic **joining
`score` to `target`** is an evaluation.

In code: `load_handover` raises on any non-development window without
`allow_spent=True`; `scripts/04_backtest.py` prints a look warning;
`scripts/05_sweep.py` and `07_regime.py` refuse any window but the search one.
Holdout looks remain **0**.

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

One crossing, not two — the exit is the 15:55 auction, 8.4× midday volume, no
spread to cross.

### Feasibility labelling — a sweep level can be impossible

Median tick floor 4.3 bps, **p90 12.94**. The book is not uniformly tradeable
until 20 bps: at 5 bps, 44% of trades sit below their own floor.

**All Phase 3 headline numbers are quoted at 20 bps**, the first fully feasible
level. Phase 2 quoted 5 bps, which is *partly impossible* — that is why the
baseline reads 11.63 in §8 and the sweep reads 8.55 for the same config.

### A discontinuity at 30 bps

Max drawdown across the sweep: −1.71 … −2.27 through 20 bps, then **−7.37** at
30. A regime change, not degradation — some stretch that was marginally
positive turns negative. Uncomfortably close to the pessimistic bound. Open
item (§11).

### Borrow — weaker than the spec implies, on cost

Spec §6.2 and §13 treat borrow as a possible killer. That is about **overnight
financing**, and nothing is held overnight. What remains is **availability** —
a locate is required, hard-to-borrow names may be untradeable at any price, and
there is no borrow feed in either project. `borrow_bps = 0` is correct on
financing and silent on availability.

---

## 7. Findings that shaped the engine

### 7.1 The panel uses a next-print fill rule — STRUCTURAL, undocumented upstream

731 of 17,589 sampled rows (4.2%) have no bar at *t+1*. **All 731 are
entry-bar misses; zero are signal-bar.** For every one, `entry_price` equals
the open of the next bar that printed, to 1e-6 — 731 of 731. Median delay 5
minutes, p90 15, max 20. Even across bar times, no early-close involvement.

The rule is correct: a name with no trade in the entry bar cannot be bought
there, and dropping those rows would bias the panel toward the liquid half of
every cross-section.

**Any bar-tree-based fill model must implement it explicitly** or it will
disagree with the panel on 4% of trades, invisibly.

### 7.2 Cost-adjusted edge peaks at 10:50–11:10 — but see §8.4

| bar | edge bps | range bps | ratio |
| --- | --- | --- | --- |
| 09:50 | 106.45 | 92.07 | 1.16 |
| **10:50** | 105.03 | 50.90 | **2.06** |
| **11:10** | 101.29 | 48.90 | **2.07** |
| 14:30 | 3.31 | 32.98 | 0.10 |

This drove the Phase 2 baseline's `bar_times=["10:50","11:10"]`. **The sweep
later showed that reasoning was too narrow** (§8.4): it optimises per-trade
quality and ignores idle capital.

### 7.3 The afternoon fails on arithmetic

15:30, top 1%, 2023–24: edge 4.27 bps against a 5.3 bps tick floor. Negative
before any real spread. Not a threshold we chose.

### 7.4 The 15:50 bar is a statistical artefact

Edge 5.52 bps, **t 5.28** — a one-bar hold collapses variance and inflates t.

### 7.5 The short leg is a hedge, not an alpha source

Spec §5.2 argues short is the better risk-adjusted side on SD grounds (158 vs
309). Measured, it is worse on every dimension we can see:

- lower gross edge (46.48 vs 92.10 bps at the Phase 2 baseline)
- worse liquidity — lower dollar volume, wider ranges than universe, 6.9%
  missing prints against the long's 1.3%
- faster turnover (5,816 short candidates against 4,628 long), so more
  crossings on the thinner side
- **median Sharpe 1.84 standalone across 665 sweep configs, against long's 6.03
  and both's 7.57**

**But `both` beats `long` in 28 of 30 regime buckets** and lifts Sharpe from
6.22 to 8.55 at the same capital. The short leg removes more variance than the
return it costs.

So §5.2's conclusion (use long/short) survives and its *reasoning* does not.
Include the short leg as a hedge, not because it is the better leg.

### 7.6 `score_pct` is not symmetric — cuts finer than 1/n are impossible

`score_pct` is rank/n spanning (0, 1]. `>= 0.999` selects the top name;
`<= 0.001` selects **nothing**. `selection.py` ranks in both directions and
selects on the rank, so cuts are genuinely symmetric.

### 7.7 Appendix A.5's ~30% does not reproduce

Mean gap +0.52 bps (top) and −0.75 (bottom), implying 1.0% and 5.5% inflation
against A.5's ~30%. SE ≈ 0.25 bps; the interval excludes 15 bps by a wide
margin. Direction matches, magnitude is off by more than an order of magnitude.

**The convention does not change** — entry at the *t+1* open is what the label
was built on. But a headline justification does not reproduce.

Median gap is **exactly 0.00** in every group at every bar.

### 7.8 The daily cap makes timing decisions silently — GUARDED

`k=5` long across 19 bars: 95 raw candidates a day, 42 after dedupe, against a
20-slot cap. FCFS then decides timing:

| config | looks like | is |
| --- | --- | --- |
| `k=5, long, all bars` | all-day | 09:50 takes 25% of positions |
| `k=5, both, balanced` | all-day | 09:50 takes **50%** — the book fills at the first bar |

`select()` refuses when `bar_times is None` and the cap binds on most days.
**The guard must run before `balance_sides`**, which clamps each side to
`max_positions // 2` so `per_day` never exceeds the cap and the guard measures
0% binding on a book that is entirely 09:50. Measure candidate *supply*, not
the capped result.

In the 756-config sweep this refused **91 configs**, concentrated in `all`
bars — which is why the `all` median (6.92) is drawn from 17 self-selected
survivors and should be discounted.

### 7.9 Accounting: arithmetic returns, compounded drawdown

Returns are summed — the book is rebuilt from cash each morning and
compounding would credit growth it does not have. But arithmetic summation has
**no floor**: bottom tranche first reported −100.68% total with **−102.94%
drawdown**, impossible for a long-only book. Drawdown now runs on the
compounded equity curve, bounded at −100%. Corrected: −64.49% and −68.59%.
`total_return_pct` is flagged by `metrics.total_exceeds_capital`.

### 7.10 `ann_return_pct` is not a result

It is a function of `max_positions`. Halve the cap and it doubles, with vol
doubling too. **Sharpe is the invariant.** Written into every `summary.json`.

---

## 8. Results

### 8.1 Baseline and benchmarks (Phase 2, quoted at 5 bps — *partly impossible*)

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
`score` replaced by uniform noise — earns exactly the cost it pays. Survivorship
and small-cap beta would show up there. They do not.

**The ranking is two-sided.** Buying the worst names loses 51 and 38 bps.
Against the ORB floor (Sharpe −1.25), bottom tranche is −7.69 and −4.64.

**No intraday drift is being harvested.** IWM open-to-15:55 Sharpe −0.15/+0.14.

**Two windows agree to within 1% on per-trade edge** despite sharing no dates
and spanning COVID, the meme period and the 2022 bear against a calm 2023–24.

### 8.2 The design sweep — the distribution is the result

756 configurations on the search window (2020–22), 91 refused by the timing
guard, 665 evaluated, all quoted at **20 bps**.

| statistic | Sharpe |
| --- | --- |
| min | −0.054 |
| p25 | 2.501 |
| **median** | **5.930** |
| p75 | 7.236 |
| p90 | 8.429 |
| max | 11.582 |
| **fraction profitable** | **99.4%** |

**This is the shape of a real signal, not an overfit search.** When a grid fits
noise the median sits near zero and only the argmax looks good. Here the median
configuration would be a strong strategy and the worst of 665 is −0.054. The
argmax is therefore the *least* interesting number in the table.

Per-dimension medians, which are robust to the argmax:

| dimension | medians |
| --- | --- |
| **sides** | both **7.57**, long 6.03, short **1.84** |
| **k** | 1 → 5.34, 2 → **6.63**, 3 → 6.41, 5 → 6.23, 8 → 5.71, 15 → 4.67 |
| **bars** | wide_am **6.75**, morning 6.65, peak 6.22, midday 6.09, 10:50 5.57, 09:50 5.22, all 6.92 *(17 trials, discount)* |
| **max_positions** | 10 → 5.93, 20 → 5.94, 40 → 5.85 |
| **unique_symbols** | False 5.94, True 5.86 |

`k=1` holds the grid's single best trial (11.58) and a **below-median** median
(5.34) — one name per section is high variance, so it wins the max and loses
the average. A textbook best-of-N artefact.

`max_positions` and `unique_symbols` are **flat**: 0.09 and 0.08 apart across
665 configs. The Phase 2 `unique_symbols=True` decision, taken on an
11.63-vs-11.04 margin from a two-bar test, turns out not to matter.

### 8.3 Regime — NOTHING (12 rules, best gain 0.000)

The question tested was a **switch**, not a filter: trade every day, change
only which side. Days traded is constant, so the mechanism that killed
upstream's eight filters (per-day up, days down, total flat) cannot apply.

Result: **`both` wins 28 of 30 buckets.** Ten of twelve rules select `both`
everywhere and are therefore identical to always-both — gain exactly 0.000. The
two that switch are **worse on Sharpe**: −0.306 and −0.475.

| variable | split | low | mid | high |
| --- | --- | --- | --- | --- |
| spy_above_200sma | tercile | **7.48** / 5.62 / 0.44 | 7.51 / *6.97* / −0.59 | **10.98** / 6.05 / 5.23 |
| spy_ret_prev | tercile | **7.70** / 5.80 / 0.20 | **9.06** / 7.13 / 2.00 | **9.01** / 5.76 / 2.82 |
| vxx_vol_pct | tercile | **9.10** / 5.87 / 2.77 | **9.34** / 7.64 / 1.93 | **7.38** / 5.27 / 0.28 |
| spy_intraday | tercile | 6.99 / *6.15* / −0.41 | **9.43** / 6.54 / 3.85 | **9.40** / 5.97 / 1.98 |
| breadth_above_open | tercile | **6.91** / 5.96 / 0.31 | **8.57** / 5.48 / 2.85 | **10.60** / 7.24 / 1.72 |
| breadth_above_sma50 | tercile | **6.89** / 5.05 / 0.24 | **9.20** / 6.30 / 2.56 | **9.66** / 7.42 / 2.95 |

*(both / long / short Sharpe. Bold = both wins. Italic = long's total won.)*

**For a switch, TOTAL is the wrong selector.** Both switching rules were chosen
because long's *total* beat both's in one bucket (+3.74% and +4.78%), and both
reduced Sharpe. Days traded is constant in a switch, so total's protective
value against the filter trap is absent while its blindness to volatility is
fully active. Upstream's discipline is right for filters and wrong here.

**A side pattern, consistent and counter-intuitive.** Short is strongest in
risk-on buckets (SPY above 200SMA 5.23, low VXX 2.77, high breadth 1.72–2.95)
and weakest in stressed ones (high VXX 0.28, low breadth 0.31, low prior return
0.20). Backwards from intuition — plausibly because in calm markets extreme
down-movers are idiosyncratic and revert cleanly, while in stress everything
falls together and the cross-sectional short loses its distinctiveness.
Consistent across three unrelated variables, so unlikely to be noise. Does not
change the decision.

**Known bug:** tercile-splitting a binary variable (`spy_above_200sma`)
produces a meaningless "mid" bucket cut by rank within ties. One of the two
switching rules came from it. No effect on the conclusion; should be guarded.

### 8.4 Finalists and the consistency check

Selected by two pre-registered rules — plateau ranking, and a utilisation band
of [0.70, 1.00] so the ranking is not a leverage ranking.

| config | search Sharpe | spike_gap | confirm Sharpe | **ratio** | per-trade search → confirm |
| --- | --- | --- | --- | --- | --- |
| **wide_am / k=2 / both / 20** | 10.70 | 0.270 | **9.95** | **0.930** | 70.15 → 62.28 |
| wide_am / k=3 / both / 20 | 10.43 | 0.364 | 9.75 | 0.934 | 64.08 → 56.62 |
| morning / k=5 / both / 20 | 9.70 | **0.000** | 7.81 | 0.805 | 58.89 → 45.95 |
| morning / k=5 / both / 40 | 10.28 | 0.579 | 9.43 | 0.917 | 47.36 → 41.85 |

All four hold. Ratios 0.81–0.93 — mild decay in the **right** direction, and
all four clear Sharpe 7.8 on a window they were not chosen on. Per-trade edge
decays ~10% consistently while Sharpe decays less, so the confirm window is
slightly less profitable per trade and slightly less volatile.

**`peak` did not survive**, despite being the Phase 2 baseline and where §7.2's
analysis pointed. `peak/k=5/cap=20` runs utilisation 0.834 at Sharpe 8.55: two
bars do not fill enough slots. Wider windows deploy more capital at similar
per-trade quality. §7.2's reasoning optimised per-trade quality and ignored
idle capital — see §9.10.

**The confirm window is contaminated.** 2023–24 was used throughout Phase 2
before the split was declared. A ratio near 1.0 means *consistency*, not
out-of-sample validation. Recorded in `docs/confirm_ledger.json`, which counts
looks from this point even though the count starts late.

**Nothing here has been seen by an untouched period.** The only clean test is
the holdout.

---

## 9. Corrections

**The assistant reasoned ahead of the data and was wrong ten times.** Nearly
all were caught by measurement rather than by better reasoning.

| # | Claim | Verdict |
| --- | --- | --- |
| 9.1 | "The Russell split audit is the highest-value diagnostic; top of Phase 1" | **Wrong, already closed upstream** on a mechanism the assistant re-derived two messages later. |
| 9.2 | "12 symbols step by a clean split ratio — SUSPECT" | **Wrong, the test was broken.** ±6% tolerance against a dense ratio list made ~60% of the range "clean" by construction; hit rate 46%, *below* chance. |
| 9.3 | "T2's Spearman +0.392 is the bug's signature" | **Wrong, confounded.** `price_span` is realised volatility, not cumulative *k*. |
| 9.4 | "Tail median price near $3–10; the tick floor may eat the edge" | **Wrong.** Actual $18–29. |
| 9.5 | "The morning has both the edge and the liquidity" | **Half wrong.** It has the edge and the widest ranges. |
| 9.6 | "Sharpe 11.6 is implausible; long-only should show 25–40% vol" | **Wrong — forgot utilisation.** Long-only fills 8 of 20 slots, gross exposure 0.40. Observed vol implies pairwise correlation ≈ 0.27, entirely plausible. |
| 9.7 | "2020–22 should be much weaker if the edge is bias" | **Wrong.** 63.51 vs 62.91 per trade. The right test, the wrong prediction. |
| 9.8 | "Doubling up should show signal decay" | **Wrong, and backwards.** `unique_symbols=False` gives higher per-trade edge — a name still top-5 twenty minutes later has *confirmed*, not decayed. |
| 9.9 | "`k=1` will look good and the plateau rule will demote it" | **Right** — the one prediction that held. |
| 9.10 | "Pick bars by the best edge-to-range ratio" (§7.2) | **Too narrow.** It optimises per-trade quality and ignores idle capital. `peak` has the best ratio and loses to `wide_am` and `morning`, which deploy more capital at similar quality. |
| 9.11 | "Flat plateaus will decay less out of sample than spikes" | **Wrong, and inverted.** The flattest config (gap 0.000) decayed **most** (ratio 0.805); the two mild peaks decayed least (0.930, 0.934). Four configs on a contaminated window is a small sample, so this does not refute plateau selection in general — but on this evidence `spike_gap` should not be the primary selector. |

### Process note

Six data-integrity or plausibility concerns were escalated and all six were
killed by measurement. **The assistant's priors on this dataset run
pessimistic.**

The findings that held — the missing-bar accounting (§7.1), the timing guard
(§7.8), the utilisation confound (§8.4) — came from counters and columns in
output, not from reasoning. Consistent with the kickoff prompt's note that most
upstream corrections came from diagnostics built to catch them.

---

## 10. Configuration choices versus structural decisions

**Structural — forced by a clean diagnostic, no parameter binds:**

- Holdout truncation to 2026-08-25 (§1)
- Next-print fill rule for any bar-tree fill model (§7.1)
- `rank()` rather than a threshold for cuts finer than 1/n (§7.6)
- Afternoon bars failing the tick floor (§7.3) — arithmetic
- Compounded drawdown; arithmetic total flagged past −100% (§7.9)
- The timing guard, and its position before `balance_sides` (§7.8)
- **Sides = both** (§8.3). 28 of 30 regime buckets, every variable, every split
- **No regime conditioning** (§8.3). 12 rules, best gain 0.000

**Configuration — chosen on development data, carries optimism:**

| choice | value | evidence | margin |
| --- | --- | --- | --- |
| `bar_times` | **`wide_am`** (09:50–11:30) | median 6.75 over 108 trials; both finalists | vs `morning` 6.65 — **inside noise**, either is defensible |
| `k` | **2 or 3** | medians 6.63 / 6.41 against 5.34 (k=1) and 4.67 (k=15) | k=2 and k=3 are **indistinguishable** (10.70 vs 10.43 search, 9.95 vs 9.75 confirm). k=3 has a fuller neighbourhood |
| `max_positions` | 20 | flat: 5.93 / 5.94 / 5.85 | **Does not matter.** Set by utilisation target, not performance |
| `unique_symbols` | True | flat: 5.86 vs 5.94 across 665 configs | **Does not matter.** The Phase 2 decision was taken on a margin that does not survive the wider test |

**Assumptions, stated and not measured:**

- Spread. Bracketed 4.9–23 bps, swept rather than chosen
- 1% participation cap in capacity figures — a convention
- Tick = $0.01 (0.3–1.1% of the tail quotes finer)
- `borrow_bps = 0` — correct on financing, silent on availability

---

## 11. Open items

1. **Spread.** The binding uncertainty. Not resolvable without quote data.
2. **Locate the 30 bps drawdown discontinuity** (§6), now cheap from the
   written `daily.csv`.
3. **Survivorship.** Random controls for the ranking, not the level. Both arms
   draw from the same survivor-only universe (spec §6.3). Uncorrectable without
   point-in-time constituents, and it sits under every number in §8.
4. **`dev_2020_2022` universe drift is −1.48 bps** against upstream's −2.24 to
   −2.87. Passed only because the reconciliation band was wide.
5. **Appendix A.5's 30%** (§7.7) — needs the appendix.
6. **`raw/` end date 2026-08-25 is inherited, not measured.** Phase 5 prereq.
7. **2026 `no_data` recovery completeness** — the bug hit 74% of the 2026 pull,
   and 2026 is the holdout year.
8. **The 1.33× validation overshoot.** Deprioritised. Two facts free:
   `median_names_per_bar` is 686 / 769 / **900**, so part of the overshoot is
   arithmetic; and target SD is 167.8 / 139.2 / 170.7, so validation's
   dispersion is 1.23× the 2023–24 window but only 1.02× the 2020–22 one.
9. **Data-integrity section** to be inserted into `docs/spec.md` as §11.
10. **`00_verify_handover.py` conflates ERROR and FAIL.**
11. **Tercile-splitting a binary variable** produces a meaningless bucket
    (§8.3). Guard it.
12. **Python 3.9 is end-of-life** and has cost three unrelated syntax limits.

---

## 12. Deferred — the bar-tree bundle

Four things need intrabar paths the handover does not carry. Build together:

- **Stops** (spec §5.4), initial and trailing. ORB precedent: 49.3% stop rate,
  mean R −0.554 against holds at +0.482.
- **Replacement rebalancing** (§5.5). Closing at 11:30 needs a price at 11:30.
- **Per-trade cost** — `max(tick_floor, λ × bar_range)` rather than flat, since
  bars differ nearly 2× through the session (§7.2).
- **Vol-scaled sizing** (§5.4), which interacts with stops.
- **Limit-fill modelling.**

### On limit orders

A resting buy limit fills only when price comes to it, so fills are conditioned
on the price having moved against you and the misses are disproportionately the
names that ran. For a signal selecting the day's most extreme movers that is
close to worst case — you collect the reverters and miss the runners. **And it
is invisible in a backtest**, which reports the limit price as an execution
improvement.

It cannot be computed from the handover at all: `target` is defined against a
market fill at the *t+1* open, so a limit model changes *which trades exist*.

**The version that works** is a marketable limit as a slippage cap — cross the
spread, but refuse to pay more than *x* bps through the reference. Protective
rather than cost-saving, and it converts the sweep from an unknowable market
parameter into a chosen one.

**Never on the exit.** The 15:55 auction is the best price of the session; an
unfilled exit means carrying a small-cap position overnight.

**The measurement that would settle it:** for tail longs, split on whether the
entry bar's low reached the open. Those are the trades a passive limit would
have filled; the rest are the ones it would have missed. Compare realised edge.
Script 03 already loads exactly those bars.

---

## 13. On the eventual parameter search

Recorded before it happens.

Development is ~1,125 days and upstream has already spent ~20 looks. A joint
Optuna search over entry, sizing, stops and selection is easily thousands of
evaluations. The best trial out of a thousand will look good whether or not
anything is there.

Three things make it honest rather than decorative:

1. **Split development.** Search on 2020–22, confirm on 2023–24, one confirm
   look per candidate family, recorded. Already implemented as refusals in
   `05_sweep.py` and `07_regime.py`.
2. **Pre-register the grid and the objective** before the run, so the search
   space is a decision rather than something that grew.
3. **Report the trial distribution, not the argmax.** §8.2 is the model: a
   median of 5.93 with 99.4% profitable says something the max of 11.58 does
   not.

Note that the Phase 3 grid was pre-registered and the distribution was
reported, and the exercise still produced §9.11 — a selection rule that
inverted out of sample. Structure reduces the damage; it does not eliminate it.

---

## Change log

| Version | Date | Change |
| --- | --- | --- |
| 0.1 | 2026-09-08 | Base spec |
| 0.1a | 2026-09-09 | Phases 0–1. Holdout truncated. Handover verified 64/64, recompute exact at zero. Volume adjustment negative. **Spec §6.1 inverted for the long side.** Cost bracketed. **Next-print fill rule discovered.** A.5's 30% does not reproduce. |
| 0.1b | 2026-09-09 | Phase 2. Engine, costs, benchmarks, runner. **Baseline Sharpe 11.63 / 11.21, per-trade 62.91 / 63.51 at 5 bps.** Random earns its cost, 0 of 40 seeds beat it. Timing guard and its ordering bug. Compounded drawdown. Corrections 9.6–9.8. |
| 0.1c | 2026-09-10 | Phase 3. **756-config sweep: median Sharpe 5.93, 99.4% profitable — the distribution, not the argmax, is the result.** §5.2 answered: both, unconditionally. **Regime: nothing, 12 rules, best gain 0.000** — and total is the wrong selector for a switch. Finalists `wide_am/k=2–3/both/20`, confirm ratio 0.93. `max_positions` and `unique_symbols` shown not to matter. Corrections 9.9–9.11, including the plateau rule inverting (9.11). All headline numbers moved to 20 bps. |
