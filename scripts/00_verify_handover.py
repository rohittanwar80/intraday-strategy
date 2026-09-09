#!/usr/bin/env python3
"""
Verify the handover dataset before any analysis depends on it.

--------------------------------------------------------------------------------
WHY THIS EXISTS
--------------------------------------------------------------------------------

Four times in the upstream project, a stale or mismatched file produced
plausible numbers that were acted on before the problem was found:

  - An ORB cache keyed only by filename served a 50-symbol/1-year file to a
    503-symbol/5-year run. Three consecutive outputs were identical and all
    looked reasonable.
  - Stage 1 labels covered only 2023 while features covered nine years. Six
    years silently dropped in the join; every table printed "development
    window" results computed from one year.
  - A percentile analysis read files whose paths matched by coincidence,
    producing numbers that disagreed with the training run in both directions.
  - Two runs sharing a configuration tag but differing in years wrote to the
    same filename. The second overwrote the first, and the loss was only
    noticed when a downstream date range looked wrong.

EVERY ONE WAS DETECTABLE FROM THE DATA ITSELF. None was caught by inspection;
all were caught by a downstream number looking odd, which is the expensive way.

So: assertions on load, not inspection after. This script fails loudly rather
than letting an analysis proceed on a file that is not what it claims to be.

--------------------------------------------------------------------------------
WHAT IT CHECKS
--------------------------------------------------------------------------------

  1. STRUCTURE      required columns present, dtypes sane
  2. IDENTITY       date range and row count match the manifest
  3. KEYS           no duplicate (symbol, date, bar_time)
  4. PRICES         positive, non-null, and `target` RECOMPUTES from them
  5. SIGNAL         score_pct spans [0,1] within each cross-section
  6. GRID           bar times consistent, cross-sections plausibly sized
  7. RECONCILE      row counts against the upstream source files
  8. SANITY         return distributions in a believable range

Check 4 is the strongest: if `close_1555 / entry_price - 1` does not reproduce
`target`, the prices and the label came from different places and nothing
downstream can be trusted.

Usage
-----
    python scripts/00_verify_handover.py
    python scripts/00_verify_handover.py --strict     # any warning is a failure
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = [
    "symbol", "date", "bar_time", "index_name",
    "score", "score_pct", "target",
    "entry_price", "exit_price",
]

EXPECTED_EXTRAS = [
    "ibar_atr_pct_14", "or_position", "dist_day_low",
    "ibar_log_dollar_volume", "bar_of_day",
    "breadth_above_open", "spy_above_200sma", "vxx_vol_63",
]

# Plausible ranges. Wide on purpose -- these catch corruption, not nuance.
BOUNDS = {
    "target": (-3.0, 3.0),
    "score_pct": (0.0, 1.0),
    "entry_price": (0.5, 100_000.0),
    "exit_price": (0.5, 100_000.0),
}


class Report:
    def __init__(self, strict: bool):
        self.strict = strict
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        print(f"    [{mark}] {label}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            self.failures.append(f"{label}: {detail}")
        return ok

    def warn(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            print(f"    [PASS] {label}" + (f"  -- {detail}" if detail else ""))
        else:
            print(f"    [WARN] {label}" + (f"  -- {detail}" if detail else ""))
            (self.failures if self.strict else self.warnings).append(
                f"{label}: {detail}")
        return ok


def verify_file(path: Path, spec: dict, rep: Report,
                upstream: Path | None) -> None:
    print(f"\n{'=' * 78}\n{path.name}\n{'=' * 78}")

    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    print(f"  {len(df):,} rows x {df.shape[1]} columns")

    # -- 1. structure ---------------------------------------------------
    print("\n  1. STRUCTURE")
    missing = [c for c in REQUIRED if c not in df.columns]
    rep.check(not missing, "required columns present",
              f"missing {missing}" if missing else "")
    if missing:
        print("     stopping: cannot verify further without these")
        return

    absent = [c for c in EXPECTED_EXTRAS if c not in df.columns]
    rep.warn(not absent, "expected extras present",
             f"missing {absent}" if absent else "")

    # -- 2. identity ----------------------------------------------------
    # The overwrite that cost a rerun would have been caught here: a file
    # named for one period holding another.
    print("\n  2. IDENTITY (does the file match what the manifest claims?)")
    lo, hi = str(df["date"].min().date()), str(df["date"].max().date())
    exp_lo, exp_hi = spec.get("date_range", [None, None])
    rep.check(exp_lo is None or (lo == exp_lo and hi == exp_hi),
              "date range matches manifest",
              f"file {lo}..{hi}, manifest {exp_lo}..{exp_hi}")

    exp_rows = spec.get("rows")
    rep.check(exp_rows is None or len(df) == exp_rows,
              "row count matches manifest",
              f"file {len(df):,}, manifest {exp_rows:,}"
              if exp_rows else "")

    name_years = {int(t) for t in path.stem.split("_") if t.isdigit()
                  and 2000 < int(t) < 2100}
    file_years = set(df["date"].dt.year.unique())
    if name_years:
        rep.check(name_years <= file_years,
                  "filename years present in the data",
                  f"name says {sorted(name_years)}, "
                  f"file holds {sorted(file_years)}")

    # -- 3. keys --------------------------------------------------------
    print("\n  3. KEYS")
    dupes = df.duplicated(["symbol", "date", "bar_time"]).sum()
    rep.check(dupes == 0, "no duplicate (symbol, date, bar_time)",
              f"{dupes:,} duplicates" if dupes else "")

    nulls = df[["symbol", "date", "bar_time"]].isna().sum().sum()
    rep.check(nulls == 0, "no null keys", f"{nulls:,} nulls" if nulls else "")

    # -- 4. prices, and does the target recompute? -----------------------
    print("\n  4. PRICES  (the strongest check)")
    for col in ("entry_price", "exit_price"):
        n_null = int(df[col].isna().sum())
        rep.warn(n_null / len(df) < 0.01, f"{col} mostly present",
                 f"{100*n_null/len(df):.2f}% null")
        pos = df[col].dropna()
        lo_b, hi_b = BOUNDS[col]
        bad = int(((pos < lo_b) | (pos > hi_b)).sum())
        rep.check(bad == 0, f"{col} within [{lo_b}, {hi_b}]",
                  f"{bad:,} outside" if bad else "")

    # target should equal exit/entry - 1. If it does not, the prices and the
    # label came from different sources and nothing downstream is meaningful.
    d = df.dropna(subset=["entry_price", "exit_price", "target"])
    if len(d):
        recomputed = d["exit_price"] / d["entry_price"] - 1.0
        err = (recomputed - d["target"]).abs()
        max_err = float(err.max())
        share_bad = float((err > 1e-6).mean())
        rep.check(share_bad < 0.001,
                  "target recomputes from entry_price and exit_price",
                  f"{100*share_bad:.3f}% mismatch, max error {max_err:.2e}")
    else:
        rep.check(False, "target recomputes", "no rows with both prices")

    # -- 5. signal ------------------------------------------------------
    print("\n  5. SIGNAL")
    sp = df["score_pct"].dropna()
    rep.check(sp.min() >= 0 and sp.max() <= 1, "score_pct within [0, 1]",
              f"[{sp.min():.4f}, {sp.max():.4f}]")

    # Within a cross-section the percentile should span nearly the full range.
    sec = df.groupby(["date", "bar_time"], observed=True)["score_pct"]
    spans = (sec.max() - sec.min()).dropna()
    rep.warn(float(spans.median()) > 0.9,
             "score_pct spans its cross-section",
             f"median span {spans.median():.3f}")

    n_score_null = int(df["score"].isna().sum())
    rep.check(n_score_null == 0, "no null scores",
              f"{n_score_null:,} null" if n_score_null else "")

    # -- 6. grid --------------------------------------------------------
    print("\n  6. GRID")
    bars = sorted(df["bar_time"].astype(str).unique())
    exp_bars = spec.get("bar_times")
    rep.check(exp_bars is None or bars == sorted(exp_bars),
              "bar times match manifest",
              f"{len(bars)} bars {bars[0]}..{bars[-1]}")

    sizes = df.groupby(["date", "bar_time"], observed=True).size()
    med = float(sizes.median())
    rep.warn(med > 100, "cross-sections plausibly sized",
             f"median {med:.0f} names")

    per_day = df.groupby("date", observed=True)["bar_time"].nunique()
    rep.warn(float(per_day.std()) < 1.0, "bar count stable across days",
             f"mean {per_day.mean():.1f}, sd {per_day.std():.2f}")

    # -- 7. reconcile against upstream ----------------------------------
    if upstream is not None:
        print("\n  7. RECONCILE AGAINST UPSTREAM SOURCE")
        src = upstream / spec.get("source", "")
        if src.exists():
            s = pd.read_parquet(src, columns=["date"])
            s["date"] = pd.to_datetime(s["date"])
            rep.check(len(s) == len(df), "row count matches source",
                      f"handover {len(df):,}, source {len(s):,}")
            rep.check(str(s["date"].min().date()) == lo
                      and str(s["date"].max().date()) == hi,
                      "date range matches source",
                      f"source {s['date'].min().date()}..{s['date'].max().date()}")
        else:
            rep.warn(False, "source file reachable", f"{src} not found")

    # -- 8. sanity ------------------------------------------------------
    print("\n  8. SANITY")
    t = df["target"].dropna()
    lo_b, hi_b = BOUNDS["target"]
    bad = int(((t < lo_b) | (t > hi_b)).sum())
    rep.check(bad == 0, f"target within [{lo_b}, {hi_b}]",
              f"{bad:,} outside" if bad else "")

    print(f"       mean {1e4*t.mean():+.2f} bps, sd {1e4*t.std():.0f} bps")
    # Russell intraday drift ran -2.2 to -2.9 bps across development and
    # +3.8 in validation. Far outside that is worth a look.
    rep.warn(abs(1e4 * t.mean()) < 20, "mean target in a believable range",
             f"{1e4*t.mean():+.2f} bps")

    # Does the ranking actually order the outcome? A near-zero IC here would
    # mean the scores and targets are misaligned.
    smp = df.dropna(subset=["score", "target"]).sample(
        min(200_000, len(df)), random_state=17)
    ic = smp.groupby(["date", "bar_time"], observed=True).apply(
        lambda g: g["score"].corr(g["target"], method="spearman")
        if len(g) > 20 else np.nan, include_groups=False).dropna()
    if len(ic):
        rep.warn(float(ic.mean()) > 0.005,
                 "scores rank the outcome (sampled IC)",
                 f"IC {ic.mean():+.4f} over {len(ic):,} sections")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handover", type=Path,
                    default=Path("../two-stage-intraday/handover"))
    ap.add_argument("--upstream", type=Path,
                    default=Path("../two-stage-intraday"))
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures")
    args = ap.parse_args()

    pd.set_option("display.width", 200)

    print("=" * 78)
    print("HANDOVER VERIFICATION")
    print("=" * 78)
    print(f"  handover: {args.handover.resolve()}")

    man_path = args.handover / "MANIFEST.json"
    if not man_path.exists():
        sys.exit(f"No manifest at {man_path}. The handover is not "
                 "self-describing without it; rebuild with scripts/17 "
                 "upstream.")
    manifest = json.loads(man_path.read_text())

    print(f"  created: {manifest.get('created')}")
    print(f"  model:   {manifest['model']['universe']}")
    print(f"           {manifest['model']['training']}")
    print(f"           deploy on {manifest['model']['deploy_on']}")
    spent = manifest.get("spent_looks", {})
    print(f"  spent:   validation stage2 x{spent.get('validation_stage2', '?')}, "
          f"stage1 x{spent.get('validation_stage1', '?')}, "
          f"holdout x{spent.get('holdout', '?')}")

    rep = Report(args.strict)
    files = manifest.get("files", {})
    if not files:
        sys.exit("Manifest lists no files.")

    for name, spec in files.items():
        p = args.handover / spec["file"]
        if not p.exists():
            rep.check(False, f"{spec['file']} exists", "not found")
            continue
        verify_file(p, spec, rep, args.upstream)

    # -- summary --------------------------------------------------------
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    if rep.failures:
        print(f"  {len(rep.failures)} FAILURE(S):")
        for f in rep.failures:
            print(f"    - {f}")
    if rep.warnings:
        print(f"\n  {len(rep.warnings)} warning(s):")
        for w in rep.warnings:
            print(f"    - {w}")

    if not rep.failures and not rep.warnings:
        print("  All checks passed.")
    elif not rep.failures:
        print("\n  No failures. Warnings are worth reading before proceeding.")

    print("\n  Cautions from the manifest:")
    for c in manifest.get("cautions", []):
        print(f"    - {c}")

    if rep.failures:
        print("\n  >>> Do not run analysis on this data until the failures are")
        print("  >>> resolved. Four times upstream, a bad file produced")
        print("  >>> plausible numbers that were acted on.")
        sys.exit(1)


if __name__ == "__main__":
    main()
