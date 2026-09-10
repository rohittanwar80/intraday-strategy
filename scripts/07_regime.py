#!/usr/bin/env python3
"""
scripts/07_regime.py -- does the best SIDE depend on the regime?

Switch, not filter
------------------
Upstream tested regime FILTERS on stage 1 -- trade these days, sit out those --
and all eight failed on TOTAL edge. The best, SPY > 200SMA, raised per-day
return from +5.47 to +6.94 bps while trading 80% of days: a 1.9% total
improvement. MANIFEST caution 1. That trade-off is invisible in a per-trade
number and it killed every filter tried.

A regime SWITCH is a different object. It trades every day and changes only
which side is on. Days traded is constant by construction, so the mechanism
that killed the filters does not apply. Stage 2 was never tested either way.

This still reports total alongside per-trade, because the discipline is right
regardless and because a switch can still lose money while looking better per
trade if the regime it favours is the rare one.

What is being searched
----------------------
Six regime variables x two split methods = 12 rules, and each rule gets a free
choice of which side wins in each bucket. That is a search, and the honest
comparison is against ALWAYS-BOTH -- the thing we would do without a regime
model -- not against the best bucket.

A switch that beats always-both by less than the spread of the 12 rules is
noise. The output reports the full spread so that is checkable.

Look-ahead
----------
Every regime column is known at signal time:

    spy_above_200sma   lagged one session
    spy_ret_prev       prior session
    vxx_vol_63/pct     lagged
    spy_intraday       SPY open to THIS bar -- contemporaneous with the signal
                       bar, and entry is the NEXT bar's open, so it is known
    breadth_above_*    same bar, same reasoning

Regime is evaluated at the DAY level, taken from the first traded bar of the
session. Bar-level switching is a larger design and would need its own run.

DEVELOPMENT SEARCH WINDOW ONLY.

Usage
-----
    python scripts/07_regime.py
    python scripts/07_regime.py --k 3 --bars wide_am --cost 20
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import TRADING_DAYS, run_backtest  # noqa: E402
from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402
from src.execution.costs import CostConfig  # noqa: E402
from src.portfolio.selection import NEEDED, SelectionConfig  # noqa: E402

SEARCH_WINDOW = "development_2020_2022"

REGIME_COLS = ["spy_above_200sma", "spy_ret_prev", "vxx_vol_pct",
               "spy_intraday", "breadth_above_open", "breadth_above_sma50"]

BAR_SETS = {
    "peak": ["10:50", "11:10"],
    "morning": ["09:50", "10:10", "10:30", "10:50"],
    "midday": ["11:10", "11:30", "11:50", "12:10"],
    "wide_am": ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"],
}
SIDES = ["long", "short", "both"]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def sharpe(r: pd.Series) -> float:
    sd = float(r.std(ddof=1))
    return float(r.mean()) / sd * np.sqrt(TRADING_DAYS) if sd > 0 else np.nan


def day_regime(panel: pd.DataFrame, col: str, bars: list[str]) -> pd.Series:
    """One regime value per session, from the first traded bar of the day."""
    sub = panel[panel["bar_time"].isin(bars)][["date", "bar_time", col]]
    first = sorted(bars)[0]
    at_first = sub[sub["bar_time"] == first]
    if at_first.empty:
        at_first = sub
    return at_first.groupby("date", observed=True)[col].median()


def bucketise(s: pd.Series, method: str) -> pd.Series:
    if method == "binary":
        u = s.dropna().unique()
        if len(u) <= 2:                      # already binary, e.g. a flag
            return s.map(lambda v: f"={v:g}" if pd.notna(v) else None)
        return pd.Series(np.where(s > s.median(), "high", "low"), index=s.index)
    q = pd.qcut(s.rank(method="first"), 3, labels=["low", "mid", "high"])
    return pd.Series(q.astype(str), index=s.index)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=SEARCH_WINDOW)
    ap.add_argument("--bars", default="peak", choices=sorted(BAR_SETS))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--max-positions", type=int, default=20)
    ap.add_argument("--cost", type=float, default=20.0,
                    help="first feasible level for the baseline (amendments §6)")
    ap.add_argument("--tag", default="regime")
    args = ap.parse_args()

    if args.file != SEARCH_WINDOW:
        print(f"REFUSED: regime work is a search. Use {SEARCH_WINDOW}.",
              file=sys.stderr)
        return 2

    P = load_paths()
    bars = BAR_SETS[args.bars]
    cost = CostConfig(spread_bps=args.cost)

    h = load_handover(args.file, columns=NEEDED + REGIME_COLS)
    have = [c for c in REGIME_COLS if c in h.df.columns]
    missing = [c for c in REGIME_COLS if c not in h.df.columns]
    if missing:
        print(f"note: absent from the panel, skipped: {missing}", file=sys.stderr)

    # Daily return series for each side, same capital, same bars.
    print(f"running {len(SIDES)} side variants ...", file=sys.stderr)
    daily: dict[str, pd.Series] = {}
    base_metrics: dict[str, dict] = {}
    for side in SIDES:
        cfg = SelectionConfig(k=args.k, sides=side, bar_times=bars,
                              max_positions=args.max_positions,
                              balance_sides=(side == "both"))
        res = run_backtest(h, cfg, cost)
        daily[side] = res.daily["net"]
        base_metrics[side] = res.metrics

    idx = daily["both"].index
    always = {s: sharpe(daily[s]) for s in SIDES}
    best_fixed = max(always, key=lambda s: always[s])

    print("\nalways-on baselines (no regime model):", file=sys.stderr)
    for s in SIDES:
        m = base_metrics[s]
        print(f"  {s:5s} sharpe {always[s]:6.2f}  total {m['total_return_pct']:8.2f}%"
              f"  per-trade {m['per_trade_bps_net']:6.2f}  n {m['n_trades']:,}"
              f"  util {m['mean_utilisation']:.2f}", file=sys.stderr)

    rules, bucket_rows = [], []
    for col in have:
        for method in ("binary", "tercile"):
            reg = day_regime(h.df, col, bars)
            reg = reg.reindex(pd.to_datetime(idx.date)
                              if not isinstance(reg.index[0], pd.Timestamp) else idx)
            reg.index = idx
            lab = bucketise(reg, method)
            if lab.dropna().nunique() < 2:
                continue

            # Which side wins in each bucket -- this is the fitted part.
            chosen, per_bucket = {}, []
            for b in sorted(lab.dropna().unique()):
                mask = (lab == b).to_numpy()
                if mask.sum() < 20:
                    continue
                tot = {s: float(daily[s][mask].sum()) for s in SIDES}
                win = max(tot, key=lambda s: tot[s])
                chosen[b] = win
                row = {"regime": col, "method": method, "bucket": b,
                       "n_days": int(mask.sum()), "winner": win}
                for s in SIDES:
                    row[f"{s}_total_pct"] = round(tot[s] * 100, 2)
                    row[f"{s}_sharpe"] = round(sharpe(daily[s][mask]), 2)
                per_bucket.append(row)
            if len(chosen) < 2:
                continue
            bucket_rows.extend(per_bucket)

            # The switched strategy: same days, different side.
            sw = pd.Series(0.0, index=idx)
            for b, s in chosen.items():
                mask = (lab == b).to_numpy()
                sw[mask] = daily[s][mask]

            rules.append({
                "regime": col, "method": method,
                "n_buckets": len(chosen),
                "rule": ", ".join(f"{b}->{s}" for b, s in sorted(chosen.items())),
                "switch_sharpe": round(sharpe(sw), 3),
                "switch_total_pct": round(float(sw.sum()) * 100, 2),
                "always_both_sharpe": round(always["both"], 3),
                "always_both_total_pct": round(float(daily["both"].sum()) * 100, 2),
                "sharpe_gain": round(sharpe(sw) - always["both"], 3),
                "total_gain_pct": round(float(sw.sum() - daily["both"].sum()) * 100, 2),
                "switches_sides": len(set(chosen.values())) > 1,
            })

    rdf = pd.DataFrame(rules).sort_values("sharpe_gain", ascending=False)
    bdf = pd.DataFrame(bucket_rows)

    spread = None
    if len(rdf):
        spread = {
            "n_rules_tried": int(len(rdf)),
            "gain_max": float(rdf.sharpe_gain.max()),
            "gain_median": float(rdf.sharpe_gain.median()),
            "gain_min": float(rdf.sharpe_gain.min()),
            "gain_sd": float(rdf.sharpe_gain.std(ddof=1)) if len(rdf) > 1 else None,
            "n_rules_that_actually_switch": int(rdf.switches_sides.sum()),
            "verdict": None,
        }
        best = float(rdf.sharpe_gain.max())
        sd = spread["gain_sd"] or 0.0
        spread["verdict"] = (
            "the best rule's gain is within one SD of the spread across rules -- "
            "consistent with noise" if sd and best < sd else
            "the best rule's gain exceeds the spread across rules -- worth a "
            "consistency check, still fitted in-sample")

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(), "command": " ".join(sys.argv),
        "window": args.file,
        "base_config": {"k": args.k, "bars": args.bars,
                        "max_positions": args.max_positions,
                        "cost_bps": args.cost},
        "always_on": {s: {"sharpe": round(always[s], 3),
                          "total_pct": base_metrics[s]["total_return_pct"],
                          "per_trade_bps": base_metrics[s]["per_trade_bps_net"],
                          "n_trades": base_metrics[s]["n_trades"]} for s in SIDES},
        "best_fixed_side": best_fixed,
        "search_spread": spread,
        "reading_notes": [
            "The comparison is against ALWAYS-BOTH, not against the best "
            "bucket. A bucket table always shows one side winning somewhere.",
            "Each rule fits which side wins in each bucket, in-sample. The "
            "spread across rules is the reference for whether the best one "
            "means anything.",
            "Days traded is constant across all rows -- that is what makes "
            "this a switch and not the filter that failed upstream.",
        ],
    }

    outdir = Path(P["outputs"]["artifacts"]) / "07_regime"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = outdir / f"{args.tag}_{args.bars}_k{args.k}_{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    rdf.to_csv(outdir / "rules.csv", index=False)
    bdf.to_csv(outdir / "buckets.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 230)
    print("\n" + "=" * 104)
    print(f"REGIME SWITCH -- {args.file}, bars={args.bars}, k={args.k}, "
          f"cost={args.cost} bps")
    print("=" * 104)
    print(json.dumps(summary["always_on"], indent=2))
    print("\nrules, best first:")
    print(rdf.to_string(index=False))
    print("\nper-bucket detail:")
    print(bdf.to_string(index=False))
    print("\nsearch spread:")
    print(json.dumps(spread, indent=2))
    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
