# Intraday Strategy — Decision Spec v0.1

**Project:** `intraday-strategy`
**Upstream:** `two-stage-intraday` (spec amendments v0.3f)
**Date:** 2026-09-08
**Status:** Not started

---

## 1. What this project is

The upstream project produced a **ranking**. This one turns it into **trades**.

It does not retrain, re-tune, or re-evaluate the model. It consumes a fixed set
of scores and answers a different question: given a signal with this shape,
what is the best way to trade it, and does it survive execution?

**Explicitly out of scope.** Feature engineering, walk-forward training,
alternative targets, model selection. If the strategy work suggests the model
should change, that is a finding to take back upstream, not a change to make
here.

The boundary matters because a backtest bug and a model change are
indistinguishable once they are mixed. Keeping the scores fixed means any
result here is attributable to the strategy alone.

---

## 2. The signal, stated precisely

**Universe.** Russell 2000, full eligible universe. Not a shortlist —
the upstream project tested filtering by a daily model and found it did not
help.

**Scoring.** Every eligible name at every scoring bar receives a rank within
its `(date, bar_time, index)` cross-section. Median cross-section is ~760
names. The scores are at 20-minute resolution (every 4th 5-minute bar).

**The deploy signal is `score_pct`, the within-bar percentile.** Not
`expected_return`.

### Why rank and not predicted return

Calibration ordering held in development (+0.985) but the levels never did:
predicted spans ran 2–6× realised, and in validation the mapping inverted
entirely — bins 1–8 realised positive while predicting negative.

Ranking generalised throughout. Levels did not. `expected_return` is shipped
for completeness and should not drive sizing.

### Entry and exit

```
signal at bar t  ->  ENTRY at the OPEN of bar t+1
exit             ->  CLOSE of the 15:55 bar
```

**This is not adjustable.** Appendix A.5 of the upstream spec: entering at the
signal bar's close inflated apparent signal by ~30%, because that close is a
last-trade price sitting nearer the ask for names being bought. The label was
built on the next bar's open, so any other entry measures a different strategy
than the one that was validated.

The exit carries the closing auction — 8.4× midday volume for Russell — making
it the most executable price of the session.

---

## 3. What the signal looks like

### Monotone in both directions

Development, Russell full universe, 2023–24:

| slice | realised bps | t | SD |
| --- | --- | --- | --- |
| top 0.1% | +92.17 | 9.94 | 421 |
| top 1% | +46.49 | 10.31 | 309 |
| top 5% | +20.31 | 6.49 | 220 |
| universe | −2.87 | | 139 |
| bottom 5% | −14.86 | −6.90 | 123 |
| bottom 1% | −27.50 | −10.30 | 158 |
| bottom 0.5% | −33.21 | −11.00 | 170 |

**The short side works and is the better risk-adjusted leg** — bottom 1% has SD
158 against the top's 309, roughly half the volatility for two-thirds the edge.

Long/short at the 0.5% cut, paired within `(date, bar_time, index)`:
**+93.65 bps, t 20.91, 87% of days positive.**

### The edge decays through the session

| bar | k=5 excess (development) |
| --- | --- |
| 10:10–10:50 | +136 to +166 bps |
| 12:30 | +87 |
| 14:10 | +47 |
| 15:30 | +6 |

A strategy entering uniformly across the day will understate the signal; one
entering only in the morning has a different capacity profile. Both are
legitimate designs and the choice belongs here.

### Validation

Spent once, 2025-01-01 to 2025-08-31, 164 days. IC +0.0394 (t 8.31), k=5 excess
+91.72 bps (t 11.75), **all eight months positive**, both tails working,
monotone across selectivity.

**But it came in 1.33× development, which is the wrong direction.**
Out-of-sample should land below. Roughly half is dispersion (scaled excess was
1.15×); the rest coincides with Russell's intraday drift flipping from −2.87 to
+3.84 bps. The sign and shape confirmed; **treat the magnitude as uncertain.**

Section 12 lists this as the first diagnostic to run.

---

## 4. Data

**Do not copy the data.** Point at the existing repositories.

```
scores      ../two-stage-intraday/handover/
              development_2020_2022.parquet
              development_2023_2024.parquet
              validation_2025.parquet
              MANIFEST.json

bars        ../intraday/intraday-rank/data/
              raw/           503 S&P + 15 context ETFs
              raw_russell/   1,957 symbols
              interim/       sessions, universe, manifests
```

`MANIFEST.json` documents every column, the model configuration, spent-look
counts, and eight cautions. **Read it before writing code.**

### What the handover files carry

| group | columns |
| --- | --- |
| keys | `symbol, date, bar_time, index_name` |
| signal | `score, score_pct, expected_return` |
| outcome | `target` |
| prices | `entry_price, exit_price, bars_remaining` |
| sizing | `ibar_atr_pct_14, ibar_vol_12, ibar_parkinson_12` |
| levels | `or_position, or_breakout_state, dist_day_high, dist_day_low, pos_in_day_range` |
| liquidity | `ibar_log_dollar_volume, ibar_cum_volume_share` |
| breadth | `breadth_above_open, breadth_above_vwap, breadth_above_sma50` |
| regime | `spy_above_200sma, spy_ret_prev, vxx_vol_63, vxx_vol_pct, spy_intraday` |

If something is missing, it is a **join against the upstream feature files**,
not a model rerun. The full 76-column panel lives at
`../two-stage-intraday/artifacts/features_stage2_full/`.

### Raw bars are needed for two things

**Opening-range LEVELS.** `or_position` and `or_breakout_state` are positions
relative to the range, not the range's high and low. Stop placement at the OR
boundary needs the levels, which means recomputing from the first three bars.

**Intrabar paths**, if stops are modelled. The handover has entry and exit
only, so any stop logic needs the bars between them.

---

## 5. What must be decided

These are the project's actual questions.

### 5.1 Selection

How many positions, chosen how? The tail is monotone, so this is a trade-off
rather than a search for a magic cutoff:

| cut | names/bar | edge |
| --- | --- | --- |
| 0.1% | ~1 | +92 bps |
| 0.5% | ~4 | +61 |
| 1% | ~8 | +46 |
| 5% | ~38 | +20 |

Tighter is better per trade and worse for capacity and diversification.

### 5.2 Long only, or long/short

The short leg is the better risk-adjusted side and makes the book
market-neutral by construction. Against that: small-cap borrow is expensive,
sometimes unavailable, and short positions in the most extreme movers carry
squeeze risk.

### 5.3 Timing

The edge is roughly 20× larger at 10:30 than 15:30. Options: trade only the
morning, trade all day with time-varying size, or trade the first bar clearing
a rank threshold.

### 5.4 Sizing and stops

Volatility-scaled sizing is the obvious default — `ibar_atr_pct_14` is
directly usable as a stop distance in price terms.

**A warning from the ORB benchmark.** A stop at the far end of the opening bar,
floored at 0.1 ATR, produced a **49.3% stop rate with mean R −0.554** against
holds at +0.482 — the stop was too tight for a six-hour hold, and the strategy
lost 60% over five years partly because of it. Stops interact with holding
period; a tight stop on a long hold is a tax.

### 5.5 Rebalancing

Scores refresh every 20 minutes. Holding to 15:55 while new signals arrive
means either accumulating positions, replacing them, or ignoring later bars.
Each has different turnover and cost.

### 5.6 Costs

The upstream project assumed **4 bps round trip** and had no quote data. That
assumption is untested and must be swept here.

---

## 6. Constraints that must be modelled

### 6.1 Fill feasibility — the largest unknown

The edge concentrates in **7–8 names per bar** at the 1% cut, with SD 309
against the universe's 139. By construction these are the day's most extreme
movers in small caps.

**No quote data exists in either project.** Spread cost is therefore an
assumption, not a measurement. Proxies available: the bar's own high-low range,
dollar volume, and the observed gap between the signal bar's close and the next
bar's open.

**10.1% of eligible Russell days have no 09:35 print at all**, concentrated in
the thinnest names — so a name can be selected and not tradeable.

### 6.2 Borrow

Required for any short leg. Small-cap borrow is expensive and availability is
not in the data. A cost assumption and a sensitivity sweep are the minimum;
excluding hard-to-borrow names is not possible without a borrow feed.

### 6.3 Survivorship

Current Russell membership applied to all dates. Names that delisted never
appear. This flatters any backtest and cannot be corrected without
point-in-time constituents.

### 6.4 Capital and concentration

At 4 names per side the book is concentrated in extreme movers. Position limits
and gross exposure caps are part of the design, not an afterthought.

---

## 7. Benchmarks to beat

| benchmark | result |
| --- | --- |
| **Buy and hold IWM** | the honest passive alternative |
| **Random selection**, same count and timing | isolates the ranking |
| **Bottom-tranche selection** | if the model ranks, this should lose symmetrically |
| **ORB rules-only** | −60.4% over five years, Sharpe −1.25, negative even at zero spread |

The ORB benchmark exists because a plausible, well-specified rules-only
strategy **loses money**. Any result here should be read against that floor.

**Random and bottom are the discriminating ones.** Model-versus-buy-and-hold
cannot distinguish a strategy that ranks from one that happens to hold a
favourable subset.

---

## 8. Evaluation

### Report per period, never pooled alone

The upstream project's headline numbers repeatedly concealed regime effects
that only appeared per fold or per year. At minimum: by year, by month, and by
market regime.

### Statistics

- **Aggregate to the DAY before any t-stat.** Bars within a day share market
  moves; a bar-level t-stat overstates significance by roughly the square root
  of bars per day.
- Report **total edge alongside per-trade edge**. `total = per_trade ×
  trades_taken`. A filter that raises per-trade return while halving days can
  produce less money — this killed every regime filter tested upstream.
- Sharpe must pad non-traded days with **zero**. Annualising only traded days
  overstates a selective strategy by `sqrt(window / traded)`.

### Cost sensitivity is the result

Sweep the spread assumption from 0 upward. If the strategy is profitable at 0
bps and dead at 3, that is the finding — not the headline number at whichever
assumption was chosen. The ORB benchmark's cost table was more informative than
its Sharpe.

---

## 9. The holdout

```
2025-09-01 .. 2026-08-31   FROZEN, never read
```

A full year, untouched. It is spent **once**, on the finished strategy, at the
end.

Scoring it requires re-running the upstream stage 2 model over that period —
the handover files stop at 2025-08-31. That is a mechanical rerun of a frozen
configuration, not a retrain.

### Already spent

| window | phase | looks |
| --- | --- | --- |
| 2018-01-01 .. 2024-12-31 | development | ~20 |
| 2025-01-01 .. 2025-08-31 | validation, stage 2 | 1 |
| 2025-01-01 .. 2025-08-31 | validation, stage 1 | 1 |

**Neither validation window can be reused.** Strategy development happens on
the development period; the validation period is available as a *check* but
each look is recorded.

`../two-stage-intraday/artifacts/holdout_ledger.json` moves with the project.
The predecessor ran roughly fifteen configurations against its validation
window while debugging, which is why the ledger exists.

---

## 10. Layout

```
intraday-strategy/
  src/
    data/          loaders pointing at the upstream handover and bars
    portfolio/     selection, sizing, rebalancing
    execution/     fills, slippage, cost models
    backtest/      the engine
    evaluate/      metrics, benchmarks, attribution
  scripts/         numbered, one purpose each
  docs/
    spec.md                  this file
    spec_amendments.md       amendments as they accumulate
  artifacts/       outputs; NOT the upstream data
  config/
    paths.yaml     upstream locations, in one place
```

`paths.yaml` matters: the upstream venv lives in a *third* repository
(`intraday-rank`), which has already broken once. Record every external path
in one file rather than scattering absolute paths through the code.

---

## 11. Engineering notes carried forward

Each of these cost at least one round trip upstream.

- **LightGBM handles NaN natively — never `dropna` on features.** A
  `dropna(subset=selected)` silently deleted every bar before 10:30, truncated
  an evaluation, and reversed a comparison that had already been acted on.
- **Write results before printing reports.** A 69-minute build was lost when a
  report raised on a stale column name and the write came after it.
- **Cache keys must record their own contents.** A cache keyed only by filename
  served a 50-symbol file to a 503-symbol run and produced three identical
  outputs that all looked plausible.
- **Categorical merge keys must share a category set**, or the join silently
  returns zero rows.
- **Subsample both sides of a join on the same grid**, or it matches nothing.
- **`df.attrs` holds scalars and plain dicts only** — pandas serialises to JSON
  on parquet write.
- **Never `~series` for a boolean rate unless the dtype is genuinely `bool`.**
  After `fillna(False)` a merged column is object dtype, where `~True == -2`.
- **Aggregate before testing.** Correlated observations inflate every t-stat.

**The meta-lesson.** Three times, a stale or mismatched file produced plausible
numbers that were acted on before being caught. Every output should record what
produced it.

---

## 12. First tasks

1. **Explain the 1.33× validation overshoot.** Compare Russell's 2025 intraday
   dispersion and mean drift against 2020–24, directly from
   `../two-stage-intraday/artifacts/labels_stage2/`. No scores, no looks spent.
   The magnitude assumption depends on the answer.

2. **Measure fill feasibility.** For tail picks: what fraction have a print at
   the entry bar, how wide is the bar's high-low range as a spread proxy, and
   how does that compare to the universe. This gates everything downstream
   (§6.1).

3. **Build the backtest engine** on development data. Selection, sizing,
   costs, benchmarks.

4. **Sweep the design questions** in §5 on development only.

5. **One check against validation**, if warranted, recorded in the ledger.

6. **Holdout, once, at the end.**

---

## 13. What would make this project fail honestly

Worth stating in advance, so the answer is not rationalised later.

- **Spread cost exceeds the edge.** Plausible: the tail is the day's most
  extreme small-cap movers and there is no quote data. If the strategy needs
  sub-2 bps spreads to work, it does not work.
- **The tail is unfillable.** 7–8 names per bar, thinnest names, 10% missing
  prints at the open.
- **Borrow makes the short leg impossible**, leaving a long-only book with two
  thirds of the edge and full market exposure.
- **Capacity is too small to matter**, even if the edge is real.

Any of these is a legitimate result. The upstream project's ORB benchmark is
the precedent: a well-specified strategy that loses money is a finding, and
knowing it early is worth more than a backtest that assumes it away.
