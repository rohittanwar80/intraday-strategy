#!/usr/bin/env python3
"""
scripts/08_allday.py -- is all-day trading at a tight cut actually better?

Why this run exists
-------------------
The 756-config sweep under-sampled exactly this region:

  * it used `k` only, never `pct` -- percentile cuts were never tested
  * of its `all`-bars configs, **91 were refused** by the timing guard,
    leaving 17 self-selected survivors whose median (6.92) came from a sample
    biased toward loose caps
  * `allocation` was fixed at `fcfs`, which on all bars hands most slots to the
    morning -- so "all day" was never really tested, only "morning by accident"

And the single best configuration in the whole grid was in that region:
`all / k=1 / both / cap=40`, Sharpe **11.58**. It was dismissed as a spike,
a low-utilisation artefact, and a refusal-selected survivor. Amendments §9.11
then showed the spike reasoning inverting out of sample, so that dismissal
deserves less confidence than it was given.

What is swept
-------------
    cut          k in 1..5, AND pct in 0.001..0.005 -- both, to check they
                 converge. At a ~700-name section, ceil(700 x 0.001) = 1, so
                 pct=0.001 should reproduce k=1. If they disagree, one of the
                 two paths through _cut_size is wrong.
    max_positions 20..80, wide enough that the guard is not doing the choosing
    allocation   fcfs AND reserve -- the dimension the main sweep fixed
    unique_symbols both

sides is fixed at `both`: settled in amendments §8.3, 28 of 30 regime buckets.

The landing diagnostic
----------------------
"All bars" does not mean trades land evenly across bars. For each config this
reports where positions actually open:

    morning_share    fraction opened at 09:50-10:50
    effective_bars   exp(entropy of the bar distribution) -- the number of bars
                     the book behaves as if it uses. 19 means even; 4 means it
                     is a morning strategy wearing an all-day label.

If the good all-day configs have effective_bars near 4, the timing answer is
arriving on its own and "all bars" is not a real alternative to `wide_am`.

A note on comparing across utilisation
--------------------------------------
Loose caps leave capital idle, which lowers vol and raises Sharpe for a reason
unrelated to selection (amendments §8.4). Configs are therefore reported WITH
utilisation, and the summary ranks within bands rather than pooling. A config
at 0.4 utilisation is not comparable to one at 0.9.

SEARCH WINDOW ONLY.

Usage
-----
    python scripts/08_allday.py
    python scripts/08_allday.py --cost 20 --quick
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
                                     prepare_ranked, select_ranked)

SEARCH_WINDOW = "development_2020_2022"
MORNING_BARS = {"09:50", "10:10", "10:30", "10:50"}
WIDE_AM = ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"]

GRID = {
    "cut": [("k", 1), ("k", 2), ("k", 3), ("k", 4), ("k", 5),
            ("pct", 0.001), ("pct", 0.002), ("pct", 0.003), ("pct", 0.005)],
    "max_positions": [20, 30, 40, 50, 60, 80],
    "allocation": ["fcfs", "reserve"],
    "unique_symbols": [True, False],
}
QUICK = {
    "cut": [("k", 1), ("k", 2), ("pct", 0.001)],
    "max_positions": [40, 60],
    "allocation": ["fcfs", "reserve"],
    "unique_symbols": [True],
}


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def landing(trades: pd.DataFrame) -> dict:
    """Where do positions actually open?"""
    counts = trades["bar_time"].value_counts()
    p = (counts / counts.sum()).to_numpy()
    entropy = float(-(p * np.log(p)).sum())
    return {
        "morning_share": round(float(
            trades["bar_time"].isin(MORNING_BARS).mean()), 3),
        "effective_bars": round(float(np.exp(entropy)), 2),
        "n_bars_used": int(counts.size),
        "first_bar_share": round(float(counts.max() / counts.sum()), 3),
    }


def main() -> int:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=SEARCH_WINDOW)
    ap.add_argument("--cost", type=float, default=20.0)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--tag", default="allday")
    args = ap.parse_args()

    if args.file != SEARCH_WINDOW:
        print(f"REFUSED: this is a search. Use {SEARCH_WINDOW}.", file=sys.stderr)
        return 2

    grid = QUICK if args.quick else GRID
    combos = list(product(grid["cut"], grid["max_positions"],
                          grid["allocation"], grid["unique_symbols"]))
    print(f"{len(combos)} configurations, all 19 bars", file=sys.stderr)

    P = load_paths()
    cost = CostConfig(spread_bps=args.cost)
    h = load_handover(args.file, columns=NEEDED)

    print("ranking full panel (once) ...", file=sys.stderr)
    ranked_all = prepare_ranked(h, None)
    ranked_wide = prepare_ranked(h, WIDE_AM)

    rows = []
    for i, ((cut_kind, cut_val), cap, alloc, uniq) in enumerate(combos, 1):
        if i % 20 == 0:
            print(f"  ... {i}/{len(combos)}  ({time.time()-t0:.0f}s)",
                  file=sys.stderr)
        kw = {"k": int(cut_val)} if cut_kind == "k" else {"pct": float(cut_val)}
        cfg = SelectionConfig(sides="both", bar_times=None, max_positions=cap,
                              allocation=alloc, unique_symbols=uniq,
                              balance_sides=True, **kw)
        base = {"cut_kind": cut_kind, "cut_val": cut_val, "max_positions": cap,
                "allocation": alloc, "unique_symbols": uniq}
        try:
            trades = select_ranked(ranked_all, cfg)
        except ValueError as exc:
            rows.append({**base, "refused": True, "reason": str(exc)[:70]})
            continue

        m = run_backtest(h, cfg, cost, trades=trades).metrics
        rows.append({**base, "refused": False, "reason": "",
                     "sharpe": m["sharpe"],
                     "per_trade_bps": m["per_trade_bps_net"],
                     "n_trades": m["n_trades"],
                     "trades_per_day": round(m["n_trades"] / m["n_sessions"], 2),
                     "utilisation": m["mean_utilisation"],
                     "vol_pct": m["ann_vol_pct"],
                     "max_dd_pct": m["max_drawdown_pct"],
                     "total_pct": m["total_return_pct"],
                     **landing(trades)})

    df = pd.DataFrame(rows)
    ok = df[~df["refused"]].copy()

    # Incumbent reference, same window and cost.
    inc = {}
    for kk in (2, 3):
        c = SelectionConfig(k=kk, sides="both", bar_times=WIDE_AM,
                            max_positions=20, balance_sides=True)
        t = select_ranked(ranked_wide, c)
        m = run_backtest(h, c, cost, trades=t).metrics
        inc[f"wide_am_k{kk}_cap20"] = {
            "sharpe": m["sharpe"], "per_trade_bps": m["per_trade_bps_net"],
            "trades_per_day": round(m["n_trades"] / m["n_sessions"], 2),
            "utilisation": m["mean_utilisation"], "vol_pct": m["ann_vol_pct"],
            **landing(t)}

    # k vs pct convergence
    conv = []
    for cap, alloc, uniq in product(grid["max_positions"], grid["allocation"],
                                    grid["unique_symbols"]):
        a = ok[(ok.cut_kind == "k") & (ok.cut_val == 1) &
               (ok.max_positions == cap) & (ok.allocation == alloc) &
               (ok.unique_symbols == uniq)]
        b = ok[(ok.cut_kind == "pct") & (ok.cut_val == 0.001) &
               (ok.max_positions == cap) & (ok.allocation == alloc) &
               (ok.unique_symbols == uniq)]
        if len(a) == 1 and len(b) == 1:
            conv.append({"max_positions": cap, "allocation": alloc,
                         "unique_symbols": uniq,
                         "k1_trades": int(a.n_trades.iloc[0]),
                         "pct001_trades": int(b.n_trades.iloc[0]),
                         "k1_sharpe": float(a.sharpe.iloc[0]),
                         "pct001_sharpe": float(b.sharpe.iloc[0])})
    cdf = pd.DataFrame(conv)

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(), "command": " ".join(sys.argv),
        "window": args.file, "cost_bps": args.cost,
        "n_configs": int(len(combos)), "n_refused": int(df["refused"].sum()),
        "incumbents": inc,
        "reading_notes": [
            "Compare within utilisation bands. A loose cap lowers vol and "
            "raises Sharpe for reasons unrelated to selection.",
            "effective_bars near 4 means the config is a morning strategy "
            "wearing an all-day label -- check it before calling all-day a win.",
            "fcfs on all bars front-loads to the morning by construction; "
            "reserve is the honest all-day allocator.",
        ],
    }

    outdir = Path(P["outputs"]["artifacts"]) / "08_allday"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = outdir / f"{args.tag}_{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    df.to_csv(outdir / "all_trials.csv", index=False)
    cdf.to_csv(outdir / "k_vs_pct.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 230)
    print("\n" + "=" * 108)
    print(f"ALL-DAY SWEEP -- {args.file}, cost {args.cost} bps, all 19 bars")
    print("=" * 108)
    print("\nincumbents (wide_am, same window and cost):")
    print(pd.DataFrame(inc).T.to_string())

    show = ["cut_kind", "cut_val", "max_positions", "allocation",
            "unique_symbols", "sharpe", "per_trade_bps", "trades_per_day",
            "utilisation", "vol_pct", "max_dd_pct", "morning_share",
            "effective_bars"]

    for lo, hi in [(0.70, 1.01), (0.40, 0.70), (0.0, 0.40)]:
        band = ok[(ok.utilisation >= lo) & (ok.utilisation < hi)]
        if band.empty:
            continue
        print(f"\n--- utilisation [{lo:.2f}, {hi:.2f})  "
              f"({len(band)} configs), top 8 ---")
        print(band.nlargest(8, "sharpe")[show].to_string(index=False))

    print("\n--- allocation, median by group ---")
    print(ok.groupby("allocation")[
        ["sharpe", "utilisation", "morning_share", "effective_bars",
         "trades_per_day"]].median().round(3).to_string())

    print("\n--- cut, median by group ---")
    print(ok.groupby(["cut_kind", "cut_val"])[
        ["sharpe", "trades_per_day", "utilisation", "effective_bars"]
    ].median().round(3).to_string())

    if len(cdf):
        print("\n--- k=1 vs pct=0.001 convergence (should be near-identical) ---")
        print(cdf.to_string(index=False))

    if int(df["refused"].sum()):
        print(f"\nrefused: {int(df['refused'].sum())} (cap still binding)")
    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
