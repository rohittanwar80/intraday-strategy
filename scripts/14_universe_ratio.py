#!/usr/bin/env python3
"""
scripts/14_universe_ratio.py -- 1,905 symbols in the tree, 900 in the panel.

The problem
-----------
`scripts/13` measured a median of **1,905** Russell symbols per session across
the holdout window. The handover panel's median names per cross-section is
**900** for validation 2025, 769 for 2023-24, 686 for 2020-22.

`score_pct` is a within-cross-section percentile and the frozen config takes
`k=2` per side. Two of 1,905 is the 0.1st percentile; two of 900 is the 0.22nd.
**If the holdout's scored cross-section really were 1,905, `k=2` would be
selecting a materially more extreme slice than in any window the model was
validated on**, and the holdout result would not be comparable to anything
measured so far.

Amendments §11 item 7 already records this effect at smaller scale: the
cross-section grew 686 -> 769 -> 900 across the three panels, and part of the
1.33x validation overshoot is that arithmetic.

The likely explanation
----------------------
The panel's count is POST-eligibility-filter and script 13's is pre-filter.
Upstream §6 records `f_liquidity` alone excluding 25.71% of names. If the pass
rate is roughly 900/1905 = 47% and stable, the holdout's scored cross-section
will land near 900 too and there is no problem.

That is a guess. This measures it.

What it does
------------
1. Inspects `interim/universe_russell.parquet`. If it carries per-date
   eligibility, the answer is there and no bar scan is needed.
2. Otherwise, for one sampled year per window, counts symbols with a print at
   a reference bar in `raw_russell/` and compares against the handover panel's
   own count for the same sessions.

The number that matters is the PASS RATE and whether it is stable. A stable
rate means the holdout's scored section is predictable. A rising rate means the
sections have been growing for a real reason and the holdout will be larger
still -- which is the §11 item 7 effect continuing, and would need handling
before the holdout is spent.

Read-only. No scores joined to targets. Spends nothing.

Usage
-----
    python scripts/14_universe_ratio.py
    python scripts/14_universe_ratio.py --years 2021 2024 2025 2026
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.handover import load_handover  # noqa: E402
from src.data.paths import load_paths  # noqa: E402

ET = "America/New_York"
REF_BAR = "10:50"          # inside the frozen config's window
REF_MIN = 10 * 60 + 50


def inspect_universe(P: dict) -> dict:
    f = Path(P["bars"]["universe_russell"])
    if not f.exists():
        return {"found": False, "path": str(f)}
    try:
        df = pd.read_parquet(f)
    except Exception as exc:  # noqa: BLE001
        return {"found": True, "path": str(f), "error": str(exc)}
    out = {"found": True, "path": str(f), "shape": list(df.shape),
           "columns": list(df.columns)[:25],
           "dtypes": {c: str(t) for c, t in list(df.dtypes.items())[:25]}}
    dcol = next((c for c in ("date", "session_date", "session") if c in df.columns), None)
    out["date_column"] = dcol
    if dcol:
        d = pd.to_datetime(df[dcol])
        out["date_range"] = [str(d.min().date()), str(d.max().date())]
        out["n_dates"] = int(d.dt.date.nunique())
        out["rows_per_date_median"] = float(df.groupby(d.dt.date).size().median())
        out["carries_per_date_eligibility"] = out["n_dates"] > 1
    else:
        out["carries_per_date_eligibility"] = False
    return out


def tree_counts(root: Path, year: int, dates: set) -> dict:
    """Symbols with a print at REF_BAR on each sampled date."""
    syms = sorted(d.name for d in root.iterdir() if d.is_dir())
    counts = {d: 0 for d in dates}
    print(f"  scanning {len(syms)} symbols for {year} ...", file=sys.stderr)
    for i, sym in enumerate(syms, 1):
        if i % 400 == 0:
            print(f"    ... {i}/{len(syms)}", file=sys.stderr)
        f = root / sym / f"5min_{year}.parquet"
        if not f.exists():
            continue
        try:
            s = pq.read_table(f, columns=["t_start"]).column("t_start").to_pandas()
        except Exception:  # noqa: BLE001
            continue
        if s.empty:
            continue
        ts = pd.to_datetime(s, unit="s", utc=True).dt.tz_convert(ET)
        mins = ts.dt.hour * 60 + ts.dt.minute
        hit = ts[mins == REF_MIN].dt.date
        for d in set(hit) & dates:
            counts[d] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+",
                    default=[2021, 2024, 2025, 2026])
    ap.add_argument("--dates-per-year", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    P = load_paths()
    rng = np.random.default_rng(args.seed)
    outdir = Path(P["outputs"]["artifacts"]) / "14_universe_ratio"

    # ------------------------------------------------------------------ (1)
    uni = inspect_universe(P)
    print("universe_russell.parquet:", file=sys.stderr)
    print(json.dumps(uni, indent=2, default=str), file=sys.stderr)

    # ------------------------------------------------------- panel counts
    panel = {}
    for key in ("development_2020_2022", "development_2023_2024", "validation_2025"):
        h = load_handover(key, columns=["symbol", "date", "bar_time", "index_name",
                                        "score", "score_pct", "target",
                                        "entry_price", "exit_price"],
                          allow_spent=(key == "validation_2025"))
        sub = h.df[h.df["bar_time"] == REF_BAR]
        per = sub.groupby("date", observed=True)["symbol"].nunique()
        panel[key] = {"median_at_ref_bar": int(per.median()),
                      "dates": {str(d.date()): int(v) for d, v in per.items()}}
        print(f"{key}: median {panel[key]['median_at_ref_bar']} at {REF_BAR}",
              file=sys.stderr)

    # ------------------------------------------------------------------ (2)
    rus_root = Path(P["bars"]["russell"])
    panel_by_date = {}
    for key, v in panel.items():
        panel_by_date.update(v["dates"])

    rows = []
    for year in args.years:
        cands = [d for d in panel_by_date if d.startswith(str(year))]
        if cands:
            picks = rng.choice(sorted(cands),
                               size=min(args.dates_per_year, len(cands)),
                               replace=False)
            dates = {pd.Timestamp(d).date() for d in picks}
        else:
            # No panel coverage (2026 = holdout). Sample from the tree itself
            # via a liquid reference symbol's calendar.
            ref = rus_root / "AAON" / f"5min_{year}.parquet"
            if not ref.exists():
                any_sym = next((d for d in sorted(rus_root.iterdir())
                                if (d / f"5min_{year}.parquet").exists()), None)
                ref = None if any_sym is None else any_sym / f"5min_{year}.parquet"
            if ref is None:
                continue
            s = pq.read_table(ref, columns=["t_start"]).column("t_start").to_pandas()
            ts = pd.to_datetime(s, unit="s", utc=True).dt.tz_convert(ET)
            alld = sorted(set(ts.dt.date))
            dates = set(rng.choice(alld, size=min(args.dates_per_year, len(alld)),
                                   replace=False))

        counts = tree_counts(rus_root, year, dates)
        for d, n_tree in sorted(counts.items()):
            ds = d.isoformat()
            n_panel = panel_by_date.get(ds)
            rows.append({"date": ds, "year": year,
                         "tree_at_ref_bar": n_tree,
                         "panel_at_ref_bar": n_panel,
                         "pass_rate": round(n_panel / n_tree, 4)
                         if n_panel and n_tree else None})

    df = pd.DataFrame(rows).sort_values("date")
    by_year = df.dropna(subset=["pass_rate"]).groupby("year").agg(
        tree=("tree_at_ref_bar", "median"),
        panel=("panel_at_ref_bar", "median"),
        pass_rate=("pass_rate", "median"),
        n_dates=("date", "size")).round(4)

    proj = None
    if len(by_year) >= 2 and 2026 in df.year.values:
        tree26 = float(df[df.year == 2026]["tree_at_ref_bar"].median())
        rates = by_year["pass_rate"].dropna()
        proj = {
            "holdout_tree_at_ref_bar": tree26,
            "pass_rate_latest_observed": float(rates.iloc[-1]),
            "projected_scored_section": round(tree26 * float(rates.iloc[-1])),
            "validation_2025_actual": panel["validation_2025"]["median_at_ref_bar"],
            "note": "If the projection lands near validation's section size, "
                    "k=2 selects a comparable slice and the holdout is "
                    "comparable. If it is much larger, k must be revisited or "
                    "the cut expressed as a percentile.",
        }

    summary = {"universe_file": uni, "reference_bar": REF_BAR,
               "panel_medians": {k: v["median_at_ref_bar"] for k, v in panel.items()},
               "by_year": by_year.reset_index().to_dict("records"),
               "projection": proj}

    outdir.mkdir(parents=True, exist_ok=True)
    df.to_csv(outdir / "per_date.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 200)
    print("\n" + "=" * 88)
    print(f"UNIVERSE RATIO -- tree vs panel at {REF_BAR}")
    print("=" * 88)
    print("\nper-date:")
    print(df.to_string(index=False))
    print("\nby year:")
    print(by_year.to_string())
    if proj:
        print("\nprojection for the holdout:")
        print(json.dumps(proj, indent=2, default=str))
    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
