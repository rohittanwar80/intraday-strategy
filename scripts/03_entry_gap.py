#!/usr/bin/env python3
"""
scripts/03_entry_gap.py  (v2)

Spec §12 task 2, second half: the two spread proxies that need the bar tree,
plus a full accounting of bars that do not exist.

    gap_bps    = (open_{t+1} / close_t - 1) x 1e4
    range_bps  = (high - low) / open x 1e4     on the ENTRY bar

Changes from v1
---------------
v1 reported a single counter, rows_missing_a_bar = 731 (4.2%), which conflated
two very different situations:

  * the SIGNAL bar is missing -- a lookup problem, or a name that did not trade
    in the window its features were built from
  * the ENTRY bar is missing -- the handover carries an entry_price at a bar
    that never printed

The second is not bookkeeping. G4 verified zero null entry_price across all
17.9M rows, so every such row has a price assigned to a bar with no trade in
it. That is a fill assumption baked into the panel, sitting in the middle of
the feasibility question this script exists to answer.

v2 separates them and tests the obvious hypothesis: for an entry-bar miss, does
entry_price equal the open of the NEXT bar that did print? If yes, upstream
used a next-print fill rule -- defensible, but undocumented, and it means
"entry at the open of t+1" is not literally true for those rows. If it matches
nothing, the price was constructed some other way and we need to find out how.

Also reported: whether misses land on half-days. Amendments §1 records half-day
sessions as 42 regular bars with the 13:00 auction bar as post, so a scoring
bar at 14:10 on an early-close date has no successor by construction. That
would be benign and fully explained.

Bar convention (amendments §1, reading A)
----------------------------------------
Spec times are t_start. A signal at t_start 09:35 fills at ts_close 09:45 =
t_start 09:40 -- the next FIVE-MINUTE bar, not the next scoring bar.

DEVELOPMENT ONLY (§16).

Usage
-----
    python scripts/03_entry_gap.py
    python scripts/03_entry_gap.py --days 80 --seed 1
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
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data.paths import load_paths  # noqa: E402

ET = "America/New_York"
FILL_OFFSET_MIN = 5

HANDOVER_COLS = ["symbol", "date", "bar_time", "index_name", "score_pct",
                 "target", "entry_price", "bars_remaining"]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def symbol_dir(P: dict, sym: str) -> tuple[Path | None, str]:
    """raw_russell first, then raw. Returns the tree it came from, so a symbol
    resolving to the wrong tree is visible rather than silent -- the failure
    mode amendments §6.3 records in the upstream paths.py."""
    for root, name in ((P["bars"]["russell"], "russell"), (P["bars"]["sp500"], "sp500")):
        d = Path(root) / sym
        if d.is_dir():
            return d, name
    return None, "none"


def load_bars(P: dict, sym: str, year: int) -> tuple[pd.DataFrame | None, str]:
    d, tree = symbol_dir(P, sym)
    if d is None:
        return None, tree
    f = d / f"5min_{year}.parquet"
    if not f.exists():
        return None, tree
    try:
        df = pq.read_table(
            f, columns=["t_start", "open", "high", "low", "close", "volume"]
        ).to_pandas()
    except Exception:  # noqa: BLE001
        return None, tree
    ts = pd.to_datetime(df["t_start"], unit="s", utc=True).dt.tz_convert(ET)
    df["d"] = ts.dt.date
    df["hhmm"] = ts.dt.strftime("%H:%M")
    df["mins"] = ts.dt.hour * 60 + ts.dt.minute
    return df, tree


def load_early_closes(P: dict) -> dict:
    """date -> market close in minutes past midnight ET, for early closes only."""
    try:
        cal = pd.read_parquet(P["bars"]["sessions"])
    except Exception:  # noqa: BLE001
        return {}
    dcol = next((c for c in ("date", "session_date", "session") if c in cal.columns), None)
    ccol = next((c for c in cal.columns if "close" in c.lower()), None)
    if dcol is None or ccol is None:
        return {}
    out = {}
    for d, c in zip(pd.to_datetime(cal[dcol]).dt.date, cal[ccol]):
        try:
            ts = pd.to_datetime(str(c))
            mins = ts.hour * 60 + ts.minute
        except Exception:  # noqa: BLE001
            continue
        if mins < 16 * 60:
            out[d] = mins
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="development_2023_2024")
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cut", type=float, default=0.01)
    ap.add_argument("--outdir", type=Path, default=None)
    args = ap.parse_args()

    P = load_paths()
    manifest = json.loads(Path(P["handover"]["manifest"]).read_text())
    spec = manifest["files"].get(args.file)
    if spec is None or spec.get("phase") != "development":
        print(f"FATAL: {args.file!r} is not a development file (§16).", file=sys.stderr)
        return 2

    path = Path(P["handover"]["dir"]) / spec["file"]
    outdir = args.outdir or Path(P["outputs"]["artifacts"]) / "03_entry_gap"
    rng = np.random.default_rng(args.seed)
    early = load_early_closes(P)
    print(f"session calendar: {len(early)} early-close dates", file=sys.stderr)

    hv = pq.read_table(path, columns=HANDOVER_COLS).to_pandas()
    hv["date"] = pd.to_datetime(hv["date"]).dt.date

    dates = np.sort(hv["date"].unique())
    take = rng.choice(dates, size=min(args.days, len(dates)), replace=False)
    hv = hv[hv["date"].isin(set(take))].copy()

    long_ = hv[hv.score_pct >= 1 - args.cut].assign(grp="top")
    short = hv[hv.score_pct <= args.cut].assign(grp="bottom")
    ctrl = hv.sample(n=min((len(long_) + len(short)) // 2, len(hv)),
                     random_state=args.seed).assign(grp="control")
    sample = pd.concat([long_, short, ctrl], ignore_index=True)

    t = pd.to_datetime(sample["bar_time"].astype(str), format="%H:%M")
    sample["entry_hhmm"] = (t + pd.Timedelta(minutes=FILL_OFFSET_MIN)).dt.strftime("%H:%M")
    sample["entry_mins"] = t.dt.hour * 60 + t.dt.minute + FILL_OFFSET_MIN
    sample["year"] = pd.to_datetime(sample["date"]).dt.year

    pairs = sample[["symbol", "year"]].drop_duplicates()
    print(f"{len(sample):,} sampled rows, {len(pairs):,} symbol-years", file=sys.stderr)

    out, misses = [], []
    for i, (sym, yr) in enumerate(pairs.itertuples(index=False), 1):
        if i % 200 == 0:
            print(f"  ... {i}/{len(pairs)}", file=sys.stderr)
        bars, tree = load_bars(P, sym, int(yr))
        need = sample[(sample.symbol == sym) & (sample.year == yr)]

        if bars is None:
            for r in need.itertuples(index=False):
                misses.append({"symbol": sym, "date": r.date, "bar_time": str(r.bar_time),
                               "grp": r.grp, "index_name": r.index_name, "tree": tree,
                               "which": "no_bar_file", "hv_entry_price": r.entry_price,
                               "next_print_hhmm": None, "next_print_open": None,
                               "next_print_delta_min": None, "early_close_min": None})
            continue

        idx = bars.set_index(["d", "hhmm"])
        by_day = {d: g for d, g in bars.groupby("d", observed=True)}

        for r in need.itertuples(index=False):
            has_sig = (r.date, str(r.bar_time)) in idx.index
            has_ent = (r.date, r.entry_hhmm) in idx.index

            if has_sig and has_ent:
                s_bar = idx.loc[(r.date, str(r.bar_time))]
                e_bar = idx.loc[(r.date, r.entry_hhmm)]
                if isinstance(s_bar, pd.DataFrame):
                    s_bar = s_bar.iloc[0]
                if isinstance(e_bar, pd.DataFrame):
                    e_bar = e_bar.iloc[0]
                if s_bar["close"] > 0 and e_bar["open"] > 0:
                    out.append({
                        "grp": r.grp, "symbol": sym, "date": r.date,
                        "bar_time": str(r.bar_time), "target": r.target,
                        "hv_entry_price": r.entry_price,
                        "raw_entry_open": float(e_bar["open"]),
                        "signal_close": float(s_bar["close"]),
                        "entry_high": float(e_bar["high"]),
                        "entry_low": float(e_bar["low"]),
                        "entry_volume": float(e_bar["volume"]),
                    })
                    continue
                which = "non_positive_price"
            elif not has_sig and not has_ent:
                which = "both"
            elif not has_ent:
                which = "entry"
            else:
                which = "signal"

            nxt_hhmm = nxt_open = nxt_delta = None
            day = by_day.get(r.date)
            if day is not None and which != "signal":
                later = day[(day.mins >= r.entry_mins) & (day["open"] > 0)]
                if len(later):
                    row = later.sort_values("mins").iloc[0]
                    nxt_hhmm = str(row["hhmm"])
                    nxt_open = float(row["open"])
                    nxt_delta = int(row["mins"] - r.entry_mins)

            misses.append({
                "symbol": sym, "date": r.date, "bar_time": str(r.bar_time),
                "grp": r.grp, "index_name": r.index_name, "tree": tree,
                "which": which, "hv_entry_price": r.entry_price,
                "next_print_hhmm": nxt_hhmm, "next_print_open": nxt_open,
                "next_print_delta_min": nxt_delta,
                "early_close_min": early.get(r.date),
            })

    df = pd.DataFrame(out)
    md = pd.DataFrame(misses)

    # ------------------------------------------------------------------ misses
    miss_summary: dict = {"n_attempted": int(len(sample)),
                          "n_matched": int(len(df)),
                          "n_missed": int(len(md))}
    if len(md):
        miss_summary["by_which"] = md["which"].value_counts().to_dict()
        miss_summary["by_group"] = md["grp"].value_counts().to_dict()
        miss_summary["by_tree"] = md["tree"].value_counts().to_dict()
        miss_summary["by_index_name"] = md["index_name"].astype(str).value_counts().to_dict()

        ec = md.dropna(subset=["early_close_min"])
        after_close = 0
        if len(ec):
            bm = pd.to_datetime(ec["bar_time"], format="%H:%M")
            after_close = int(((bm.dt.hour * 60 + bm.dt.minute + FILL_OFFSET_MIN)
                               >= ec["early_close_min"]).sum())
        miss_summary["on_early_close_dates"] = int(len(ec))
        miss_summary["explained_by_early_close"] = after_close

        ent = md[md.which.isin(["entry", "both", "non_positive_price"])].dropna(
            subset=["next_print_open"])
        if len(ent):
            rel = (ent.hv_entry_price - ent.next_print_open).abs() / ent.next_print_open
            frac = float((rel < 1e-6).mean())
            miss_summary["next_print_test"] = {
                "n_testable": int(len(ent)),
                "n_match_1e-6": int((rel < 1e-6).sum()),
                "frac_match": round(frac, 4),
                "median_delta_min": float(ent.next_print_delta_min.median()),
                "p90_delta_min": float(ent.next_print_delta_min.quantile(0.90)),
                "verdict": ("next-print fill rule CONFIRMED" if frac > 0.9 else
                            "NOT a simple next-print rule -- entry_price came from "
                            "somewhere else and needs explaining"),
            }

        miss_summary["by_bar_time"] = (
            md.groupby(["which", "bar_time"]).size()
              .unstack(fill_value=0).to_dict("index"))

    # -------------------------------------------------------------- gap stats
    df["gap_bps"] = (df.raw_entry_open / df.signal_close - 1) * 1e4
    df["range_bps"] = (df.entry_high - df.entry_low) / df.raw_entry_open * 1e4
    df["join_rel_err"] = (df.hv_entry_price - df.raw_entry_open).abs() / df.raw_entry_open
    df["adverse_bps"] = np.where(df.grp == "bottom", -df.gap_bps, df.gap_bps)

    join = {"n": int(len(df)), "max_rel_err": float(df.join_rel_err.max()),
            "n_over_1e-4": int((df.join_rel_err > 1e-4).sum())}

    def stats(g):
        return {"n": int(len(g)),
                "gap_bps_mean": round(float(g.gap_bps.mean()), 2),
                "abs_gap_med": round(float(g.gap_bps.abs().median()), 2),
                "range_bps_med": round(float(g.range_bps.median()), 2),
                "half_range_med": round(float(g.range_bps.median()) / 2, 2),
                "edge_bps": round(float(g.target.mean()) * 1e4, 2)}

    by_grp = pd.DataFrame([{"grp": k, **stats(v)} for k, v in df.groupby("grp")])

    summary = {"run_at": datetime.now(timezone.utc).isoformat(),
               "git_commit": git_commit(), "command": " ".join(sys.argv),
               "file": args.file, "days_sampled": int(len(take)), "seed": args.seed,
               "join_check": join, "miss_accounting": miss_summary}

    outdir.mkdir(parents=True, exist_ok=True)
    df.to_csv(outdir / "rows.csv", index=False)
    md.to_csv(outdir / "missing_bars.csv", index=False)
    by_grp.to_csv(outdir / "by_group.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 200)
    print("\n" + "=" * 92)
    print(f"ENTRY GAP + MISSING BAR ACCOUNTING -- {args.file}")
    print("=" * 92)
    print(json.dumps(summary, indent=2, default=str))
    print("\n--- by group ---")
    print(by_grp.to_string(index=False))
    if len(md):
        print("\n--- 20 example misses ---")
        print(md.head(20)[["symbol", "date", "bar_time", "grp", "which",
                           "hv_entry_price", "next_print_hhmm", "next_print_open",
                           "next_print_delta_min"]].to_string(index=False))
    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
