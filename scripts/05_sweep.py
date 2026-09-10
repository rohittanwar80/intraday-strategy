#!/usr/bin/env python3
"""
scripts/05_sweep.py -- spec §5's design questions, swept on the SEARCH window.

    §5.1 selection     how many names, chosen how
    §5.2 sides         long only, short only, or long/short
    §5.3 timing        which bars
    §5.5 rebalancing   accumulate vs dedupe, fcfs vs reserve
    §5.6 costs         swept at every trial, not chosen

Sizing (§5.4) stays equal-weight. Vol-scaling introduces a parameter and needs
the stop work it belongs with; see amendments §12.

The search / confirm split
--------------------------
Development is ~1,125 days and upstream has already spent ~20 looks on it. A
grid of several hundred configurations against a signal whose daily t is ~10
will produce an impressive-looking argmax whether or not anything is there.
Best-of-N selection bias does not announce itself.

So:

    SEARCH   development_2020_2022   690 days   swept freely, this script
    CONFIRM  development_2023_2024   435 days   a SMALL number of finalists,
                                                deliberately, later

This script **never touches the confirm window**. Confirmation is a separate
decision made after looking at the distribution here, not an automatic step. It
is not clean -- 2023-24 carries upstream looks -- but it is far better than
searching the pooled set and quoting the winner.

Reporting the distribution, not the argmax
------------------------------------------
Every trial is written, and the summary reports the full spread of Sharpe
across the grid: median, quartiles, how many trials are profitable, and how
many configurations were tried.

A search where the 75th percentile trial also works is a different object from
one where only the winner does. An unadjusted argmax Sharpe out of 600 trials
is not a number anyone can act on, and the trial count is the minimum context
needed to discount it.

The grid is pre-registered below, in code, before the run. It is a decision,
not something that grew.

Usage
-----
    python scripts/05_sweep.py                 # full grid, search window
    python scripts/05_sweep.py --quick         # reduced grid, for a smoke test
    python scripts/05_sweep.py --file development_2023_2024   # REFUSED
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import run_backtest  # noqa: E402
from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402
from src.execution.costs import CostConfig  # noqa: E402
from src.portfolio.selection import (NEEDED, SelectionConfig,  # noqa: E402
                                     prepare_ranked, select_ranked,
                                     selection_summary)

SEARCH_WINDOW = "development_2020_2022"
CONFIRM_WINDOW = "development_2023_2024"

# --------------------------------------------------------------------------
# PRE-REGISTERED GRID. Decided before the run, not grown during it.
# --------------------------------------------------------------------------
MORNING = ["09:50", "10:10", "10:30", "10:50"]
PEAK = ["10:50", "11:10"]              # cost-adjusted peak, amendments §7.2
MIDDAY = ["11:10", "11:30", "11:50", "12:10"]
WIDE_AM = ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"]

GRID = {
    "bar_times": [PEAK, MORNING, MIDDAY, WIDE_AM, ["10:50"], ["09:50"], None],
    "k": [1, 2, 3, 5, 8, 15],
    "sides": ["long", "short", "both"],
    "max_positions": [10, 20, 40],
    "unique_symbols": [True, False],
}
QUICK = {
    "bar_times": [PEAK, MORNING, None],
    "k": [3, 5, 15],
    "sides": ["long", "both"],
    "max_positions": [20],
    "unique_symbols": [True],
}

# Evaluated per trial. 20 is the first FEASIBLE level for the baseline config
# (amendments §6): below it, part of the book cannot transact at the assumed
# price. Read the 20 bps column as the headline.
COST_LEVELS = [0.0, 5.0, 20.0]
HEADLINE_COST = 20.0


def git_state() -> dict:
    def run(*a: str) -> str:
        try:
            return subprocess.check_output(a, stderr=subprocess.DEVNULL,
                                           text=True).strip()
        except Exception:  # noqa: BLE001
            return ""
    return {"commit": run("git", "rev-parse", "HEAD") or "unknown",
            "dirty": bool(run("git", "status", "--porcelain"))}


def label_bars(bars) -> str:
    if bars is None:
        return "all"
    if bars == PEAK:
        return "peak"
    if bars == MORNING:
        return "morning"
    if bars == MIDDAY:
        return "midday"
    if bars == WIDE_AM:
        return "wide_am"
    return ",".join(bars)


def main() -> int:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=SEARCH_WINDOW)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--i-am-confirming", action="store_true",
                    help="required to sweep anything but the search window")
    ap.add_argument("--tag", default="sweep")
    ap.add_argument("--outdir", type=Path, default=None)
    args = ap.parse_args()

    if args.file != SEARCH_WINDOW and not args.i_am_confirming:
        print(f"REFUSED: this script sweeps the SEARCH window "
              f"({SEARCH_WINDOW}).\n"
              f"  {CONFIRM_WINDOW} is reserved for confirming a SMALL number "
              f"of finalists,\n"
              f"  chosen deliberately after reading the search distribution. "
              f"Sweeping a grid\n"
              f"  across both windows and quoting the winner is the thing the "
              f"split exists to prevent.\n"
              f"  If you really mean it, pass --i-am-confirming.",
              file=sys.stderr)
        return 2

    grid = QUICK if args.quick else GRID
    combos = list(product(grid["bar_times"], grid["k"], grid["sides"],
                          grid["max_positions"], grid["unique_symbols"]))
    print(f"grid: {len(combos)} configurations x {len(COST_LEVELS)} cost levels",
          file=sys.stderr)

    P = load_paths()
    h = load_handover(args.file, columns=NEEDED)

    # Rank once per timing option. This is the whole reason for the refactor:
    # ranking sorts the panel and depends only on the bar filter.
    ranked: dict[str, pd.DataFrame] = {}
    for bars in grid["bar_times"]:
        key = label_bars(bars)
        print(f"  ranking {key} ...", file=sys.stderr)
        ranked[key] = prepare_ranked(h, bars)

    rows: list[dict] = []
    for i, (bars, k, sides, cap, uniq) in enumerate(combos, 1):
        if i % 25 == 0:
            print(f"  ... {i}/{len(combos)}  ({time.time()-t0:.0f}s)",
                  file=sys.stderr)
        cfg = SelectionConfig(k=k, sides=sides, bar_times=bars,
                              max_positions=cap, unique_symbols=uniq,
                              balance_sides=(sides == "both"))
        base = {"bars": label_bars(bars), "k": k, "sides": sides,
                "max_positions": cap, "unique_symbols": uniq}
        try:
            trades = select_ranked(ranked[label_bars(bars)], cfg)
        except ValueError as exc:
            rows.append({**base, "refused": True, "reason": str(exc)[:80]})
            continue

        sel = selection_summary(trades, cfg)
        rec = {**base, "refused": False, "reason": "",
               "n_trades": sel["n_trades"],
               "utilisation": sel["slot_utilisation"],
               "pos_per_day": sel["positions_per_day_mean"]}
        for lv in COST_LEVELS:
            res = run_backtest(h, cfg, CostConfig(spread_bps=lv), trades=trades)
            m = res.metrics
            tag = f"c{int(lv)}"
            rec[f"sharpe_{tag}"] = m["sharpe"]
            rec[f"per_trade_{tag}"] = m["per_trade_bps_net"]
            if lv == HEADLINE_COST:
                rec["vol_pct"] = m["ann_vol_pct"]
                rec["max_dd_pct"] = m["max_drawdown_pct"]
                rec["ann_pct"] = m["ann_return_pct"]
                rec["hit_rate"] = m["hit_rate_days"]
                rec["t_stat"] = m["t_stat_daily"]
        rows.append(rec)

    df = pd.DataFrame(rows)
    ok = df[~df["refused"]].copy()
    hcol = f"sharpe_c{int(HEADLINE_COST)}"

    dist = {}
    if len(ok):
        s = ok[hcol].dropna()
        dist = {
            "n_trials_run": int(len(combos)),
            "n_refused": int(df["refused"].sum()),
            "n_evaluated": int(len(s)),
            "headline_cost_bps": HEADLINE_COST,
            "sharpe_min": round(float(s.min()), 3),
            "sharpe_p25": round(float(s.quantile(0.25)), 3),
            "sharpe_median": round(float(s.median()), 3),
            "sharpe_p75": round(float(s.quantile(0.75)), 3),
            "sharpe_p90": round(float(s.quantile(0.90)), 3),
            "sharpe_max": round(float(s.max()), 3),
            "frac_profitable": round(float((s > 0).mean()), 3),
            "frac_sharpe_above_2": round(float((s > 2).mean()), 3),
            "note": ("The max is a best-of-N statistic. With this many trials "
                     "it is biased upward and should not be quoted alone. The "
                     "median and p75 say whether the result is a property of "
                     "the signal or of the search."),
        }

    summary = {
        "provenance": {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_sec": round(time.time() - t0, 1),
            "command": " ".join(sys.argv),
            "git": git_state(),
            "python": platform.python_version(),
            "handover": h.meta,
        },
        "windows": {"search": args.file, "confirm_reserved": CONFIRM_WINDOW,
                    "confirm_touched": False},
        "grid_preregistered": {k: [label_bars(v) if k == "bar_times" else v
                                   for v in vs] for k, vs in grid.items()},
        "cost_levels": COST_LEVELS,
        "distribution": dist,
        "reading_notes": [
            "Sharpe is the invariant; ann_pct is a function of max_positions.",
            "20 bps is the first feasible cost level for the baseline config. "
            "Lower columns are reference points, not scenarios.",
            "Refused trials are configs where the daily cap would decide "
            "timing silently (amendments §7.8). They are counted, not hidden.",
            "Nothing here has been confirmed out of sample. The confirm "
            "window is untouched.",
        ],
    }

    outdir = (args.outdir or Path(P["outputs"]["artifacts"]) / "05_sweep")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = outdir / f"{args.tag}_{args.file}_{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    df.to_csv(outdir / "all_trials.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 220)
    print("\n" + "=" * 100)
    print(f"DESIGN SWEEP -- search window {args.file}")
    print("=" * 100)
    print(json.dumps(dist, indent=2))

    if len(ok):
        show = ["bars", "k", "sides", "max_positions", "unique_symbols",
                "n_trades", "utilisation", hcol, "sharpe_c5", "per_trade_c20",
                "vol_pct", "max_dd_pct"]
        print(f"\ntop 15 by Sharpe at {HEADLINE_COST:.0f} bps "
              f"(best-of-{len(ok)} -- biased upward):")
        print(ok.nlargest(15, hcol)[show].to_string(index=False))
        print("\nworst 5:")
        print(ok.nsmallest(5, hcol)[show].to_string(index=False))

        print("\nmedian Sharpe by dimension (robust to the argmax):")
        for dim in ("bars", "k", "sides", "max_positions", "unique_symbols"):
            g = ok.groupby(dim)[hcol].agg(["median", "max", "count"]).round(2)
            print(f"\n  {dim}:")
            print(g.to_string())

    if int(df["refused"].sum()):
        print(f"\nrefused: {int(df['refused'].sum())} configs "
              f"(cap would decide timing -- amendments §7.8)")

    print(f"\nwritten to {outdir.resolve()}")
    print(f"\nCONFIRM WINDOW ({CONFIRM_WINDOW}) NOT TOUCHED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
