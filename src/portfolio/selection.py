#!/usr/bin/env python3
"""
src/portfolio/selection.py -- turns a scored panel into a list of trades.

One job: decide WHICH rows become positions. Sizing beyond equal weight,
cost, and P&L accounting live elsewhere.

The model
---------
Capital is `max_positions` equal slots, fixed and known in advance. A slot that
goes unfilled earns zero. This matters: a morning-only book that fills 8 of 20
slots carries its idle capital in the denominator, which is the honest
treatment and the one that will make a morning-only design look worse than its
per-trade numbers suggest. That is the same error §8 warns about for
annualising only traded days, one level down.

No look-ahead: slot size is fixed before the session, not divided by however
many trades the day turns out to produce.

Rebalancing (spec §5.5)
-----------------------
Three options are listed. Only two are implementable here:

    accumulate    every signal becomes a position held to 15:55
    ignore_later  restrict to a set of bar times, e.g. morning only

**Replacement is NOT implementable from the handover.** Closing a position at
11:30 requires an exit price at 11:30, and the panel carries only entry_price
(the open of bar t+1) and exit_price (the 15:55 close). Replacement needs the
bar tree, and is deferred alongside stops -- same dependency.

Selection by rank, not by percentile threshold
----------------------------------------------
Amendments §7.6: score_pct is rank/n spanning (0, 1], so its minimum is ~1/n.
`score_pct >= 0.999` selects the top name; `score_pct <= 0.001` selects
NOTHING. Any short cut finer than 1/n must use a rank. This module ranks within
each cross-section in both directions and selects on the rank, so long and
short cuts are genuinely symmetric.

Ties are broken by row order (method="first" equivalent): arbitrary but
deterministic.

Two things that must be chosen, never defaulted
-----------------------------------------------
1. **The cut** (k or pct). It drives every downstream number.

2. **Timing**, whenever the daily cap binds. Measured on development
   2023-2024: k=5 long-only across all 19 bars yields 95 raw candidates a day,
   42 after unique_symbols, against a 20-slot cap. FCFS then fills those slots
   in time order and the timing decision is made by the cap rather than by the
   design. The resulting book is not what its config says it is:

       k=5, long, all bars       09:50 gets 2,175 of 8,700 positions (25%),
                                 decaying to single days by 14:30
       k=5, both, balanced       09:50 gets 4,350 of 8,700 (50%) -- the cap
                                 fills entirely at the first bar, making it a
                                 09:50-only strategy labelled all-day

   09:50 is the worst bar to concentrate in: top-tail range 92 bps against 51
   at 10:50, cost-adjusted edge ratio 1.16 against 2.06 (amendments §7.2).

   So `select()` refuses when bar_times is None and the cap binds on most days.
   Escape hatches: set bar_times, raise max_positions above the candidate
   count, or tighten k. None of them happen by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal, Sequence

import numpy as np
import pandas as pd

from src.data.handover import Handover, SECTION

Side = Literal["long", "short", "both"]
Allocation = Literal["fcfs", "reserve"]

NEEDED = ["symbol", "date", "bar_time", "index_name", "score", "score_pct",
          "target", "entry_price", "exit_price"]

# Fraction of days on which the cap may bind before bar_times becomes required.
CAP_BIND_TOLERANCE = 0.5


@dataclass
class SelectionConfig:
    """Every knob that decides which rows become positions.

    Defaults are the ones that cannot be tuned: equal weight, accumulate,
    first-come-first-served. The cut has NO default -- see the module
    docstring. Anything chosen on development data belongs in the amendments'
    configuration list, not here as a silent default.
    """
    k: int | None = None              # names per section per side
    pct: float | None = None          # alternative: fraction of the section
    sides: Side = "long"
    bar_times: Sequence[str] | None = None   # None = every scoring bar
    max_positions: int = 20           # the day's slot count. Capital = this.
    allocation: Allocation = "fcfs"
    unique_symbols: bool = True       # one position per name per day
    balance_sides: bool = False       # cap each side at max_positions // 2

    def __post_init__(self) -> None:
        if (self.k is None) == (self.pct is None):
            raise ValueError(
                f"give exactly one of k or pct (got k={self.k}, pct={self.pct}). "
                f"There is no default: the selection cut drives every downstream "
                f"number and should be an explicit choice.")
        if self.k is not None and self.k < 1:
            raise ValueError(f"k must be >= 1, got {self.k}")
        if self.pct is not None and not 0 < self.pct < 1:
            raise ValueError(f"pct must be in (0,1), got {self.pct}")
        if self.max_positions < 1:
            raise ValueError(f"max_positions must be >= 1, got {self.max_positions}")
        if self.sides not in ("long", "short", "both"):
            raise ValueError(f"bad sides {self.sides!r}")
        if self.allocation not in ("fcfs", "reserve"):
            raise ValueError(f"bad allocation {self.allocation!r}")

    @property
    def slot_weight(self) -> float:
        return 1.0 / self.max_positions

    def describe(self) -> dict:
        return {**asdict(self), "slot_weight": self.slot_weight}


def _rank_both_ways(df: pd.DataFrame) -> pd.DataFrame:
    """Add long_rank and short_rank (both 0-based) within each cross-section.

    One sort, both directions: sorting by score descending gives the long rank
    as a cumulative count, and the short rank falls out of the section size.
    """
    cols = [c for c in SECTION if c in df.columns]
    df = df.sort_values(cols + ["score"], ascending=[True] * len(cols) + [False],
                        kind="mergesort").reset_index(drop=True)
    g = df.groupby(cols, observed=True, sort=False)
    df["long_rank"] = g.cumcount()
    df["section_size"] = g["score"].transform("size")
    df["short_rank"] = df["section_size"] - 1 - df["long_rank"]
    return df


def _cut_size(cfg: SelectionConfig, section_size: pd.Series) -> pd.Series:
    if cfg.k is not None:
        return pd.Series(cfg.k, index=section_size.index)
    # ceil so a pct finer than 1/n still selects one name rather than none
    return np.ceil(section_size * cfg.pct).astype(int)


def prepare_ranked(h: Handover,
                   bar_times: Sequence[str] | None = None) -> pd.DataFrame:
    """Filter to the chosen bars and rank within each cross-section.

    Split out from `select` because ranking sorts the whole panel and is the
    expensive step, but depends ONLY on the bar-time filter -- not on k, sides,
    or the cap. A sweep over those can rank once per timing option and reuse
    it, which is the difference between minutes and hours.

    The returned frame carries `_bar_times` in `.attrs` so `select_ranked` can
    refuse a config that does not match it. Note df.attrs holds scalars and
    plain containers only; pandas serialises to JSON on parquet write.
    """
    missing = [c for c in NEEDED if c not in h.df.columns]
    if missing:
        raise ValueError(f"{h.key}: panel is missing {missing}")

    df = h.df
    if bar_times is not None:
        keep = set(bar_times)
        df = df[df["bar_time"].isin(keep)]
        if df.empty:
            raise ValueError(f"no rows at bar_times={sorted(keep)}; "
                             f"available: {sorted(h.df['bar_time'].unique())}")

    out = _rank_both_ways(df[NEEDED].copy())
    out.attrs["_bar_times"] = None if bar_times is None else sorted(bar_times)
    return out


def select_ranked(ranked: pd.DataFrame, cfg: SelectionConfig) -> pd.DataFrame:
    """Apply a config to an already-ranked frame from `prepare_ranked`."""
    want = None if cfg.bar_times is None else sorted(cfg.bar_times)
    have = ranked.attrs.get("_bar_times", "MISSING")
    if have != want:
        raise ValueError(
            f"ranked frame was prepared for bar_times={have} but the config "
            f"asks for {want}. Reusing a ranked frame across different timing "
            f"silently ranks the wrong cross-sections.")
    return _select_from_ranked(ranked, cfg)


def select(h: Handover, cfg: SelectionConfig) -> pd.DataFrame:
    """Panel -> trades. One row per position taken.

    Returns columns: date, bar_time, symbol, side, rank, section_size,
    score_pct, entry_price, exit_price, target, ret, weight.

    `ret` is the SIGNED return of the position: target for a long, -target for
    a short. `weight` is the slot weight, identical for every position.

    Raises if the daily cap would bind while bar_times is unset -- see the
    module docstring.
    """
    return _select_from_ranked(prepare_ranked(h, cfg.bar_times), cfg)


def _select_from_ranked(df: pd.DataFrame, cfg: SelectionConfig) -> pd.DataFrame:
    n = _cut_size(cfg, df["section_size"])

    parts = []
    if cfg.sides in ("long", "both"):
        sel = df[df["long_rank"] < n].copy()
        sel["side"] = "long"
        sel["rank"] = sel["long_rank"]
        parts.append(sel)
    if cfg.sides in ("short", "both"):
        sel = df[df["short_rank"] < n].copy()
        sel["side"] = "short"
        sel["rank"] = sel["short_rank"]
        parts.append(sel)

    t = pd.concat(parts, ignore_index=True)
    t["ret"] = np.where(t["side"] == "long", t["target"], -t["target"])

    # Time order, then quality within a bar. Everything below is priority order.
    t = t.sort_values(["date", "bar_time", "rank"], kind="mergesort")

    if cfg.unique_symbols:
        # Earliest bar wins; within a bar, the better rank wins.
        t = t.drop_duplicates(subset=["date", "symbol"], keep="first")

    # --- the timing guard, BEFORE any capping ----------------------------
    # Must run here, not after balance_sides. balance_sides clamps each side to
    # max_positions // 2, so per_day comes out exactly equal to the cap and
    # never greater -- the guard would measure 0% binding on a book that is
    # entirely 09:50. Measure candidate SUPPLY, not the capped result.
    if cfg.bar_times is None:
        per_day = t.groupby("date", observed=True).size()
        frac_binding = float((per_day > cfg.max_positions).mean())
        if frac_binding > CAP_BIND_TOLERANCE:
            first_bar = t.groupby("date", observed=True)["bar_time"].first()
            raise ValueError(
                f"bar_times is None and the cap binds on {frac_binding:.0%} of "
                f"days: {per_day.median():.0f} candidates against "
                f"{cfg.max_positions} slots. Allocation '{cfg.allocation}' then "
                f"decides timing silently -- with sides={cfg.sides} the book can "
                f"fill entirely at {first_bar.mode().iloc[0]}, the widest bar of "
                f"the session (amendments §7.2). Set bar_times explicitly, raise "
                f"max_positions, or tighten k.")

    if cfg.balance_sides and cfg.sides == "both":
        per_side = cfg.max_positions // 2
        t = t.groupby(["date", "side"], observed=True, group_keys=False).head(per_side)
        t = t.sort_values(["date", "bar_time", "rank"], kind="mergesort")

    if cfg.allocation == "fcfs":
        # Signals fill slots in the order they arrive.
        t = t.groupby("date", observed=True, group_keys=False).head(cfg.max_positions)
    else:  # reserve
        n_bars = t["bar_time"].nunique()
        per_bar = max(cfg.max_positions // max(n_bars, 1), 1)
        t = t.groupby(["date", "bar_time"], observed=True,
                      group_keys=False).head(per_bar)
        t = t.groupby("date", observed=True, group_keys=False).head(cfg.max_positions)

    t["weight"] = cfg.slot_weight

    out = ["date", "bar_time", "symbol", "side", "rank", "section_size",
           "score_pct", "entry_price", "exit_price", "target", "ret", "weight"]
    return t[out].reset_index(drop=True)


def selection_summary(trades: pd.DataFrame, cfg: SelectionConfig) -> dict:
    """Descriptive only -- no returns, no t-stats. Those belong in evaluate/.

    Slot utilisation is here because it is the number that says whether
    max_positions is binding or decorative, and it is easy to never look at.
    """
    per_day = trades.groupby("date", observed=True).size()
    return {
        "config": cfg.describe(),
        "n_trades": int(len(trades)),
        "n_days": int(trades["date"].nunique()),
        "positions_per_day_mean": round(float(per_day.mean()), 2),
        "positions_per_day_median": int(per_day.median()),
        "positions_per_day_max": int(per_day.max()),
        "slot_utilisation": round(float(per_day.mean()) / cfg.max_positions, 3),
        "days_hitting_cap": int((per_day >= cfg.max_positions).sum()),
        "by_side": trades["side"].value_counts().to_dict(),
        "by_bar_time": trades["bar_time"].value_counts().sort_index().to_dict(),
        "median_entry_price": round(float(trades["entry_price"].median()), 2),
        "median_section_size": int(trades["section_size"].median()),
    }


if __name__ == "__main__":
    from src.data.handover import load_handover

    h = load_handover("development_2023_2024", columns=NEEDED)

    configs = [
        # Expect REFUSED: cap binds, timing left to the allocator.
        SelectionConfig(k=5, sides="long"),
        SelectionConfig(k=5, sides="both", balance_sides=True),
        SelectionConfig(pct=0.001, sides="both"),
        # Timing stated explicitly.
        SelectionConfig(k=5, sides="long", bar_times=["10:50", "11:10"]),
        SelectionConfig(k=5, sides="both", balance_sides=True,
                        bar_times=["10:50", "11:10"]),
        # Cap raised above the candidate count, so nothing binds: a genuine
        # all-day book with idle slots showing in the denominator.
        SelectionConfig(pct=0.001, sides="both", max_positions=60),
    ]

    for cfg in configs:
        label = (f"{cfg.sides:5s} k={cfg.k} pct={cfg.pct} cap={cfg.max_positions} "
                 f"bars={'all' if cfg.bar_times is None else len(cfg.bar_times)}")
        try:
            s = selection_summary(select(h, cfg), cfg)
        except ValueError as exc:
            print(f"\n{label}\n  REFUSED: {str(exc).split('.')[0]}.")
            continue
        print(f"\n{label}")
        print(f"  trades {s['n_trades']:>8,}  per day {s['positions_per_day_mean']:>6.2f}"
              f"  utilisation {s['slot_utilisation']:.1%}"
              f"  cap hit {s['days_hitting_cap']}/{s['n_days']} days")
        print(f"  by_side {s['by_side']}")
