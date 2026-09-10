#!/usr/bin/env python3
"""
scripts/10_sparse.py -- fewer trades, without giving up temporal spread.

The gap in what has been tested
-------------------------------
    wide_am   dense in one part of the session   17 trades/day, eff_bars 5.8
    all       spread but dense everywhere        42 trades/day, eff_bars 18.5

Nothing tested is SPARSE and SPREAD. Every third bar across the whole session
would be both -- roughly a dozen trades a day with most of the temporal
diversification that made the all-day config work (confirm Sharpe 11.15 at
6.59% vol, against wide_am's 9.95 at 13.68%).

Part 1 -- where does the all-day book earn?
-------------------------------------------
Amendments §7.3 found the afternoon fails the tick floor per trade: at 15:30,
edge 4.27 bps against a 5.3 bps floor. Yet the all-day config includes those
bars and beats the morning-only one. Two possible explanations:

  a) the afternoon contributes positive return in portfolio context, and §7.3
     (measured at the top-1% cut, no cap, no dedupe) does not transfer
  b) the afternoon loses money but reduces variance enough to pay for itself

LEAVE-ONE-OUT distinguishes them. Drop each bar in turn and rebuild the book.
A bar whose removal RAISES Sharpe is a bar we can drop for free. A bar whose
removal lowers it is earning its place, whichever way it earns.

This is a per-bar contribution measured on the search window, so using it to
choose bars is fitting. It is reported as a diagnostic, and the sparse sets in
part 2 are PRE-REGISTERED rather than derived from it -- otherwise the sweep
would just be the leave-one-out result read back.

Part 2 -- pre-registered sparse sets
------------------------------------
Regular strides across the session, chosen by arithmetic rather than by
performance, plus two anchored variants. Swept at k=2 against a range of caps.

SEARCH WINDOW ONLY.

Usage
-----
    python scripts/10_sparse.py
    python scripts/10_sparse.py --k 2 --cost 20
"""

from __future__ import annotations

import argparse
import json
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
                                     prepare_ranked, select, select_ranked)

SEARCH_WINDOW = "development_2020_2022"
ALL_BARS = ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30", "11:50",
            "12:10", "12:30", "12:50", "13:10", "13:30", "13:50", "14:10",
            "14:30", "14:50", "15:10", "15:30", "15:50"]
MORNING_BARS = {"09:50", "10:10", "10:30", "10:50"}

# PRE-REGISTERED. Regular strides and two anchored variants -- chosen by
# arithmetic, not by the leave-one-out result in part 1.
SPARSE_SETS = {
    "stride2": ALL_BARS[0::2],                    # 10 bars
    "stride3": ALL_BARS[0::3],                    # 7 bars
    "stride4": ALL_BARS[0::4],                    # 5 bars
    "stride5": ALL_BARS[0::5],                    # 4 bars
    "stride3_late": ALL_BARS[1::3],               # 6 bars, offset start
    "stride2_no_last": ALL_BARS[0:16:2],          # 8 bars, drops 15:10-15:50
    "stride3_no_last": ALL_BARS[0:16:3],          # 6 bars, drops the tail
    "wide_am": ALL_BARS[0:6],                     # incumbent, for reference
    "all": ALL_BARS,                              # all-day, for reference
}
CAPS = [15, 20, 25, 30, 40, 50]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def landing(trades: pd.DataFrame) -> dict:
    counts = trades["bar_time"].value_counts()
    p = (counts / counts.sum()).to_numpy()
    return {"morning_share": round(float(
                trades["bar_time"].isin(MORNING_BARS).mean()), 3),
            "effective_bars": round(float(np.exp(-(p * np.log(p)).sum())), 2)}


def main() -> int:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=SEARCH_WINDOW)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--cost", type=float, default=20.0)
    ap.add_argument("--ref-cap", type=int, default=50,
                    help="cap for the all-day reference in part 1")
    args = ap.parse_args()

    if args.file != SEARCH_WINDOW:
        print(f"REFUSED: this is a search. Use {SEARCH_WINDOW}.", file=sys.stderr)
        return 2

    P = load_paths()
    cost = CostConfig(spread_bps=args.cost)
    h = load_handover(args.file, columns=NEEDED)

    # ---------------------------------------------------------------- part 1
    print("part 1: per-bar decomposition and leave-one-out ...", file=sys.stderr)
    ref = SelectionConfig(k=args.k, sides="both", bar_times=None,
                          max_positions=args.ref_cap, balance_sides=True)
    ref_trades = select(h, ref)
    ref_m = run_backtest(h, ref, cost, trades=ref_trades).metrics

    # Per-bar share of positions and of gross contribution.
    t = ref_trades.copy()
    t["contrib"] = (t["ret"] - args.cost / 1e4) * t["weight"]
    per_bar = t.groupby("bar_time", observed=True).agg(
        n=("contrib", "size"),
        mean_bps=("ret", lambda s: float(s.mean()) * 1e4),
        total_contrib_pct=("contrib", lambda s: float(s.sum()) * 100),
    ).round(3)
    per_bar["net_bps"] = (per_bar["mean_bps"] - args.cost).round(2)

    loo_rows = []
    for drop in ALL_BARS:
        keep = [b for b in ALL_BARS if b != drop]
        cfg = SelectionConfig(k=args.k, sides="both", bar_times=keep,
                              max_positions=args.ref_cap, balance_sides=True)
        try:
            m = run_backtest(h, cfg, cost).metrics
        except ValueError as exc:
            loo_rows.append({"dropped": drop, "error": str(exc)[:60]})
            continue
        loo_rows.append({
            "dropped": drop,
            "sharpe": m["sharpe"],
            "delta_sharpe": round(m["sharpe"] - ref_m["sharpe"], 3),
            "tpd": round(m["n_trades"] / m["n_sessions"], 1),
            "vol_pct": m["ann_vol_pct"],
            "per_trade": m["per_trade_bps_net"],
        })
    loo = pd.DataFrame(loo_rows).sort_values("delta_sharpe", ascending=False)

    # ---------------------------------------------------------------- part 2
    print("part 2: sparse sets ...", file=sys.stderr)
    ranked = {name: prepare_ranked(h, bars) for name, bars in SPARSE_SETS.items()}

    rows = []
    for name, cap in product(SPARSE_SETS, CAPS):
        bars = SPARSE_SETS[name]
        cfg = SelectionConfig(k=args.k, sides="both", bar_times=bars,
                              max_positions=cap, balance_sides=True)
        try:
            trades = select_ranked(ranked[name], cfg)
            m = run_backtest(h, cfg, cost, trades=trades).metrics
        except ValueError as exc:
            rows.append({"bars": name, "n_bars": len(bars), "cap": cap,
                         "refused": True, "reason": str(exc)[:60]})
            continue
        rows.append({"bars": name, "n_bars": len(bars), "cap": cap,
                     "refused": False,
                     "sharpe": m["sharpe"],
                     "per_trade_bps": m["per_trade_bps_net"],
                     "tpd": round(m["n_trades"] / m["n_sessions"], 1),
                     "utilisation": m["mean_utilisation"],
                     "vol_pct": m["ann_vol_pct"],
                     "max_dd_pct": m["max_drawdown_pct"],
                     **landing(trades)})
    sw = pd.DataFrame(rows)
    ok = sw[~sw["refused"]].copy()

    # Frontier: best Sharpe at each rounded turnover level.
    frontier = pd.DataFrame()
    if len(ok):
        ok["tpd_bucket"] = (ok["tpd"] / 5).round() * 5
        frontier = (ok.sort_values("sharpe", ascending=False)
                      .groupby("tpd_bucket", as_index=False).first()
                      .sort_values("tpd_bucket"))

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(), "command": " ".join(sys.argv),
        "window": args.file, "k": args.k, "cost_bps": args.cost,
        "reference": {"config": f"all bars, k={args.k}, cap={args.ref_cap}",
                      "sharpe": ref_m["sharpe"],
                      "tpd": round(ref_m["n_trades"] / ref_m["n_sessions"], 1),
                      "vol_pct": ref_m["ann_vol_pct"],
                      "utilisation": ref_m["mean_utilisation"]},
        "elapsed_sec": round(time.time() - t0, 1),
        "reading_notes": [
            "Leave-one-out is measured on the search window. Using it to pick "
            "bars would be fitting; the sparse sets in part 2 are "
            "pre-registered strides, chosen by arithmetic.",
            "delta_sharpe > 0 means dropping that bar HELPED.",
            "Compare within utilisation bands -- a loose cap lowers vol for "
            "reasons unrelated to selection.",
        ],
    }

    outdir = Path(P["outputs"]["artifacts"]) / "10_sparse"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = outdir / f"sparse_k{args.k}_{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    per_bar.to_csv(outdir / "per_bar.csv")
    loo.to_csv(outdir / "leave_one_out.csv", index=False)
    sw.to_csv(outdir / "sparse_sweep.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 230)
    print("\n" + "=" * 104)
    print(f"SPARSE BARS -- {args.file}, k={args.k}, cost {args.cost} bps")
    print("=" * 104)
    print(f"\nreference (all bars, k={args.k}, cap={args.ref_cap}): "
          f"sharpe {ref_m['sharpe']:.3f}, "
          f"{summary['reference']['tpd']} trades/day, "
          f"vol {ref_m['ann_vol_pct']:.2f}%, util {ref_m['mean_utilisation']:.2f}")

    print("\n--- part 1a: per-bar, inside the all-day book ---")
    print("net_bps = gross edge minus the assumed cost. Negative means that "
          "bar loses money per trade.")
    print(per_bar.to_string())

    print("\n--- part 1b: leave-one-out (positive delta = dropping it HELPED) ---")
    print(loo.to_string(index=False))

    print("\n--- part 2: sparse sets, utilisation 0.70-1.00 ---")
    band = ok[(ok.utilisation >= 0.70) & (ok.utilisation <= 1.00)]
    show = ["bars", "n_bars", "cap", "sharpe", "per_trade_bps", "tpd",
            "utilisation", "vol_pct", "max_dd_pct", "effective_bars"]
    if len(band):
        print(band.sort_values("sharpe", ascending=False)[show].to_string(index=False))
    else:
        print("  none in band")

    if len(frontier):
        print("\n--- turnover frontier: best Sharpe at each trade level ---")
        print(frontier[show].to_string(index=False))

    if int(sw["refused"].sum()):
        print(f"\nrefused: {int(sw['refused'].sum())}")
    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
