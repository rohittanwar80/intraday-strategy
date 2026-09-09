#!/usr/bin/env python3
"""
src/execution/costs.py -- what a position costs to put on and take off.

Flat model, swept. Spec §8: "Cost sensitivity is the result. Sweep the spread
assumption from 0 upward. If the strategy is profitable at 0 bps and dead at 3,
that is the finding -- not the headline number at whichever assumption was
chosen."

Why flat, for now
-----------------
A per-trade model -- cost as a function of that name's own price and bar range
-- is more honest, and we know the bars differ by nearly 2x through the session
(amendments §7.2: 92 bps at 09:50 against 51 at 10:50). But it needs bar-range
data from the raw tree, which the handover does not carry.

That is the same dependency as stops, replacement rebalancing, and limit-fill
modelling. Splitting per-trade cost off from its natural companions is the
wrong seam. Flat first, then one bar-tree build for all four.

What the sweep can and cannot say
---------------------------------
No quote data exists in either project, so cost is an assumption at every
level. Amendments §6 brackets it:

    optimistic    4.9 bps    market is one tick wide
    pessimistic  ~23 bps     quote as wide as the whole bar range, ONE crossing

One crossing, not two: the exit is the 15:55 auction, which carries 8.4x
midday volume and clears at a single price with no spread to cross. Entry
crosses; exit does not. `spread_bps` here is therefore the TOTAL charged per
position, not a per-side figure.

Feasibility
-----------
A swept level below a trade's own tick floor is not a conservative scenario,
it is an impossible one. Minimum round trip is one tick:

    tick_floor_bps = (0.01 / price) x 1e4

At the tail's median price of $19-29 that is 3.4-5.4 bps, so any result
reported at 0, 1 or 2 bps is fiction for most of the book. `apply_costs`
reports the fraction of trades whose assumed cost is below their own floor, and
`sweep_feasibility` labels each level. Read the sweep from the first FEASIBLE
level upward.

Borrow (§6.2)
-------------
Spec §6.2 and §13 treat borrow as a possible killer for the short leg. That is
about overnight financing, and this strategy holds nothing overnight -- every
position exits at 15:55. Intraday short financing is largely nil.

What does NOT go away is AVAILABILITY: a locate is still required, hard-to-
borrow names may be untradeable at any price, and locate fees exist. There is
no borrow feed in either project, so availability cannot be modelled at all.
`borrow_bps` defaults to 0 with that reasoning; sweep it to test sensitivity,
but a non-zero value here is a proxy for a constraint we cannot measure, not a
measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

TICK = 0.01   # US equities above $1. Sub-dollar names quote finer.

# Spans the amendments §6 bracket and continues past it. The low end is
# retained deliberately: 0 bps is the reference point that shows how much of
# the result is cost, even though it cannot occur.
DEFAULT_SWEEP = [0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 30.0]


@dataclass
class CostConfig:
    """Cost charged per position, in basis points of notional.

    spread_bps
        TOTAL round trip, not per side. See the module docstring on why the
        auction exit means one crossing.
    borrow_bps
        Additional charge on short positions only. Default 0 -- intraday
        holding avoids overnight financing. A non-zero value is a proxy for
        unmeasurable availability constraints.
    """
    spread_bps: float = 0.0
    borrow_bps: float = 0.0

    def __post_init__(self) -> None:
        if self.spread_bps < 0:
            raise ValueError(f"spread_bps must be >= 0, got {self.spread_bps}")
        if self.borrow_bps < 0:
            raise ValueError(f"borrow_bps must be >= 0, got {self.borrow_bps}")

    def describe(self) -> dict:
        return asdict(self)


def tick_floor_bps(price: pd.Series | np.ndarray) -> pd.Series:
    """Minimum possible round-trip cost, in bps, for a one-tick-wide market."""
    return (TICK / pd.Series(price).astype(float)) * 1e4


def apply_costs(trades: pd.DataFrame, cfg: CostConfig) -> pd.DataFrame:
    """Add cost_bps, net_ret, and a per-trade feasibility flag.

    `net_ret` is the position's signed return after cost. Cost is charged on
    the absolute position, so a short pays the same spread as a long -- being
    short does not make crossing free.
    """
    for col in ("ret", "entry_price", "side"):
        if col not in trades.columns:
            raise ValueError(f"trades is missing {col!r}")

    t = trades.copy()
    t["tick_floor_bps"] = tick_floor_bps(t["entry_price"])

    cost = pd.Series(cfg.spread_bps, index=t.index, dtype=float)
    if cfg.borrow_bps:
        cost = cost + np.where(t["side"] == "short", cfg.borrow_bps, 0.0)

    t["cost_bps"] = cost
    t["net_ret"] = t["ret"] - cost / 1e4
    # Below the trade's own tick floor the assumed cost cannot occur.
    t["cost_infeasible"] = t["cost_bps"] < t["tick_floor_bps"]
    return t


def feasibility(trades: pd.DataFrame, cfg: CostConfig) -> dict:
    """How much of the book could actually trade at this assumed cost."""
    t = apply_costs(trades, cfg)
    frac = float(t["cost_infeasible"].mean())
    return {
        "spread_bps": cfg.spread_bps,
        "borrow_bps": cfg.borrow_bps,
        "frac_below_tick_floor": round(frac, 4),
        "median_tick_floor_bps": round(float(t["tick_floor_bps"].median()), 2),
        "p90_tick_floor_bps": round(float(t["tick_floor_bps"].quantile(0.90)), 2),
        "label": ("IMPOSSIBLE" if frac > 0.5 else
                  "partly impossible" if frac > 0.05 else "feasible"),
    }


def sweep_feasibility(trades: pd.DataFrame,
                      levels: list[float] | None = None,
                      borrow_bps: float = 0.0) -> pd.DataFrame:
    """Feasibility label for each level of a sweep. No returns -- see engine."""
    levels = DEFAULT_SWEEP if levels is None else levels
    return pd.DataFrame([
        feasibility(trades, CostConfig(spread_bps=lv, borrow_bps=borrow_bps))
        for lv in levels
    ])


def breakeven_bps(trades: pd.DataFrame) -> dict:
    """The cost at which gross edge is exactly consumed.

    Per-trade, so it does NOT account for idle slots -- a book at 40%
    utilisation earns this per position but far less per unit of capital. The
    engine's daily accounting is the one to trust for anything decision-making;
    this is a quick orientation number.
    """
    if "ret" not in trades.columns:
        raise ValueError("trades is missing 'ret'")
    gross_bps = float(trades["ret"].mean()) * 1e4
    floor = float(tick_floor_bps(trades["entry_price"]).median())
    return {
        "gross_edge_bps": round(gross_bps, 2),
        "median_tick_floor_bps": round(floor, 2),
        "breakeven_bps": round(gross_bps, 2),
        "breakeven_in_ticks": round(gross_bps / floor, 1) if floor > 0 else None,
        "note": "per-trade, ignores idle slots; use the engine for capital returns",
    }


if __name__ == "__main__":
    from src.data.handover import load_handover
    from src.portfolio.selection import NEEDED, SelectionConfig, select

    h = load_handover("development_2023_2024", columns=NEEDED)
    cfg = SelectionConfig(k=5, sides="both", balance_sides=True,
                          bar_times=["10:50", "11:10"])
    trades = select(h, cfg)

    print(f"trades: {len(trades):,}")
    print("\nbreakeven (per-trade, gross):")
    for k, v in breakeven_bps(trades).items():
        print(f"  {k:26s} {v}")

    print("\nfeasibility across the sweep:")
    print(sweep_feasibility(trades).to_string(index=False))

    print("\nlong and short separately:")
    for side in ("long", "short"):
        sub = trades[trades.side == side]
        b = breakeven_bps(sub)
        print(f"  {side:5s} n={len(sub):>6,}  gross {b['gross_edge_bps']:>7.2f} bps"
              f"  floor {b['median_tick_floor_bps']:>5.2f}"
              f"  breakeven {b['breakeven_in_ticks']} ticks")
