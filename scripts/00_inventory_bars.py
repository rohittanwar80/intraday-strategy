#!/usr/bin/env python3
"""
00_inventory_bars_v2.py -- read-only inventory of the Russell 5-minute bar tree.

Changes from v1
---------------
1. Per-session symbol COUNTS, not just a union of dates. v1 reported that the
   holdout union reached 2026-08-31 while 23 of 25 sampled symbols stopped at
   08-27; the union hid the ragged download boundary. This version writes the
   count per session so the cliff is visible.
2. Year-gap detection. A symbol with 5min files for 2018-2022 and 2026 but not
   2023-2025 has a three-year hole with data on both sides. Benign reading:
   delisting plus ticker reuse. Malignant reading: the same thing unnoticed,
   splicing two companies into one price series and producing a fake move of
   arbitrary size -- in exactly the extreme-mover tail this strategy trades.
   Filename-only, so it covers every year at no extra read cost.
3. A suggested truncation date: the latest session where coverage reaches
   --coverage-floor of the scanned universe. REPORTED, never applied.

Still read-only. Touches no scores, spends no looks, writes only to --outdir.
Results are written to disk BEFORE anything is printed.

Usage
-----
    python 00_inventory_bars_v2.py --limit 25          # smoke test
    python 00_inventory_bars_v2.py                     # full run, 2025+2026
    python 00_inventory_bars_v2.py --years 2024 2025 2026

Note: --years controls which files are OPENED (for session dates). Year-gap
detection reads filenames only and always covers every year on disk.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

DEFAULT_DATA_ROOT = Path(
    "/Users/rohittanwar/Documents/Claude/Projects/intraday/intraday-rank/data"
)
HOLDOUT_START = date(2025, 9, 1)
HOLDOUT_END = date(2026, 8, 31)
ET = "America/New_York"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--years", type=int, nargs="+", default=[2025, 2026],
                   help="which 5min_{year}.parquet files to open (default: 2025 2026)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--outdir", type=Path, default=Path("artifacts/00_inventory"))
    p.add_argument("--coverage-floor", type=float, default=0.90,
                   help="fraction of scanned symbols required for a session to "
                        "count as intact (default 0.90)")
    return p.parse_args()


def session_dates(path: Path) -> tuple[list[date], str | None]:
    """Distinct ET session dates in one bar file, plus any error string.

    Prefers `t_start` (unix seconds, bar START) per the upstream schema note:
    session labels derive from t_start, not ts_close, so the spec's wall-clock
    times map onto stored rows. Falls back to ts_close only if t_start is
    absent, and says so, rather than silently mixing conventions.
    """
    try:
        names = set(pq.read_schema(path).names)
    except Exception as exc:  # noqa: BLE001 -- want the message, not the class
        return [], f"schema_read_failed: {exc}"

    if "t_start" in names:
        col, kind = "t_start", "t_start"
    elif "ts_close" in names:
        col, kind = "ts_close", "ts_close_fallback"
    else:
        return [], f"no_time_column (has: {sorted(names)[:8]})"

    try:
        s = pq.read_table(path, columns=[col]).column(col).to_pandas()
    except Exception as exc:  # noqa: BLE001
        return [], f"read_failed: {exc}"

    if s.empty:
        return [], "empty_file"

    if kind == "t_start":
        ts = pd.to_datetime(s, unit="s", utc=True).dt.tz_convert(ET)
    else:
        ts = pd.to_datetime(s, utc=True).dt.tz_convert(ET)
        # ts_close labels a bar's END; step back one bar so a 16:00 close maps
        # to its own session. 5-minute grid assumed.
        ts = ts - pd.Timedelta(minutes=5)

    return sorted(set(ts.dt.date)), (None if kind == "t_start"
                                     else "used_ts_close_fallback")


def year_gaps(years: list[int]) -> list[int]:
    """Interior missing years: present before AND after, absent itself."""
    if len(years) < 2:
        return []
    return [y for y in range(min(years), max(years) + 1) if y not in set(years)]


def main() -> int:
    args = parse_args()
    russell = args.data_root / "raw_russell"
    if not russell.is_dir():
        print(f"FATAL: {russell} is not a directory", file=sys.stderr)
        return 2

    symbols = sorted(d.name for d in russell.iterdir() if d.is_dir())
    n_total_symbols = len(symbols)
    if args.limit:
        symbols = symbols[: args.limit]

    print(f"scanning {len(symbols)} of {n_total_symbols} symbol dirs "
          f"(opening years {args.years}) ...", file=sys.stderr)

    rows: list[dict] = []
    session_counts: Counter[date] = Counter()

    for i, sym in enumerate(symbols, 1):
        if i % 250 == 0:
            print(f"  ... {i}/{len(symbols)}", file=sys.stderr)

        sdir = russell / sym
        files = sorted(sdir.glob("5min_*.parquet"))
        years_on_disk = sorted(
            int(p.stem.split("_")[-1]) for p in files
            if p.stem.split("_")[-1].isdigit()
        )
        gaps = year_gaps(years_on_disk)

        sym_dates: set[date] = set()
        notes: list[str] = []
        for y in args.years:
            f = sdir / f"5min_{y}.parquet"
            if not f.exists():
                notes.append(f"missing_5min_{y}")
                continue
            d, err = session_dates(f)
            if err:
                notes.append(f"{y}:{err}")
            sym_dates.update(d)

        session_counts.update(sym_dates)  # one increment per symbol per session
        in_holdout = {d for d in sym_dates if HOLDOUT_START <= d <= HOLDOUT_END}

        rows.append({
            "symbol": sym,
            "has_daily": (sdir / "daily.parquet").exists(),
            "n_5min_files": len(files),
            "years_on_disk": ",".join(map(str, years_on_disk)),
            "year_gaps": ",".join(map(str, gaps)),
            "n_year_gaps": len(gaps),
            "first_date_scanned": min(sym_dates).isoformat() if sym_dates else None,
            "last_date_scanned": max(sym_dates).isoformat() if sym_dates else None,
            "n_sessions_scanned": len(sym_dates),
            "n_sessions_in_holdout": len(in_holdout),
            "notes": ";".join(notes),
        })

    df = pd.DataFrame(rows)

    # --- per-session coverage --------------------------------------------
    cov = (pd.Series(session_counts, name="n_symbols")
             .sort_index()
             .rename_axis("date")
             .reset_index())
    cov["coverage"] = cov.n_symbols / max(len(symbols), 1)
    cov_holdout = cov[(cov.date >= HOLDOUT_START) & (cov.date <= HOLDOUT_END)]

    intact = cov_holdout[cov_holdout.coverage >= args.coverage_floor]
    suggested_end = intact.date.max() if len(intact) else None

    # --- session calendar -------------------------------------------------
    cal: dict = {"found": False}
    cal_files = sorted((args.data_root / "interim").glob("sessions*.parquet"))
    if cal_files:
        try:
            cdf = pd.read_parquet(cal_files[0])
            dcol = next((c for c in ("date", "session_date", "session")
                         if c in cdf.columns), None)
            if dcol:
                cd = pd.to_datetime(cdf[dcol]).dt.date
                cal = {"found": True, "file": cal_files[0].name,
                       "min": min(cd).isoformat(), "max": max(cd).isoformat(),
                       "n_sessions_in_holdout": int(
                           sum(HOLDOUT_START <= d <= HOLDOUT_END for d in cd))}
            else:
                cal = {"found": True, "file": cal_files[0].name,
                       "error": f"no date column; columns={list(cdf.columns)[:10]}"}
        except Exception as exc:  # noqa: BLE001
            cal = {"found": True, "file": cal_files[0].name, "error": str(exc)}

    summary = {
        "data_root": str(args.data_root),
        "years_opened": args.years,
        "coverage_floor": args.coverage_floor,
        "symbol_dirs_total": n_total_symbols,
        "symbols_scanned": len(symbols),
        "symbols_with_zero_scanned_sessions": int((df.n_sessions_scanned == 0).sum()),
        "symbols_with_notes": int((df.notes != "").sum()),
        "symbols_with_year_gaps": int((df.n_year_gaps > 0).sum()),
        "holdout_window": [HOLDOUT_START.isoformat(), HOLDOUT_END.isoformat()],
        "holdout_union_n_sessions": int(len(cov_holdout)),
        "holdout_sessions_at_or_above_floor": int(len(intact)),
        "suggested_holdout_end": suggested_end.isoformat() if suggested_end else None,
        "holdout_sessions_per_symbol": {
            "min": int(df.n_sessions_in_holdout.min()) if len(df) else None,
            "p05": int(df.n_sessions_in_holdout.quantile(0.05)) if len(df) else None,
            "median": int(df.n_sessions_in_holdout.median()) if len(df) else None,
            "max": int(df.n_sessions_in_holdout.max()) if len(df) else None,
        },
        "session_calendar": cal,
    }

    # --- write BEFORE printing -------------------------------------------
    args.outdir.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.outdir / "per_symbol.csv", index=False)
    cov.to_csv(args.outdir / "session_coverage.csv", index=False)
    df[df.n_year_gaps > 0].to_csv(args.outdir / "year_gap_symbols.csv", index=False)
    (args.outdir / "summary.json").write_text(json.dumps(summary, indent=2))

    # --- report -----------------------------------------------------------
    print("\n" + "=" * 68)
    print("BAR TREE INVENTORY  (v2)")
    print("=" * 68)
    print(json.dumps(summary, indent=2))

    print("\n--- last 15 sessions: symbol coverage ---")
    tail = cov_holdout.tail(15).copy()
    tail["coverage"] = (tail.coverage * 100).round(1).astype(str) + "%"
    print(tail.to_string(index=False))

    if (df.n_year_gaps > 0).any():
        print(f"\n--- symbols with interior year gaps "
              f"({int((df.n_year_gaps > 0).sum())} of {len(symbols)}) ---")
        print(df[df.n_year_gaps > 0][["symbol", "years_on_disk", "year_gaps"]]
              .head(25).to_string(index=False))

    if (df.notes != "").any():
        print(f"\n--- symbols with notes ({int((df.notes != '').sum())}) ---")
        print(df[df.notes != ""][["symbol", "years_on_disk", "notes"]]
              .head(25).to_string(index=False))

    print(f"\nwritten to {args.outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
