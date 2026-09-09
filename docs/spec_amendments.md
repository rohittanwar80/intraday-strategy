# Spec Amendments and Data Findings — `intraday-strategy`

**Applies to:** Intraday Strategy — Decision Spec v0.1
**Amendment version:** 0.1a
**Date:** 2026-09-09
**Status:** Phase 0 complete (handover verified). Phase 1 complete for what the
available data can answer. Backtest engine not started. Holdout frozen, 0 looks.

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
| **Holdout** | Truncated to 2025-09-02 … 2026-08-25. Structural |

Three things changed materially from the spec, and all three are structural:

1. **§6.1's premise is inverted for the long side.** The tail is not "the
   thinnest names."
2. **The panel uses an undocumented next-print fill rule** on 4.2% of rows.
   The backtest engine must replicate it.
3. **The afternoon fails on arithmetic**, not on a chosen threshold.

---

## 1. Holdout window truncated to 2026-08-25 — STRUCTURAL

### What the spec said

> `2025-09-01 .. 2026-08-31   FROZEN, never read`

### What it now says

**`2025-09-02 .. 2026-08-25`, 249 sessions.**

### Why

Two independent truncations, neither a configuration choice.

**Start.** 2025-09-01 was Labor Day. No session exists. First session 09-02.

**End.** Upstream amendments §12.5 records four panel end dates: `raw/`
2026-08-25, SPY 08-26, `raw_russell/` 08-27, retried files 08-31. Each tree
stops on the day it was fetched.

The binding constraint is `raw/`, **not** `raw_russell/`. The regime columns —
`spy_above_200sma`, `spy_ret_prev`, `vxx_vol_63`, `vxx_vol_pct`,
`spy_intraday` — come from the context ETFs. A bar with Russell prices and no
SPY context is not a scoreable bar.

Inventory scan confirms the Russell cliff is clean rather than graded: coverage
runs 100% of symbols through 2026-08-27, then drops to 8% on 08-28 and 08-31.
Those 8% are the 99 files recovered from the spurious `no_data` bug that
upstream §5 records as concentrated in the 2026 pull (74% of that year against
0.6% elsewhere), retried on 08-31.

**No threshold we picked determines this.** Any coverage floor between 0.09 and
1.00 returns the same date. The parameter does not bind.

### Consequence

Recorded in `config/paths.yaml` with the reason, and enforced by an invariant
in `src/data/paths.py` that fails at load if `common_end_date` and
`holdout.effective_end` are edited apart.

Phase 5 also turns out to be larger than "a mechanical rerun." Upstream §14
shows `artifacts/features_stage2_full/` covers 2020–2025 only, so scoring the
holdout requires a stage 2 **feature build** for 2025-09 → 2026-08 first, then
scoring under the frozen configuration in upstream §15. Reading upstream
`src/` — otherwise out of scope — is required for this and only this.

---

## 2. Russell split risk — inherited as CLOSED, not re-litigated

Upstream §12.1 downgrades it: stage 2's target is `close_1555 / next_bar_open`,
both from the same session and the same intraday file, and every stage 2
feature is within-day. A split applied between sessions cannot reach them.
Upstream §11 records this as a claim its author got wrong and Rohit got right.

Residual, per §12.1: a scan for overnight returns beyond ±50%, "eventually, not
a blocker." Not done. Not needed for Phases 1–4.

See §8.1 below for the assistant's error in raising this as gating.

---

## 3. Volume adjustment — tested, NEGATIVE

### The concern

§12.1's argument covers a *discontinuity*. It does not cover the
*rescaling*, which is present inside every session on the wrong side of a split
date. Almost every sizing column is scale-free and cancels *k*
(`ibar_atr_pct_14`, `ibar_parkinson_12`, `ibar_vol_12`, `or_position`,
`pos_in_day_range`, `ibar_cum_volume_share`). One is not:

    ibar_log_dollar_volume = log(price x volume)

It cancels only if volume was divided by *k* when price was multiplied by it.
Raw dollar volume is already continuous across a split — price halves, volume
doubles — so adjusting price alone would introduce a step of exactly *k* where
none should exist.

Direction mattered: §12.1's failure mode is *omission* (a name looks cheap and
illiquid, gets wrongly excluded). This one is the reverse — dollar volume
inflated by *k* makes a distressed micro-cap look like the most liquid name in
the universe, so it gets wrongly *included*, in the tail, where the edge lives.
Upstream §6 records `f_liquidity` excluding 25.71% of names alone, so that
filter does real work.

### The result: volume is adjusted correctly

**TNXP** settles it. Max adjusted price $1,228,800, min $19,902 — cumulative
*k* on the order of 10⁴, the largest in the file. Its dollar volume sits at the
**33rd percentile** of the universe. Under the bug it would be the most liquid
name on the tape. Working backwards: observed median implied volume is
e^11.09 ≈ $65,600 per bar; inflated by 10⁴ the true figure would be $6.56 per
bar, which is not a number a listed equity produces. DTIL (0.101), SAFE
(0.263) and UP (0.230) agree.

`script 01` is retained as a recorded negative. **Its own verdict string is
wrong** — see §8.2.

---

## 4. Handover verification — PASSED

`scripts/00_verify_handover.py`, 64 checks, 0 warnings, 0 failures.

### The check that matters most

    target == exit_price / entry_price - 1

**max|diff| = 0.000e+00** across all 17,902,858 rows, all three files. Not
within tolerance — exact. The label was computed in the same float64 arithmetic
from the same prices. Upstream §11.3 calls this the single strongest integrity
check available; it passes without qualification.

### Cross-project join

The handover and the raw bar tree had never been checked against each other.
16,858 sampled rows: handover `entry_price` equals the bar tree's open at
*t+1*, **max relative error 0.0**.

### Reconciliation

| | measured | recorded upstream |
| --- | --- | --- |
| dev 2023–24 universe drift | −2.87 bps, SD 139.2 | −2.87, SD 139 (spec §3) |
| validation 2025 drift | +3.84 bps, SD 170.7 | +3.84 (§16) |
| top 1% 2023–24 edge | 46.16 bps | +46.49 (spec §3) |
| top 0.1% 2023–24 edge | 92.17 bps | +92.17 (spec §3) |

We are computing the same quantities on the same data.

### The look boundary, as drawn

Statistics of `target` **alone** are properties of the labels, not the model,
and upstream §17 item 2 says explicitly that drift and dispersion checks spend
nothing. Any statistic **joining `score` to `target`** is an evaluation, and on
validation that is a second look.

So the verifier computes target moments on all three files and IC on
development only. Scripts 02 and 03 do not read `validation_2025.parquet` at
all. Holdout looks remain **0**.

---

## 5. The tail is MORE liquid than the universe — inverts spec §6.1

### What the spec said

> **Fill feasibility — the largest unknown.** … By construction these are the
> day's most extreme movers in small caps. … **10.1% of eligible Russell days
> have no 09:35 print at all** … so a name can be selected and not tradeable.

### What it now says

**For the long tail, this is inverted.** Three independent measurements, all
agreeing:

| measure | universe | top 1% | bottom 1% |
| --- | --- | --- | --- |
| median $ volume/bar (2023–24) | $114,012 | **$241,003** | $145,621 |
| median $ volume/bar (2020–22) | $121,985 | **$282,664** | $105,314 |
| 5-min bar range, bps | 19.02 | 46.23 | 25.55 |
| entry-bar print missing | 4.6% | **1.3%** | 6.9% |

The long tail runs 2.1–2.3× universe dollar volume and is **3.5× less likely**
to be missing a print. Extreme intraday movers have elevated volume for the
same reason they are moving; that is what an extreme move in a cross-section
looks like.

The 10.1% figure describes *eligibility* — days that never entered the panel —
and was being read as though it described the tail.

### The tail is also not cheap stocks

Predicted median tail price near $3–10, with the tick floor eating the edge.
Actual median is **$18–29**, `pct_under_5` is 0.3–1.1%.

| cut / period | edge bps | tick floor bps | breakeven |
| --- | --- | --- | --- |
| top 1%, 2020–22 | 64.33 | 3.4 | **19 ticks** |
| top 1%, 2023–24 | 46.16 | 4.9 | 9.4 ticks |
| bottom 1%, 2023–24 | 26.95 | 3.6 | 7.5 ticks |
| top 1% @ 15:30, 2023–24 | 4.27 | 5.3 | **0.8 ticks** |

Tick floor = `(0.01 / price) × 1e4`, the minimum possible round trip in a
one-tick-wide market. It does not bind.

---

## 6. Cost is bracketed, not measured

No quote data exists in either project. Two computable bounds, top 1% 2023–24
against a 46.16 bps edge:

| bound | assumption | round trip |
| --- | --- | --- |
| optimistic | market is one tick wide | **4.9 bps** |
| pessimistic | quote as wide as the whole bar range | ~23 bps |

The pessimistic figure is one crossing, not two: the exit is the 15:55 auction,
which carries 8.4× midday volume and clears at a single price with no spread to
cross. Entry crosses; exit does not.

**The bracket spans "clearly works" to "half the edge gone." It resolves
nothing**, and that is the honest state of the answer. Per spec §8, the
backtest must sweep the spread assumption from 0 upward and report the cost
curve as the result rather than picking a point on it.

---

## 7. Findings that change the backtest engine

### 7.1 The panel uses a next-print fill rule — STRUCTURAL

**Undocumented upstream.** Not in `MANIFEST.json`, not in amendments v0.3f.

Of 17,589 sampled rows, 731 (4.2%) have no bar at *t+1*. **All 731 are
entry-bar misses; zero are signal-bar misses.** For every one, `entry_price`
equals the open of the next bar that did print, matching to 1e-6 — 731 of 731,
`frac_match` 1.0.

Delay is short: median 5 minutes, p90 15, maximum observed 20. Misses spread
evenly across all bar times (20–56 per bar), no early-close involvement, all
`r2000`, all resolving to the Russell tree.

**The rule is correct.** A name with no trade in the entry bar cannot be bought
there; the next print is the first price at which it could be. Dropping those
rows instead would bias the panel toward the more liquid half of every
cross-section.

**Consequence for the engine.** It must replicate next-print, not assume a bar
always exists. Otherwise the engine and the panel will disagree on 4% of trades
and the disagreement will be invisible.

### 7.2 Cost-adjusted edge peaks at 10:50–11:10, not at the open

Raw edge peaks at 09:50. Edge ÷ bar range does not:

| bar | edge bps | range bps | ratio |
| --- | --- | --- | --- |
| 09:50 | 106.45 | 92.07 | 1.16 |
| 10:30 | 91.78 | 64.05 | 1.43 |
| **10:50** | 105.03 | 50.90 | **2.06** |
| **11:10** | 101.29 | 48.90 | **2.07** |
| 13:10 | 38.66 | 36.76 | 1.05 |
| 14:30 | 3.31 | 32.98 | 0.10 |

The 09:50 bar has the most edge and the worst execution environment at the same
time. Range collapses 45% by 10:50 while edge is still near maximum.

This bears on upstream §12.6 (the 09:35 bar has never been scored): if the
pattern extends backwards, 09:35 is the widest bar of all, and not scoring it
may have cost nothing.

### 7.3 The afternoon fails on arithmetic

Top 1%, 2023–24, 15:30 bar: edge 4.27 bps against a 5.3 bps tick floor.
Negative before any real spread is added. 15:10 and 14:50 are marginal.

This converts spec §5.3 from an open question toward a forced answer. **Not by
a threshold we chose** — the tick floor is arithmetic.

### 7.4 The 15:50 bar is a statistical artefact

Dollar volume $971,739 (the auction), edge 5.52 bps, **t 5.28**. High
significance, no magnitude: a one-bar holding period collapses the variance and
inflates t mechanically. Not tradeable. Noted so it is not mistaken for a
finding later.

### 7.5 The short leg's liquidity disadvantage — bears on §5.2

Spec §5.2 argues the short leg is the better risk-adjusted side on SD grounds
(158 vs 309). It is thinner on **every** liquidity measure we have: lower
dollar volume than the top tail, wider ranges than the universe, and 6.9%
missing prints against the top's 1.3%. Borrow cost sits on top of that.

The volatility advantage is measured on prices. The execution disadvantage is
not in those prices at all. §5.2 remains open, but the SD argument alone should
not decide it.

### 7.6 `score_pct` is not symmetric — cuts finer than 1/n are impossible

`score_pct` appears to be `rank/n`, spanning (0, 1]: minimum 0.0011 (2023–24)
and 0.0012 (2020–22), maximum exactly 1.0. So `top 0.1%` selects via
`score_pct >= 0.999` and works, while `bottom 0.1%` via `score_pct <= 0.001`
selects **nothing**.

Any bottom cut finer than ~1/n must use `rank() <= k` within the section, not a
threshold. Spec §5.1's table lists a 0.1% cut at ~1 name; that is correct in
principle and not reachable through this column on the short side.

### 7.7 Appendix A.5's ~30% does not reproduce

A.5 is the stated reason the entry convention is fixed: entering at the signal
bar's close inflated apparent signal by ~30%. The gap driving that inflation:

| group | mean gap | edge | implied inflation |
| --- | --- | --- | --- |
| top | +0.52 bps | 51.18 | **1.0%** |
| bottom | −0.75 bps | −13.61 | 5.5% |

Standard error ≈ 0.25 bps, so the estimate is small and tight; the interval
excludes 15 bps by a wide margin. The *direction* matches A.5's mechanism —
tail longs show a positive mean gap, consistent with `close_t` being a
last-trade print that reverts — but the magnitude is off by more than an order
of magnitude.

**Unresolved.** Appendix A is not available to this project. Three
possibilities: A.5 measured a different universe or configuration (it predates
the Russell full-universe design), it measured a different quantity, or the
figure is wrong.

**The convention does not change.** Entry at the open of *t+1* is what the
label was built on, so any other entry measures a different strategy
regardless of A.5. But one of the spec's headline justifications does not
reproduce in our data, and that should be known before it is cited again.

Note also: median gap is **exactly 0.00** in every group at every bar. A large
point mass sits at zero — for many observations the next bar's first trade
prints at the previous bar's last price.

---

## 8. Corrections

**The assistant reasoned ahead of the data and was wrong three times. Two were
caught by measurement rather than by better reasoning.**

| Claim | Verdict |
| --- | --- |
| "The Russell split audit is the highest-value diagnostic in the project; put it at the top of Phase 1" | **Wrong, and already closed upstream.** §12.1 downgraded it on a mechanism the assistant independently re-derived two messages later. Cost: one round trip, no code. |
| "12 symbols step by a clean split ratio — SUSPECT" (script 01's verdict string) | **Wrong, the test was broken.** `CLEAN_RATIOS` is dense between 5 and 25 and ±6% tolerance is ±0.12 in log space against gaps of ~0.20, so ~60% of that range counts as "clean" by construction. Observed hit rate 12/26 = 46%, *below* chance. The three largest steps (36×, 23×, 19×) match nothing. The flagged dates are AMC 2021-05-21, GME-era volume events — retail mania, not adjustment. |
| "T2's Spearman +0.392 is the bug's signature" | **Wrong, confounded.** `price_span` is max/min over the window — realised volatility and drawdown, not cumulative *k*. Meme names have large spans and large volume for the same economic reason. High-`max_price` names with `dv_pctile` ≈ 0.96–0.99 (CABO, SAM, RH) are genuinely expensive large caps. |
| "Tail median price near $3–10; the tick floor may eat the edge" | **Wrong.** Actual $18–29. Floor is 3.4–5.4 bps against 46–64 bps. |
| "The morning has both the edge and the liquidity" | **Half wrong.** It has the edge and the dollar volume, and the *widest* ranges. Cost-adjusted edge peaks at 10:50–11:10, not 09:50. |

### Process note

Two data-integrity concerns were escalated and both were killed by measurement.
Both were cheap and worth running, but the pattern is recorded: **the
assistant's priors on this dataset's integrity run pessimistic**, and its next
such flag should be weighted accordingly.

The one concern that was *not* raised speculatively — the missing-bar
accounting — came from a counter in the output rather than from reasoning, and
it produced the only structural finding in §7. Consistent with the kickoff
prompt's note that most upstream corrections came from diagnostics built to
catch them rather than from improved reasoning.

---

## 9. Configuration choices versus structural decisions

Per the kickoff: the first category accumulates optimism; the second does not.

**Structural — forced by a clean diagnostic, no parameter binds:**

- Holdout truncation to 2026-08-25 (§1)
- Next-print fill rule in the engine (§7.1)
- `rank()` rather than a threshold for bottom cuts finer than 1/n (§7.6)
- Afternoon bars failing the tick floor (§7.3) — arithmetic, not a chosen cutoff

**Configuration — chosen on development data, carries optimism:**

- *(none yet — no design parameter has been selected)*

**Assumptions, stated and not measured:**

- Spread. Bracketed 4.9–23 bps (§6), swept rather than chosen
- 1% participation cap in the capacity figures — a convention
- Tick = $0.01 (sub-dollar names quote finer; 0.3–1.1% of the tail)

---

## 10. Open items

1. **Spread.** The binding uncertainty. Not resolvable without quote data.
   Sweep it (spec §8).
2. **`dev_2020_2022` universe drift is −1.48 bps**; upstream §16 records
   development at −2.24 to −2.87. It passed only because the reconciliation
   band was wide (−4 to −1). §10's −2.24 may be a full-universe figure of
   different scope. Small, unexplained, and unexplained gaps are what §11.1 is
   about.
3. **Appendix A.5's 30%** (§7.7) — needs the appendix to resolve.
4. **`raw/` end date 2026-08-25 is inherited, not measured.** The inventory
   scan walked `raw_russell` only. One-line check, Phase 5 prerequisite.
5. **2026 `no_data` recovery completeness** — upstream §5 records the spurious
   bug as concentrated in the 2026 pull (74%), and 2026 is the holdout year.
   99 files were recovered; whether that was complete is unverified.
6. **The 1.33× validation overshoot.** Deprioritised by decision. Two facts are
   now free: `median_names_per_bar` is 686 / 769 / **900**, so the cross-section
   grew 17% and k=5 out of 900 is a more extreme percentile than k=5 out of 769
   — part of the overshoot is arithmetic. And target SD is 167.8 / 139.2 /
   170.7, so validation's dispersion is 1.23× the 2023–24 window but only 1.02×
   the 2020–22 one. The overshoot is largely measured against an unusually
   quiet development period.
7. **Data-integrity section** to be inserted into `docs/spec.md` as §11, with
   Engineering Notes renumbered to §12.
8. **`00_verify_handover.py` conflates ERROR and FAIL.** A check that raises
   (missing import) is reported identically to a check that ran and disagreed,
   and triggers the §11.5 "find what produced the bad file" language, which is
   the wrong advice. Separate the states.

---

## Change log

| Version | Date | Change |
| --- | --- | --- |
| 0.1 | 2026-09-08 | Base spec |
| 0.1a | 2026-09-09 | Phase 0 and Phase 1. Holdout truncated to 2026-08-25 (structural). Handover verified, 64/64, recompute exact at zero. Cross-project join confirmed. Volume adjustment tested, negative. **Spec §6.1 inverted for the long side**: tail is 2.1× universe dollar volume and 3.5× less likely to miss a print. Cost bracketed 4.9–23 bps. **Next-print fill rule discovered** and marked structural for the engine. Cost-adjusted edge peaks 10:50–11:10. Afternoon fails the tick floor. Appendix A.5's 30% does not reproduce. |
