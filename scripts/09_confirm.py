#!/usr/bin/env python3
"""
scripts/09_confirm.py -- run explicit configurations on both development
windows and record the look.

`06_finalists.py` picks finalists from a sweep by its own rules. This is for
the other case: a specific configuration arrived at by argument or from a
different run, which needs the same consistency check and the same ledger
entry.

WHAT THIS IS NOT
----------------
`development_2023_2024` is CONTAMINATED. It was used throughout Phase 2 --
baseline, benchmarks, unique_symbols test, cost sweep, monthly breakdown --
before the search/confirm split was declared. A ratio near 1.0 means the
config behaves consistently across two windows it has both been exposed to.
It is NOT out-of-sample validation.

The only clean test is the holdout, spent once, at the end.

Every run appends to docs/confirm_ledger.json. The count starts late and the
ledger says so, but losing track entirely is worse.

Reading the ratio
-----------------
    ~1.0        consistent
    well <1.0   fitted to the search window
    >1.2        the WRONG direction -- out-of-sample should land at or below
                in-sample. Upstream's validation came in at 1.33x and §16
                flagged it as needing explanation, not celebration.

Usage
-----
    python scripts/09_confirm.py                       # the current candidates
    python scripts/09_confirm.py \
        --config bars=all,k=1,cap=30 \
        --config bars=wide_am,k=3,cap=20
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import run_backtest  # noqa: E402
from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402
from src.execution.costs import CostConfig  # noqa: E402
from src.portfolio.selection import NEEDED, SelectionConfig, select  # noqa: E402

SEARCH_WINDOW = "development_2020_2022"
CONFIRM_WINDOW = "development_2023_2024"
MORNING_BARS = {"09:50", "10:10", "10:30", "10:50"}

BAR_SETS = {
    "all": None,
    "peak": ["10:50", "11:10"],
    "morning": ["09:50", "10:10", "10:30", "10:50"],
    "midday": ["11:10", "11:30", "11:50", "12:10"],
    "wide_am": ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"],
}

# The configs under consideration as of amendments v0.1c plus the all-day run.
DEFAULT_CONFIGS = [
    "bars=all,k=1,cap=30",       # all-day candidate, 24 trades/day
    "bars=all,k=2,cap=50",       # all-day, best search Sharpe, 43 trades/day
    "bars=wide_am,k=2,cap=20",   # incumbent, best confirm ratio so far
    "bars=wide_am,k=3,cap=20",   # incumbent, fuller neighbourhood
]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def parse_config(spec: str) -> tuple[str, SelectionConfig]:
    kv = dict(p.split("=", 1) for p in spec.split(","))
    bars_name = kv.get("bars", "wide_am")
    if bars_name not in BAR_SETS:
        raise ValueError(f"unknown bars {bars_name!r}; "
                         f"choose from {sorted(BAR_SETS)}")
    cfg = SelectionConfig(
        k=int(kv["k"]) if "k" in kv else None,
        pct=float(kv["pct"]) if "pct" in kv else None,
        sides=kv.get("sides", "both"),
        bar_times=BAR_SETS[bars_name],
        max_positions=int(kv.get("cap", 20)),
        allocation=kv.get("alloc", "fcfs"),
        unique_symbols=kv.get("uniq", "1") not in ("0", "false", "False"),
        balance_sides=kv.get("sides", "both") == "both",
    )
    return spec, cfg


def landing(trades: pd.DataFrame) -> dict:
    counts = trades["bar_time"].value_counts()
    p = (counts / counts.sum()).to_numpy()
    import numpy as np
    return {"morning_share": round(float(
                trades["bar_time"].isin(MORNING_BARS).mean()), 3),
            "effective_bars": round(float(np.exp(-(p * np.log(p)).sum())), 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", default=None,
                    help="key=value pairs, e.g. bars=all,k=1,cap=30. Repeatable.")
    ap.add_argument("--cost", type=float, default=20.0)
    args = ap.parse_args()

    specs = args.config or DEFAULT_CONFIGS
    try:
        parsed = [parse_config(s) for s in specs]
    except (ValueError, KeyError) as exc:
        print(f"bad config: {exc}", file=sys.stderr)
        return 2

    P = load_paths()
    cost = CostConfig(spread_bps=args.cost)

    print("!" * 96)
    print(f"CONSISTENCY CHECK on {CONFIRM_WINDOW}")
    print("This window is already contaminated -- Phase 2 used it extensively")
    print("before the split was declared. A ratio near 1.0 means consistency,")
    print("NOT out-of-sample validation. The clean test is the holdout.")
    print("!" * 96)

    hs = load_handover(SEARCH_WINDOW, columns=NEEDED)
    hc = load_handover(CONFIRM_WINDOW, columns=NEEDED)

    rows = []
    for spec, cfg in parsed:
        rec = {"config": spec}
        for tag, h in (("search", hs), ("confirm", hc)):
            try:
                trades = select(h, cfg)
                m = run_backtest(h, cfg, cost, trades=trades).metrics
                rec[f"{tag}_sharpe"] = m["sharpe"]
                rec[f"{tag}_per_trade"] = m["per_trade_bps_net"]
                rec[f"{tag}_tpd"] = round(m["n_trades"] / m["n_sessions"], 1)
                rec[f"{tag}_util"] = m["mean_utilisation"]
                rec[f"{tag}_vol"] = m["ann_vol_pct"]
                rec[f"{tag}_maxdd"] = m["max_drawdown_pct"]
                rec[f"{tag}_eff_bars"] = landing(trades)["effective_bars"]
            except ValueError as exc:
                rec[f"{tag}_error"] = str(exc)[:70]
        if "search_sharpe" in rec and "confirm_sharpe" in rec:
            rec["ratio"] = round(rec["confirm_sharpe"] / rec["search_sharpe"], 3) \
                if rec["search_sharpe"] else None
            rec["per_trade_ratio"] = round(
                rec["confirm_per_trade"] / rec["search_per_trade"], 3) \
                if rec["search_per_trade"] else None
        rows.append(rec)

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 240)

    print(f"\ncost {args.cost} bps\n")
    cols = [c for c in ["config", "search_sharpe", "confirm_sharpe", "ratio",
                        "search_per_trade", "confirm_per_trade",
                        "per_trade_ratio", "search_tpd", "confirm_tpd",
                        "search_util", "confirm_util", "search_vol",
                        "confirm_vol", "search_maxdd", "confirm_maxdd",
                        "search_eff_bars", "confirm_eff_bars"]
            if c in df.columns]
    print(df[cols].to_string(index=False))

    ledger_path = Path(P["roots"]["project"]) / "docs" / "confirm_ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else \
        {"window": CONFIRM_WINDOW,
         "note": "Contaminated before the split was declared; see Phase 2.",
         "looks": []}
    ledger["looks"].append({
        "at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "command": " ".join(sys.argv),
        "script": "09_confirm.py",
        "cost_bps": args.cost,
        "configs": specs,
        "ratios": df.get("ratio", pd.Series(dtype=float)).tolist(),
    })
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, indent=2, default=str))

    outdir = Path(P["outputs"]["artifacts"]) / "09_confirm"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    df.to_csv(outdir / f"confirm_{stamp}.csv", index=False)

    print(f"\nlook #{len(ledger['looks'])} recorded in {ledger_path}")
    print(f"written to {outdir / f'confirm_{stamp}.csv'}")
    print("\nratio > 1.2 is the WRONG direction -- see the module docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
