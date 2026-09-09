#!/usr/bin/env python3
"""
scripts/04_backtest.py -- run a configuration and write it down.

Everything up to now printed to a terminal. This writes, because §11.6 is
explicit: a backtest whose provenance is unclear is a backtest that cannot be
reproduced, and an unreproducible good result is worse than no result -- it
invites belief without evidence.

Every run records:

    the handover file's sha256 and the manifest it came from
    the FULL parameter set, including defaults not passed on the command line
    the git commit, and whether the working tree was dirty
    the command, the wall-clock time, the interpreter version

The dirty flag matters. A commit hash identifies code only if the tree is
clean; a result produced from uncommitted edits is not reproducible from that
hash, and saying so is the difference between provenance and decoration.

Collision safety (§11.2 rule 4)
-------------------------------
Upstream, two runs sharing a configuration tag and differing only in years
wrote to the same filename, and a downstream date range looked wrong weeks
later. Every run here lands in its own timestamped directory, so distinct runs
cannot collide. A write-time invariant also asserts the data's date range
matches the file key before anything is written.

Results are written BEFORE anything is printed. §13: a 69-minute build was lost
when a report raised on a stale column name and the write came after it.

Usage
-----
    python scripts/04_backtest.py --k 5 --sides both --bars 10:50,11:10 \
        --balance --tag baseline

    python scripts/04_backtest.py --k 5 --sides long --bars 10:50,11:10 \
        --file development_2020_2022 --no-benchmarks

    python scripts/04_backtest.py --pct 0.001 --sides both --max-positions 60 \
        --cost 20 --tag tight_cut
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import by_period, run_backtest, sweep_costs  # noqa: E402
from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402
from src.evaluate.benchmarks import ORB_REFERENCE, compare  # noqa: E402
from src.execution.costs import CostConfig  # noqa: E402
from src.portfolio.selection import NEEDED, SelectionConfig  # noqa: E402


def git_state() -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.check_output(args, stderr=subprocess.DEVNULL,
                                           text=True).strip()
        except Exception:  # noqa: BLE001
            return ""
    commit = run("git", "rev-parse", "HEAD")
    dirty = bool(run("git", "status", "--porcelain"))
    return {
        "commit": commit or "unknown",
        "dirty": dirty,
        "branch": run("git", "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "note": ("WORKING TREE DIRTY -- this result was produced from "
                 "uncommitted edits and is NOT reproducible from the commit "
                 "hash above") if dirty else "clean",
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", default="development_2023_2024",
                   help="handover key. Non-development requires --allow-spent.")
    p.add_argument("--allow-spent", action="store_true",
                   help="required to read validation. Records a LOOK.")

    p.add_argument("--k", type=int, default=None, help="names per section per side")
    p.add_argument("--pct", type=float, default=None, help="fraction of section")
    p.add_argument("--sides", default="both", choices=["long", "short", "both"])
    p.add_argument("--bars", default=None,
                   help="comma-separated bar times, e.g. 10:50,11:10. "
                        "Required when the daily cap binds.")
    p.add_argument("--max-positions", type=int, default=20)
    p.add_argument("--allocation", default="fcfs", choices=["fcfs", "reserve"])
    p.add_argument("--balance", action="store_true", help="balance_sides")
    p.add_argument("--allow-repeats", action="store_true",
                   help="unique_symbols=False; a name may take several slots")

    p.add_argument("--cost", type=float, default=5.0, help="spread bps, headline run")
    p.add_argument("--borrow", type=float, default=0.0)
    p.add_argument("--no-sweep", action="store_true")
    p.add_argument("--no-benchmarks", action="store_true")
    p.add_argument("--seeds", type=int, default=20, help="random benchmark draws")

    p.add_argument("--tag", default="run", help="directory name under artifacts")
    p.add_argument("--outdir", type=Path, default=None)
    return p.parse_args()


def main() -> int:
    t0 = time.time()
    args = parse_args()

    try:
        sel_cfg = SelectionConfig(
            k=args.k, pct=args.pct, sides=args.sides,
            bar_times=[b.strip() for b in args.bars.split(",")] if args.bars else None,
            max_positions=args.max_positions, allocation=args.allocation,
            unique_symbols=not args.allow_repeats, balance_sides=args.balance,
        )
    except ValueError as exc:
        print(f"bad configuration: {exc}", file=sys.stderr)
        return 2

    cost_cfg = CostConfig(spread_bps=args.cost, borrow_bps=args.borrow)
    P = load_paths()

    try:
        h = load_handover(args.file, columns=NEEDED, allow_spent=args.allow_spent)
    except Exception as exc:  # noqa: BLE001
        print(f"load failed: {exc}", file=sys.stderr)
        return 2

    if h.phase != "development":
        print(f"\n*** {args.file} is phase={h.phase}. This run SPENDS A LOOK. ***",
              file=sys.stderr)
        print(f"*** Record it in {P['upstream_artifacts']['holdout_ledger']} ***\n",
              file=sys.stderr)

    # Write-time invariant (§11.2 rule 3): does the data match its name?
    lo = str(h.df["date"].min().date())
    hi = str(h.df["date"].max().date())
    want_lo, want_hi = h.meta["date_range"]
    if [lo, hi] != [want_lo, want_hi]:
        print(f"FATAL: {args.file} holds {lo}..{hi} but the manifest claims "
              f"{want_lo}..{want_hi}", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = (args.outdir or Path(P["outputs"]["artifacts"]) / "04_backtest")
    outdir = outdir / f"{args.tag}_{args.file}_{stamp}"
    if outdir.exists():
        print(f"FATAL: {outdir} already exists. Refusing to overwrite (§11.2 "
              f"rule 4).", file=sys.stderr)
        return 1

    print(f"running {args.tag}: {sel_cfg.sides} k={sel_cfg.k} pct={sel_cfg.pct} "
          f"bars={args.bars or 'all'} cap={sel_cfg.max_positions} "
          f"cost={cost_cfg.spread_bps}bps ...", file=sys.stderr)

    try:
        res = run_backtest(h, sel_cfg, cost_cfg)
    except ValueError as exc:
        print(f"\nselection refused:\n  {exc}", file=sys.stderr)
        return 2

    sweep = None if args.no_sweep else sweep_costs(h, sel_cfg, borrow_bps=args.borrow)

    bench, extras = (None, {})
    if not args.no_benchmarks:
        print(f"  benchmarks, {args.seeds} random seeds ...", file=sys.stderr)
        bench, extras = compare(h, sel_cfg, cost_cfg, n_seeds=args.seeds)

    by_year = by_period(res.daily, "YE")
    by_month = by_period(res.daily, "ME")

    summary = {
        "provenance": {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_sec": round(time.time() - t0, 1),
            "command": " ".join(sys.argv),
            "git": git_state(),
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "handover": res.meta["handover"],
            "manifest_spent_looks": res.meta["handover"].get("spent_looks"),
        },
        "config": {
            "selection": sel_cfg.describe(),
            "cost": cost_cfg.describe(),
            "file": args.file,
            "phase": h.phase,
            "spends_a_look": h.phase != "development",
        },
        "metrics": res.metrics,
        "selection_summary": res.meta["selection"],
        "feasibility": res.meta["feasibility"],
        "benchmarks_vs_random": extras.get("strategy_vs_random"),
        "orb_reference": ORB_REFERENCE,
        "reading_notes": [
            "ann_return_pct is a function of max_positions, not a result. "
            "Halving the cap doubles it. Sharpe is the invariant.",
            "total_return_pct is an arithmetic sum with no floor. Check "
            "metrics.total_exceeds_capital before quoting it.",
            "Read the cost sweep from the first FEASIBLE row. Levels below a "
            "trade's own tick floor cannot occur.",
            "The universe applies current Russell membership to all dates "
            "(spec §6.3). Random controls for the ranking, not for the level.",
        ],
    }

    # --- write BEFORE printing (§13) ---------------------------------------
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    res.daily.to_csv(outdir / "daily.csv")
    res.trades.to_csv(outdir / "trades.csv", index=False)
    by_year.to_csv(outdir / "by_year.csv")
    by_month.to_csv(outdir / "by_month.csv")
    if sweep is not None:
        sweep.to_csv(outdir / "cost_sweep.csv", index=False)
    if bench is not None:
        bench.to_csv(outdir / "benchmarks.csv", index=False)
        pd.DataFrame(extras.get("random_seeds", [])).to_csv(
            outdir / "random_seeds.csv", index=False)

    # --- report -------------------------------------------------------------
    pd.set_option("display.width", 200)
    m = res.metrics
    print("\n" + "=" * 92)
    print(f"{args.tag}  --  {args.file}  {lo} .. {hi}")
    print("=" * 92)
    if summary["provenance"]["git"]["dirty"]:
        print("!! working tree DIRTY -- not reproducible from the commit hash\n")

    print(f"sharpe {m['sharpe']:.2f}   ann {m['ann_return_pct']:.1f}%   "
          f"vol {m['ann_vol_pct']:.2f}%   maxDD {m['max_drawdown_pct']:.2f}%")
    print(f"per-trade net {m['per_trade_bps_net']:.2f} bps x {m['n_trades']:,} "
          f"trades   traded {m['n_traded_days']}/{m['n_sessions']} days   "
          f"utilisation {m['mean_utilisation']:.2f}")
    if m.get("total_exceeds_capital"):
        print("!! total_return_pct passes -100%: arithmetic sum, book would be gone")

    if sweep is not None:
        print("\ncost sweep:")
        print(sweep.to_string(index=False))
    if bench is not None:
        print("\nbenchmarks:")
        print(bench.to_string(index=False))
    print("\nby year:")
    print(by_year.to_string())

    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
