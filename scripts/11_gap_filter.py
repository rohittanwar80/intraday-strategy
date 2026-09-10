#!/usr/bin/env python3
"""
scripts/11_gap_filter.py -- don't buy what has already run away from you.

The rule
--------
Between the signal bar's close and the entry bar's open, the price moves. If it
has moved far enough AGAINST the position, the premise of the trade may already
be spent. This tests declining those trades.

    gap_bps      = (entry_open / signal_close - 1) x 1e4
    adverse_bps  = +gap for a long, -gap for a short

Mechanically this is a marketable limit priced ABOVE the reference for a buy:
take it at market, but not if that costs more than x bps above the signal
close. Unfilled means no position.

This is NOT a passive limit
---------------------------
A resting limit below the market fills only when price comes to you, so fills
are conditioned on the price having moved against you and the misses are
disproportionately the names that ran. For a signal selecting the day's most
extreme movers that is close to worst case, and it is invisible in a backtest
because the limit price reports as an execution improvement.

A gap filter has the opposite shape: it REMOVES trades rather than selectively
filling them, and it removes them on information available before the fill. No
adverse selection, and the skipped trades are simply absent.

THE PLACEBO
-----------
The result is ambiguous without a control. Large-gap trades are also
high-volatility trades, so a filter on adverse gap might work only because it
removes volatile names -- nothing to do with direction.

So every threshold is run three ways:

    adverse      skip if the gap went AGAINST the position   <- the hypothesis
    favourable   skip if the gap went WITH it, same size     <- the placebo
    absolute     skip on |gap|, either direction             <- volatility only

If adverse and favourable help equally, direction is irrelevant and the real
finding is "avoid volatile entries", which should be built on a volatility
measure rather than on the gap.

Capital treatment
-----------------
A skipped trade leaves its slot IDLE. Capital is max_positions slots fixed
before the session (amendments §7.10), and declining a trade does not conjure
a replacement -- the next candidate is not known until the next bar. Utilisation
therefore falls as the filter tightens, and the daily return carries that.

Backfilling from deeper in the ranking is the more realistic live behaviour and
is a separate build; noted, not done.

SEARCH WINDOW by default. --confirm runs the locked config on both windows and
records a look.

Usage
-----
    python scripts/11_gap_filter.py
    python scripts/11_gap_filter.py --confirm
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

from src.backtest.engine import run_backtest  # noqa: E402
from src.data.bars import BarPaths, hhmm_to_min  # noqa: E402
from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402
from src.execution.costs import CostConfig  # noqa: E402
from src.portfolio.selection import NEEDED, SelectionConfig, select  # noqa: E402

SEARCH_WINDOW = "development_2020_2022"
CONFIRM_WINDOW = "development_2023_2024"

# The locked configuration (amendments v0.1c + the all-day run).
WIDE_AM = ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"]
LOCKED = dict(k=2, sides="both", bar_times=WIDE_AM, max_positions=20,
              balance_sides=True)

THRESHOLDS = [5.0, 10.0, 15.0, 20.0, 30.0, 50.0]
VARIANTS = ["adverse", "favourable", "absolute"]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def add_gap(trades: pd.DataFrame, bp: BarPaths) -> pd.DataFrame:
    """Attach signal_close and gap_bps to each trade.

    The paths from BarPaths start at the ENTRY bar, so the signal bar's close
    is fetched separately from the same cache.
    """
    t = trades.reset_index(drop=True).copy()
    t["trade_id"] = t.index
    t["sess_date"] = pd.to_datetime(t["date"]).dt.date
    t["sess_year"] = pd.to_datetime(t["date"]).dt.year
    t["sig_min"] = t["bar_time"].map(hhmm_to_min)

    closes = np.full(len(t), np.nan)
    for sym, yr in t[["symbol", "sess_year"]].drop_duplicates().itertuples(index=False):
        bars = bp.load(sym, int(yr))
        if bars is None:
            continue
        idx = bars.set_index(["d", "mins"])["close"]
        sub = t[(t["symbol"] == sym) & (t["sess_year"] == yr)]
        for r in sub.itertuples(index=False):
            try:
                v = idx.loc[(r.sess_date, r.sig_min)]
            except KeyError:
                continue
            closes[r.trade_id] = float(v if np.isscalar(v) else np.asarray(v)[0])

    t["signal_close"] = closes
    t["gap_bps"] = (t["entry_price"] / t["signal_close"] - 1.0) * 1e4
    t["adverse_bps"] = np.where(t["side"] == "short", -t["gap_bps"], t["gap_bps"])
    return t


def apply_filter(t: pd.DataFrame, variant: str, thr: float) -> pd.DataFrame:
    """Rows to KEEP. Trades with no signal close are kept -- the filter cannot
    fire without the information, and dropping them would silently change the
    population being compared."""
    g = t["gap_bps"]
    a = t["adverse_bps"]
    if variant == "adverse":
        keep = ~(a > thr)
    elif variant == "favourable":
        keep = ~(a < -thr)
    else:
        keep = ~(g.abs() > thr)
    return t[keep.fillna(True)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=20.0)
    ap.add_argument("--confirm", action="store_true",
                    help=f"also run on {CONFIRM_WINDOW}; records a look")
    args = ap.parse_args()

    P = load_paths()
    cost = CostConfig(spread_bps=args.cost)
    cfg = SelectionConfig(**LOCKED)
    bp = BarPaths()

    windows = [SEARCH_WINDOW] + ([CONFIRM_WINDOW] if args.confirm else [])
    all_rows, all_buckets = [], []

    for win in windows:
        print(f"\n=== {win} ===", file=sys.stderr)
        h = load_handover(win, columns=NEEDED)
        trades = select(h, cfg)
        print(f"attaching gaps to {len(trades):,} trades ...", file=sys.stderr)
        t = add_gap(trades, bp)

        n_no_close = int(t["signal_close"].isna().sum())
        print(f"  signal close missing on {n_no_close} trades "
              f"({n_no_close/len(t):.2%}) -- kept, filter cannot fire",
              file=sys.stderr)

        # Decile table: does adverse gap predict a worse outcome at all?
        d = t.dropna(subset=["adverse_bps"]).copy()
        d["decile"] = pd.qcut(d["adverse_bps"].rank(method="first"), 10,
                              labels=False) + 1
        b = d.groupby("decile").agg(
            n=("ret", "size"),
            adverse_lo=("adverse_bps", "min"),
            adverse_hi=("adverse_bps", "max"),
            gross_bps=("ret", lambda s: round(float(s.mean()) * 1e4, 2)),
        )
        b["net_bps"] = (b["gross_bps"] - args.cost).round(2)
        b["window"] = win
        all_buckets.append(b.reset_index())

        base = run_backtest(h, cfg, cost, trades=trades).metrics
        all_rows.append({"window": win, "variant": "none", "threshold": np.inf,
                         "frac_removed": 0.0, "sharpe": base["sharpe"],
                         "per_trade_bps": base["per_trade_bps_net"],
                         "tpd": round(base["n_trades"] / base["n_sessions"], 2),
                         "n_trades": base["n_trades"],
                         "utilisation": base["mean_utilisation"],
                         "vol_pct": base["ann_vol_pct"],
                         "max_dd_pct": base["max_drawdown_pct"]})

        for variant in VARIANTS:
            for thr in THRESHOLDS:
                kept = apply_filter(t, variant, thr)
                if kept.empty:
                    continue
                m = run_backtest(h, cfg, cost, trades=kept).metrics
                all_rows.append({
                    "window": win, "variant": variant, "threshold": thr,
                    "frac_removed": round(1 - len(kept) / len(t), 4),
                    "sharpe": m["sharpe"],
                    "per_trade_bps": m["per_trade_bps_net"],
                    "tpd": round(m["n_trades"] / m["n_sessions"], 2),
                    "n_trades": m["n_trades"],
                    "utilisation": m["mean_utilisation"],
                    "vol_pct": m["ann_vol_pct"],
                    "max_dd_pct": m["max_drawdown_pct"]})

        # side asymmetry at one threshold
        for side in ("long", "short"):
            sub = t[t["side"] == side].dropna(subset=["adverse_bps"])
            all_rows.append({
                "window": win, "variant": f"_diag_{side}", "threshold": 20.0,
                "frac_removed": round(float((sub["adverse_bps"] > 20).mean()), 4),
                "sharpe": np.nan,
                "per_trade_bps": round(float(sub["ret"].mean()) * 1e4, 2),
                "tpd": np.nan, "n_trades": len(sub),
                "utilisation": np.nan, "vol_pct": np.nan, "max_dd_pct": np.nan})

    res = pd.DataFrame(all_rows)
    buckets = pd.concat(all_buckets, ignore_index=True)

    outdir = Path(P["outputs"]["artifacts"]) / "11_gap_filter"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = outdir / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    res.to_csv(outdir / "results.csv", index=False)
    buckets.to_csv(outdir / "deciles.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(), "command": " ".join(sys.argv),
        "config": LOCKED, "cost_bps": args.cost, "windows": windows,
        "reading_notes": [
            "Compare adverse against favourable at the same threshold. If they "
            "help equally, direction is irrelevant and the finding is 'avoid "
            "volatile entries', not 'avoid gaps'.",
            "Skipped trades leave their slot idle -- utilisation falls as the "
            "filter tightens, and the daily return carries that.",
        ]}, indent=2, default=str))

    pd.set_option("display.width", 220)
    for win in windows:
        w = res[res.window == win]
        print("\n" + "=" * 96)
        print(f"GAP FILTER -- {win}, cost {args.cost} bps, "
              f"config k=2 both wide_am cap20")
        print("=" * 96)
        print("\ndeciles of adverse gap (does it predict at all?):")
        print(buckets[buckets.window == win].drop(columns="window")
              .to_string(index=False))
        print("\nfilter results:")
        print(w[~w.variant.str.startswith("_diag")][
            ["variant", "threshold", "frac_removed", "sharpe", "per_trade_bps",
             "tpd", "n_trades", "utilisation", "vol_pct", "max_dd_pct"]
        ].to_string(index=False))
        print("\nside asymmetry at 20 bps (gross per-trade, unfiltered):")
        print(w[w.variant.str.startswith("_diag")][
            ["variant", "frac_removed", "per_trade_bps", "n_trades"]
        ].to_string(index=False))

    print(f"\nwritten to {outdir.resolve()}")
    print("\nADVERSE must beat FAVOURABLE at the same threshold, or the result "
          "is about volatility, not gaps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
