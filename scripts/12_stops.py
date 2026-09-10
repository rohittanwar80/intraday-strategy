#!/usr/bin/env python3
"""
scripts/12_stops.py -- do stops help, on real intrabar paths?

Spec §5.4, and the warning attached to it:

    A stop at the far end of the opening bar, floored at 0.1 ATR, produced a
    49.3% stop rate with mean R -0.554 against holds at +0.482 -- the stop was
    too tight for a six-hour hold, and the strategy lost 60% over five years
    partly because of it. Stops interact with holding period; a tight stop on
    a long hold is a tax.

That is the ORB benchmark and it is the prior. This tests whether the same
thing happens to a signal that actually works.

Two realism points, because this is where stop backtests flatter themselves
-------------------------------------------------------------------------
**Gap-through.** If a bar OPENS past the stop level, the fill is the open, not
the stop. A model that always fills at the stop level pretends the worst fills
away. Handled explicitly and reported as `gap_through_frac`.

**Within-bar ordering is unknown.** We have OHLC per five-minute bar, not the
sequence inside it. For a trailing stop that matters: assuming the high came
first -- extending the trail -- before checking the low is the optimistic
assumption, and it is how trailing stops get overstated. Here the trailing
level is computed from bars STRICTLY BEFORE the current one, so the trail
cannot be extended by the same bar that stops it.

For the initial stop, if a bar's low touches the level the trade stops at the
level. That is the conventional assumption and it is mildly optimistic on thin
names, since a touch is not a guaranteed fill.

**Stop slippage.** A stop becomes a market order in a fast move. `--stop-slip`
charges extra bps on stopped trades only. Default 10 -- on top of the base
cost, so a stopped trade pays 30 bps at the default settings.

Distances are in ATR multiples, using `ibar_atr_pct_14` from the panel, which
spec §5.4 says is directly usable as a stop distance in price terms.

Reported
--------
    stop_rate       fraction stopped -- compare against ORB's 49.3%
    mean_R_stopped  mean return / stop distance for stopped trades
    mean_R_held     same for trades that ran to 15:55
    sharpe, tpd, per-trade, utilisation

R is the natural unit: it says what a stop costs relative to what it risks, and
it is what makes the ORB numbers comparable.

SEARCH WINDOW by default.

Usage
-----
    python scripts/12_stops.py
    python scripts/12_stops.py --confirm --stop-slip 15
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import run_backtest  # noqa: E402
from src.data.bars import BarPaths  # noqa: E402
from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402
from src.execution.costs import CostConfig  # noqa: E402
from src.portfolio.selection import NEEDED, SelectionConfig, select  # noqa: E402

SEARCH_WINDOW = "development_2020_2022"
CONFIRM_WINDOW = "development_2023_2024"
WIDE_AM = ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"]
LOCKED = dict(k=2, sides="both", bar_times=WIDE_AM, max_positions=20,
              balance_sides=True)

ATR_MULTS = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0]
ORB_REF = {"stop_rate": 0.493, "mean_R_stopped": -0.554, "mean_R_held": 0.482}


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def simulate(paths: pd.DataFrame, meta: pd.DataFrame, mult: float,
             trailing: bool) -> pd.DataFrame:
    """Walk each path and apply the stop. One row per trade.

    meta: trade_id, side, atr_pct, panel_exit_price
    """
    p = paths.sort_values(["trade_id", "bar_seq"], kind="mergesort")
    tid = p["trade_id"].to_numpy()
    op = p["open"].to_numpy(float)
    hi = p["high"].to_numpy(float)
    lo = p["low"].to_numpy(float)
    cl = p["close"].to_numpy(float)

    starts = np.flatnonzero(np.r_[True, tid[1:] != tid[:-1]])
    ends = np.r_[starts[1:], len(tid)]

    m = meta.set_index("trade_id")
    sides = m["side"].to_dict()
    atrs = m["atr_pct"].to_dict()

    out = []
    for s, e in zip(starts, ends):
        t = int(tid[s])
        side = sides[t]
        atr = atrs.get(t, np.nan)
        entry = op[s]
        if not np.isfinite(atr) or atr <= 0 or entry <= 0:
            out.append((t, cl[e - 1], False, 0, False))
            continue

        dist = mult * atr                      # fraction of price
        is_long = side == "long"
        extreme = entry                        # running high (long) / low (short)
        exit_px, stopped, stop_bar, gapped = cl[e - 1], False, 0, False

        for i in range(s, e):
            level = (extreme * (1 - dist)) if is_long else (extreme * (1 + dist))
            if is_long:
                if op[i] <= level:             # gapped through: fill at the open
                    exit_px, stopped, gapped = op[i], True, True
                elif lo[i] <= level:
                    exit_px, stopped = level, True
            else:
                if op[i] >= level:
                    exit_px, stopped, gapped = op[i], True, True
                elif hi[i] >= level:
                    exit_px, stopped = level, True
            if stopped:
                stop_bar = i - s
                break
            # Trail updates only AFTER this bar has been tested, so the same
            # bar cannot both extend the trail and be stopped by the extended
            # level. Static stop keeps `extreme` at entry.
            if trailing:
                extreme = max(extreme, hi[i]) if is_long else min(extreme, lo[i])

        out.append((t, exit_px, stopped, stop_bar, gapped))

    r = pd.DataFrame(out, columns=["trade_id", "stop_exit_price", "stopped",
                                   "stop_bar", "gapped_through"])
    r["entry_open"] = [op[s] for s in starts]
    r["stop_dist"] = mult * r["trade_id"].map(atrs)
    return r


def build(trades: pd.DataFrame, sim: pd.DataFrame, cost_bps: float,
          slip_bps: float) -> pd.DataFrame:
    t = trades.reset_index(drop=True).copy()
    t["trade_id"] = t.index
    j = t.merge(sim, on="trade_id", how="left")
    px = j["stop_exit_price"].fillna(j["exit_price"])
    raw = px / j["entry_price"] - 1.0
    j["ret"] = np.where(j["side"] == "long", raw, -raw)
    # Stopped trades pay extra: a stop is a market order in a fast move.
    j["extra_bps"] = np.where(j["stopped"].fillna(False), slip_bps, 0.0)
    j["ret"] = j["ret"] - j["extra_bps"] / 1e4
    j["R"] = np.where(j["stop_dist"] > 0, j["ret"] / j["stop_dist"], np.nan)
    return j


def main() -> int:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=20.0)
    ap.add_argument("--stop-slip", type=float, default=10.0)
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()

    P = load_paths()
    cost = CostConfig(spread_bps=args.cost)
    cfg = SelectionConfig(**LOCKED)
    bp = BarPaths()

    windows = [SEARCH_WINDOW] + ([CONFIRM_WINDOW] if args.confirm else [])
    rows = []

    for win in windows:
        print(f"\n=== {win} ===", file=sys.stderr)
        h = load_handover(win, columns=NEEDED + ["ibar_atr_pct_14"])
        trades = select(h, cfg)
        paths = bp.paths_for_trades(trades)

        v = bp.validate_against_panel(paths, trades)
        print(f"path validation: {v['verdict']}", file=sys.stderr)
        if v["frac_over_tol"] and v["frac_over_tol"] > 1e-3:
            print("FATAL: paths disagree with the panel.", file=sys.stderr)
            return 1

        meta = trades.reset_index(drop=True)[["side"]].copy()
        meta["trade_id"] = meta.index
        meta["atr_pct"] = h.df.set_index(
            ["symbol", "date", "bar_time"]).loc[
            pd.MultiIndex.from_frame(
                trades[["symbol", "date", "bar_time"]]), "ibar_atr_pct_14"].to_numpy()
        meta["panel_exit_price"] = trades["exit_price"].to_numpy()

        # Does an unstopped path end where the panel says it does?
        last = paths.sort_values(["trade_id", "bar_seq"]).groupby(
            "trade_id").last()["close"]
        rel = (meta.set_index("trade_id")["panel_exit_price"] - last).abs() / last
        print(f"exit reconciliation: max rel err {float(rel.max()):.3e}, "
              f"n over 1e-6 {(rel > 1e-6).sum()}", file=sys.stderr)

        base = run_backtest(h, cfg, cost, trades=trades).metrics
        rows.append({"window": win, "stop": "none", "mult": np.nan,
                     "stop_rate": 0.0, "gap_through_frac": 0.0,
                     "mean_R_stopped": np.nan, "mean_R_held": np.nan,
                     "sharpe": base["sharpe"],
                     "per_trade_bps": base["per_trade_bps_net"],
                     "tpd": round(base["n_trades"] / base["n_sessions"], 2),
                     "vol_pct": base["ann_vol_pct"],
                     "max_dd_pct": base["max_drawdown_pct"]})

        for trailing in (False, True):
            kind = "trailing" if trailing else "initial"
            for mult in ATR_MULTS:
                sim = simulate(paths, meta, mult, trailing)
                j = build(trades, sim, args.cost, args.stop_slip)
                m = run_backtest(h, cfg, cost, trades=j).metrics
                st = j["stopped"].fillna(False)
                rows.append({
                    "window": win, "stop": kind, "mult": mult,
                    "stop_rate": round(float(st.mean()), 4),
                    "gap_through_frac": round(float(
                        j.loc[st, "gapped_through"].mean()), 4) if st.any() else 0.0,
                    "median_stop_bar": float(j.loc[st, "stop_bar"].median())
                    if st.any() else np.nan,
                    "mean_R_stopped": round(float(j.loc[st, "R"].mean()), 3)
                    if st.any() else np.nan,
                    "mean_R_held": round(float(j.loc[~st, "R"].mean()), 3)
                    if (~st).any() else np.nan,
                    "sharpe": m["sharpe"],
                    "per_trade_bps": m["per_trade_bps_net"],
                    "tpd": round(m["n_trades"] / m["n_sessions"], 2),
                    "vol_pct": m["ann_vol_pct"],
                    "max_dd_pct": m["max_drawdown_pct"]})
                print(f"  {kind} {mult}x ATR: stop rate "
                      f"{rows[-1]['stop_rate']:.1%}, sharpe {m['sharpe']:.2f}",
                      file=sys.stderr)
        bp.clear()   # 34M cached rows per window

    res = pd.DataFrame(rows)
    outdir = Path(P["outputs"]["artifacts"]) / "12_stops"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = outdir / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    res.to_csv(outdir / "results.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(), "command": " ".join(sys.argv),
        "config": LOCKED, "cost_bps": args.cost,
        "stop_slip_bps": args.stop_slip,
        "orb_reference": ORB_REF,
        "elapsed_sec": round(time.time() - t0, 1),
        "reading_notes": [
            "R = return / stop distance. ORB: 49.3% stopped, mean R -0.554 "
            "stopped against +0.482 held.",
            "gap_through_frac is the share of stops filled at a bar's open "
            "rather than at the level -- worse than the stop price.",
            "A stop must beat the no-stop row on SHARPE. Lower vol alone is "
            "not a win; the cap is fixed, so vol can be cut by trading less.",
        ]}, indent=2, default=str))

    pd.set_option("display.width", 220)
    for win in windows:
        print("\n" + "=" * 96)
        print(f"STOPS -- {win}, cost {args.cost} bps, "
              f"stop slippage {args.stop_slip} bps")
        print("=" * 96)
        print(res[res.window == win].drop(columns="window").to_string(index=False))
    print(f"\nORB reference: {json.dumps(ORB_REF)}")
    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
