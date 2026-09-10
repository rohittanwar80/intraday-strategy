#!/usr/bin/env python3
"""
scripts/06_finalists.py -- pick a small number of finalists from the sweep.

The sweep produced 665 evaluated configurations. Choosing the top row is the
one thing that must not happen: with that many trials the argmax is a
best-of-N statistic and is biased upward regardless of whether anything is
there.

Two pre-registered rules, both stated before looking at the winner.

RULE 1: prefer plateaus to peaks
--------------------------------
A configuration is scored by the MEDIAN Sharpe of its neighbourhood -- itself
plus the configs one step away in k and in max_positions, holding bars, sides
and unique_symbols fixed.

A config that is good because its neighbours are good sits on a plateau, and
plateaus survive out of sample. A config that is good while its neighbours are
not is a spike, and spikes are the shape overfitting takes. The sweep already
showed one: k=1 has the single best trial in the grid (11.58) and a
BELOW-MEDIAN median (5.34), because one name per section is high variance --
it wins the max and loses the average.

RULE 2: hold utilisation roughly constant
-----------------------------------------
The sweep's top table is dominated by low-utilisation configs -- the best runs
0.618, several near the top run 0.23-0.46 with vol as low as 5.08%. Those
Sharpes come partly from holding less risk, not from selecting better. Ranking
across utilisation levels is partly a leverage ranking.

So finalists are drawn from a stated utilisation band. Configs outside it are
not worse; they are not comparable, and mixing them makes the ranking mean
something other than what it appears to.

Diversity: at most two finalists share the same (bars, sides), so the set does
not collapse onto one corner of the grid.

WHAT THIS CANNOT DO
-------------------
`development_2023_2024` is NOT a clean confirm window. It was used extensively
in Phase 2 -- the baseline, the benchmark tables, the unique_symbols
comparison, the cost sweep, the monthly breakdown. The search/confirm split was
declared afterwards, which is a process failure and is recorded as one.

So a "confirm" run here checks CONSISTENCY -- does a config chosen on 2020-22
behave comparably on 2023-24 -- not out-of-sample performance. Consistency is
worth having and is not the same thing.

The only genuinely clean test remaining is the holdout, spent once, at the end.

Every confirm run appends to docs/confirm_ledger.json. Looks are counted even
when the window is already dirty, because the alternative is losing track of
how dirty it is.

Usage
-----
    python scripts/06_finalists.py                      # propose only
    python scripts/06_finalists.py --confirm            # also run on 2023-24
    python scripts/06_finalists.py --util-min 0.7 --n 5
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

from src.backtest.engine import by_period, run_backtest  # noqa: E402
from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402
from src.execution.costs import CostConfig  # noqa: E402
from src.portfolio.selection import NEEDED, SelectionConfig, select  # noqa: E402

SEARCH_WINDOW = "development_2020_2022"
CONFIRM_WINDOW = "development_2023_2024"
HEADLINE = "sharpe_c20"

# Grid orders, for defining "one step away". Must match scripts/05_sweep.py.
K_ORDER = [1, 2, 3, 5, 8, 15]
CAP_ORDER = [10, 20, 40]

BAR_SETS = {
    "peak": ["10:50", "11:10"],
    "morning": ["09:50", "10:10", "10:30", "10:50"],
    "midday": ["11:10", "11:30", "11:50", "12:10"],
    "wide_am": ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"],
    "10:50": ["10:50"],
    "09:50": ["09:50"],
    "all": None,
}


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def latest_sweep(P: dict) -> Path | None:
    root = Path(P["outputs"]["artifacts"]) / "05_sweep"
    if not root.is_dir():
        return None
    runs = sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "all_trials.csv").exists())
    return runs[-1] if runs else None


def neighbourhood_score(df: pd.DataFrame) -> pd.Series:
    """Median Sharpe of each config's one-step neighbourhood (Rule 1)."""
    kpos = {v: i for i, v in enumerate(K_ORDER)}
    cpos = {v: i for i, v in enumerate(CAP_ORDER)}
    lookup = {(r.bars, r.sides, r.unique_symbols, r.k, r.max_positions):
              getattr(r, HEADLINE)
              for r in df.itertuples(index=False)}

    scores, sizes = [], []
    for r in df.itertuples(index=False):
        vals = [getattr(r, HEADLINE)]
        ki, ci = kpos.get(r.k), cpos.get(r.max_positions)
        for dk in (-1, 1):
            if ki is not None and 0 <= ki + dk < len(K_ORDER):
                v = lookup.get((r.bars, r.sides, r.unique_symbols,
                                K_ORDER[ki + dk], r.max_positions))
                if v is not None and np.isfinite(v):
                    vals.append(v)
        for dc in (-1, 1):
            if ci is not None and 0 <= ci + dc < len(CAP_ORDER):
                v = lookup.get((r.bars, r.sides, r.unique_symbols,
                                r.k, CAP_ORDER[ci + dc]))
                if v is not None and np.isfinite(v):
                    vals.append(v)
        scores.append(float(np.median(vals)))
        sizes.append(len(vals))
    return pd.Series(scores, index=df.index), pd.Series(sizes, index=df.index)


def pick_finalists(trials: pd.DataFrame, util_min: float, util_max: float,
                   n: int, max_per_group: int) -> pd.DataFrame:
    ok = trials[(~trials["refused"]) & trials[HEADLINE].notna()].copy()
    ok = ok[(ok["utilisation"] >= util_min) & (ok["utilisation"] <= util_max)]
    if ok.empty:
        return ok
    ok["plateau_score"], ok["neighbours"] = neighbourhood_score(ok)
    ok["spike_gap"] = ok[HEADLINE] - ok["plateau_score"]
    ok = ok.sort_values("plateau_score", ascending=False)

    chosen, counts = [], {}
    for r in ok.itertuples(index=False):
        key = (r.bars, r.sides)
        if counts.get(key, 0) >= max_per_group:
            continue
        chosen.append(r)
        counts[key] = counts.get(key, 0) + 1
        if len(chosen) >= n:
            break
    return pd.DataFrame(chosen)


def to_config(row) -> SelectionConfig:
    return SelectionConfig(
        k=int(row.k), sides=row.sides, bar_times=BAR_SETS[row.bars],
        max_positions=int(row.max_positions),
        unique_symbols=bool(row.unique_symbols),
        balance_sides=(row.sides == "both"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-dir", type=Path, default=None)
    ap.add_argument("--util-min", type=float, default=0.70)
    ap.add_argument("--util-max", type=float, default=1.00)
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--max-per-group", type=int, default=2)
    ap.add_argument("--cost", type=float, default=20.0)
    ap.add_argument("--confirm", action="store_true",
                    help=f"also run finalists on {CONFIRM_WINDOW}. "
                         f"Appends to docs/confirm_ledger.json.")
    args = ap.parse_args()

    P = load_paths()
    sweep_dir = args.sweep_dir or latest_sweep(P)
    if sweep_dir is None:
        print("FATAL: no sweep output found. Run scripts/05_sweep.py first.",
              file=sys.stderr)
        return 2

    trials = pd.read_csv(sweep_dir / "all_trials.csv")
    print(f"sweep: {sweep_dir.name}  ({len(trials)} trials)", file=sys.stderr)

    fin = pick_finalists(trials, args.util_min, args.util_max,
                         args.n, args.max_per_group)
    if fin.empty:
        print(f"FATAL: no trials in utilisation band "
              f"[{args.util_min}, {args.util_max}]", file=sys.stderr)
        return 1

    pd.set_option("display.width", 220)
    show = ["bars", "k", "sides", "max_positions", "unique_symbols",
            "utilisation", HEADLINE, "plateau_score", "spike_gap",
            "neighbours", "per_trade_c20", "vol_pct", "max_dd_pct"]
    print("\n" + "=" * 100)
    print(f"FINALISTS -- plateau-ranked, utilisation in "
          f"[{args.util_min}, {args.util_max}]")
    print("=" * 100)
    print(fin[show].to_string(index=False))
    print("\nspike_gap = own Sharpe minus neighbourhood median. Large positive "
          "means\nthe config is better than its neighbours, which is the shape "
          "overfitting takes.")

    out = {
        "created": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "sweep_dir": str(sweep_dir),
        "search_window": SEARCH_WINDOW,
        "rules": {
            "rule_1": "rank by median Sharpe of the one-step neighbourhood "
                      "(plateaus over peaks)",
            "rule_2": f"utilisation held within [{args.util_min}, "
                      f"{args.util_max}] so the ranking is not a leverage "
                      f"ranking",
            "diversity": f"at most {args.max_per_group} per (bars, sides)",
            "headline_cost_bps": args.cost,
        },
        "finalists": fin[show].to_dict("records"),
        "confirm_window_status": (
            "CONTAMINATED. development_2023_2024 was used extensively in "
            "Phase 2 (baseline, benchmarks, unique_symbols test, cost sweep, "
            "monthly breakdown) BEFORE the search/confirm split was declared. "
            "A run there checks consistency, not out-of-sample performance. "
            "The only clean test remaining is the holdout."),
    }

    (sweep_dir / "finalists.json").write_text(json.dumps(out, indent=2, default=str))

    if not args.confirm:
        print(f"\nwritten to {sweep_dir / 'finalists.json'}")
        print(f"\n{CONFIRM_WINDOW} NOT TOUCHED. Pass --confirm to run there.")
        return 0

    # ---------------------------------------------------------------- confirm
    print("\n" + "!" * 100)
    print(f"CONSISTENCY CHECK on {CONFIRM_WINDOW}")
    print("This window is already contaminated -- see confirm_window_status.")
    print("Recording the look anyway, because losing count is worse.")
    print("!" * 100)

    hc = load_handover(CONFIRM_WINDOW, columns=NEEDED)
    hs = load_handover(SEARCH_WINDOW, columns=NEEDED)
    cost = CostConfig(spread_bps=args.cost)

    rows = []
    for r in fin.itertuples(index=False):
        cfg = to_config(r)
        rec = {"bars": r.bars, "k": int(r.k), "sides": r.sides,
               "max_positions": int(r.max_positions),
               "unique_symbols": bool(r.unique_symbols),
               "search_sharpe": round(float(getattr(r, HEADLINE)), 3),
               "search_plateau": round(float(r.plateau_score), 3)}
        for tag, h in (("search", hs), ("confirm", hc)):
            try:
                m = run_backtest(h, cfg, cost).metrics
                rec[f"{tag}_sharpe_run"] = m["sharpe"]
                rec[f"{tag}_per_trade"] = m["per_trade_bps_net"]
                rec[f"{tag}_vol"] = m["ann_vol_pct"]
                rec[f"{tag}_maxdd"] = m["max_drawdown_pct"]
                rec[f"{tag}_util"] = m["mean_utilisation"]
            except ValueError as exc:
                rec[f"{tag}_error"] = str(exc)[:70]
        if "search_sharpe_run" in rec and "confirm_sharpe_run" in rec:
            rec["decay"] = round(rec["confirm_sharpe_run"] - rec["search_sharpe_run"], 3)
            rec["ratio"] = round(rec["confirm_sharpe_run"] /
                                 rec["search_sharpe_run"], 3) if rec["search_sharpe_run"] else None
        rows.append(rec)

    conf = pd.DataFrame(rows)
    conf.to_csv(sweep_dir / "confirm_results.csv", index=False)

    print("\n" + conf.to_string(index=False))
    print("\nratio = confirm Sharpe / search Sharpe. Near 1.0 is consistent. "
          "Well below 1.0\nmeans the config was fitted to the search window. "
          "Above 1.0 is not good news --\nit is the wrong direction and means "
          "the windows differ, as upstream's 1.33x did.")

    # ---- ledger: append, never overwrite ----
    ledger_path = Path(P["roots"]["project"]) / "docs" / "confirm_ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else \
        {"window": CONFIRM_WINDOW,
         "note": "Contaminated before the split was declared; see Phase 2.",
         "looks": []}
    ledger["looks"].append({
        "at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "command": " ".join(sys.argv),
        "sweep_dir": str(sweep_dir),
        "n_configs": int(len(fin)),
        "configs": fin[["bars", "k", "sides", "max_positions",
                        "unique_symbols"]].to_dict("records"),
        "result_ratios": conf.get("ratio", pd.Series(dtype=float)).tolist(),
    })
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, indent=2, default=str))

    print(f"\nlook #{len(ledger['looks'])} recorded in {ledger_path}")
    print(f"written to {sweep_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
