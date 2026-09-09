#!/usr/bin/env python3
"""
scripts/01_check_volume_adjustment.py

ONE QUESTION: was split adjustment applied to volume as well as to price?

Why it matters
--------------
Amendments §12.1 downgrades split risk because stage 2's target and features
are within-session, so a between-session adjustment cannot reach them. That
argument is correct for a DISCONTINUITY. It does not cover a RESCALING, which
is present inside every session on the wrong side of the split date.

Almost every sizing column is scale-free and cancels *k*:

    ibar_atr_pct_14        ATR / price
    ibar_parkinson_12      log(high/low)
    ibar_vol_12            returns
    or_position, pos_in_day_range, dist_day_high/low   positions in a range
    ibar_cum_volume_share  share of the symbol's own day

One is not:

    ibar_log_dollar_volume = log(price x volume)

It cancels only if volume was divided by *k* when price was multiplied by it.

The arithmetic
--------------
Forward 2:1 split. Raw price halves, raw volume doubles, so RAW dollar volume
is already continuous across the split. Adjusting price alone breaks that:

    both adjusted:   (p/2) x (2v) = pv          continuous
    price only:      (p/2) x  v   = pv/2        STEP of exactly k

Adjusted price is continuous either way, so price cannot locate the split date.
The only signature is a persistent multiplicative step in dollar volume -- and
under correct adjustment there should be NO such steps at all.

Direction of the error, which is why this is worth twenty minutes: §12.1's
failure mode is an UNAPPLIED split, making a name look cheap and illiquid so it
is wrongly EXCLUDED -- omission, which weakens. This one is the reverse. Dollar
volume inflated by *k* makes a distressed micro-cap look like the most liquid
name in the universe, so it is wrongly INCLUDED. §6 records f_liquidity
excluding 25.71% of names on its own, so that filter does real work. Inclusion
is the direction that flatters, and it would land in the tail, where the edge
lives.

Prior: this probably comes back clean. A vendor implementing adjustsplits=true
almost certainly adjusts both series. The check is cheap and the failure would
be invisible, which is the whole argument for running it.

Three tests
-----------
T1  Step detection. Per symbol, daily median log dollar volume. Find the
    largest persistent level shift (trailing vs leading window medians) and
    compare it to that symbol's own day-to-day noise. Report the implied
    ratios and whether they cluster on clean split factors.

T2  Cumulative-k cross-check. Adjusted price level directly reveals cumulative
    *k*: a name at $1.4M adjusted in 2020 trading near $1 today has k ~ 1e6.
    Under the bug, dollar volume is inflated by the same factor, so adjusted
    price and dollar volume rise together ACROSS symbols for a mechanical
    reason rather than an economic one.

T3  Absolute sanity. Implied daily dollar volume in actual dollars. A Russell
    small cap trading billions per bar is not a liquidity fact.

Reads the handover only -- no bar tree, no scores joined to targets, no looks.

Usage
-----
    python scripts/01_check_volume_adjustment.py
    python scripts/01_check_volume_adjustment.py --file development_2023_2024
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

# Clean split ratios. If steps exist and cluster here, they are adjustment.
# If they exist and do not, they are something else worth understanding.
CLEAN_RATIOS = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100, 200, 1000]
CLEAN_TOL = 0.06          # 6% -- generous, since we take window medians
WINDOW = 20               # sessions each side of a candidate break
MIN_DAYS = 120            # symbols shorter than this are not testable
STEP_RATIO_MIN = 2.5      # smallest step worth calling a step
NOISE_MULTIPLE = 6.0      # step must exceed this many times the symbol's own noise


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def detect_log_base(median_value: float) -> tuple[str, float]:
    """ibar_log_dollar_volume's base is not documented in the MANIFEST.

    A Russell name trading ~$1M/bar gives ln ~ 13.8 or log10 ~ 6.0. The gap is
    wide enough to infer, but this IS an inference and the script says so.
    """
    if median_value > 10:
        return "ln", float(np.e)
    return "log10", 10.0


def largest_persistent_step(s: np.ndarray, window: int) -> tuple[float, int, float]:
    """Return (step, index, noise) for the largest persistent level shift.

    step  = median(after) - median(before), in the series' own log units
    noise = median |day-over-day change|, the symbol's own scale of wobble
    """
    n = len(s)
    noise = float(np.median(np.abs(np.diff(s)))) if n > 1 else 0.0
    best_step, best_i = 0.0, -1
    for i in range(window, n - window):
        before = np.median(s[i - window:i])
        after = np.median(s[i:i + window])
        step = after - before
        if abs(step) > abs(best_step):
            best_step, best_i = float(step), i
    return best_step, best_i, noise


def nearest_clean(ratio: float) -> tuple[float | None, float]:
    r = ratio if ratio >= 1 else 1.0 / ratio
    best, best_err = None, np.inf
    for c in CLEAN_RATIOS:
        err = abs(r - c) / c
        if err < best_err:
            best, best_err = c, err
    return (best, best_err) if best_err <= CLEAN_TOL else (None, best_err)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="development_2020_2022",
                    help="handover key. Default is 2020-2022, which holds the "
                         "extreme adjusted prices.")
    ap.add_argument("--outdir", type=Path, default=None)
    args = ap.parse_args()

    P = load_paths()
    manifest = json.loads(Path(P["handover"]["manifest"]).read_text())
    if args.file not in manifest["files"]:
        print(f"FATAL: unknown file {args.file!r}. "
              f"Choose from {list(manifest['files'])}", file=sys.stderr)
        return 2

    path = Path(P["handover"]["dir"]) / manifest["files"][args.file]["file"]
    outdir = args.outdir or Path(P["outputs"]["artifacts"]) / "01_volume_adjustment"

    print(f"reading {path.name} ...", file=sys.stderr)
    df = pq.read_table(
        path, columns=["symbol", "date", "entry_price", "ibar_log_dollar_volume"]
    ).to_pandas()
    df["date"] = pd.to_datetime(df["date"])

    base_name, base = detect_log_base(float(df["ibar_log_dollar_volume"].median()))
    step_min_log = np.log(STEP_RATIO_MIN) / np.log(base)
    print(f"ibar_log_dollar_volume median {df['ibar_log_dollar_volume'].median():.2f} "
          f"-> inferred base {base_name}", file=sys.stderr)

    # --- collapse to one observation per symbol-day ------------------------
    daily = (df.groupby(["symbol", "date"], observed=True)
               .agg(log_dv=("ibar_log_dollar_volume", "median"),
                    price=("entry_price", "median"))
               .reset_index()
               .sort_values(["symbol", "date"]))
    del df

    # --- T1: step detection -----------------------------------------------
    print("T1 scanning for persistent steps ...", file=sys.stderr)
    rows = []
    for i, (sym, g) in enumerate(daily.groupby("symbol", observed=True), 1):
        if i % 250 == 0:
            print(f"  ... {i}", file=sys.stderr)
        s = g["log_dv"].to_numpy()
        if len(s) < MIN_DAYS or not np.isfinite(s).all():
            continue
        step, idx, noise = largest_persistent_step(s, WINDOW)
        ratio = float(base ** abs(step))
        clean, clean_err = nearest_clean(ratio)
        rows.append({
            "symbol": sym,
            "n_days": len(s),
            "step_log": step,
            "step_ratio": ratio,
            "step_date": g["date"].iloc[idx].date().isoformat() if idx >= 0 else None,
            "noise_log": noise,
            "step_over_noise": abs(step) / noise if noise > 0 else np.inf,
            "nearest_clean_ratio": clean,
            "clean_rel_err": clean_err,
            "max_price": float(g["price"].max()),
            "min_price": float(g["price"].min()),
            "median_log_dv": float(np.median(s)),
        })

    steps = pd.DataFrame(rows)
    flagged = steps[(steps.step_log.abs() > step_min_log)
                    & (steps.step_over_noise > NOISE_MULTIPLE)].copy()
    flagged = flagged.sort_values("step_ratio", ascending=False)
    n_clean = int(flagged.nearest_clean_ratio.notna().sum())

    # --- T2: cumulative-k cross-check --------------------------------------
    # price_span = max/min adjusted price for a symbol. Under the bug, symbols
    # with huge spans carry huge cumulative k, and their dollar volume is
    # inflated by the same factor.
    steps["price_span"] = steps.max_price / steps.min_price.replace(0, np.nan)
    valid = steps.dropna(subset=["price_span", "median_log_dv"])
    rho = float(valid["price_span"].rank().corr(valid["median_log_dv"].rank())) \
        if len(valid) > 2 else float("nan")
    extreme = valid.nlargest(15, "max_price")[
        ["symbol", "max_price", "min_price", "price_span", "median_log_dv",
         "step_ratio", "step_date"]].copy()
    extreme["dv_pctile"] = [
        float((valid.median_log_dv < v).mean()) for v in extreme.median_log_dv]

    # --- T3: absolute sanity ------------------------------------------------
    implied_dollars = base ** valid["median_log_dv"]
    t3 = {
        "median_implied_dollar_volume_per_bar": float(implied_dollars.median()),
        "p99_implied": float(implied_dollars.quantile(0.99)),
        "max_implied": float(implied_dollars.max()),
        "max_implied_symbol": str(valid.loc[valid.median_log_dv.idxmax(), "symbol"]),
    }

    verdict = (
        "CLEAN -- no persistent multiplicative steps at clean split ratios"
        if n_clean == 0 else
        f"SUSPECT -- {n_clean} symbol(s) step by a clean split ratio"
    )

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "command": " ".join(sys.argv),
        "file": args.file,
        "source_path": str(path),
        "inferred_log_base": base_name,
        "inferred_log_base_note":
            "NOT documented in MANIFEST.json. Inferred from the median value. "
            "If wrong, every ratio below is wrong.",
        "params": {"window": WINDOW, "min_days": MIN_DAYS,
                   "step_ratio_min": STEP_RATIO_MIN,
                   "noise_multiple": NOISE_MULTIPLE, "clean_tol": CLEAN_TOL},
        "symbols_tested": int(len(steps)),
        "symbols_flagged_any_step": int(len(flagged)),
        "symbols_flagged_at_clean_ratio": n_clean,
        "T2_spearman_price_span_vs_dollar_volume": rho,
        "T3_absolute": t3,
        "verdict": verdict,
    }

    # --- write BEFORE printing ---------------------------------------------
    outdir.mkdir(parents=True, exist_ok=True)
    steps.to_csv(outdir / "per_symbol_steps.csv", index=False)
    flagged.to_csv(outdir / "flagged.csv", index=False)
    extreme.to_csv(outdir / "extreme_price_symbols.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # --- report -------------------------------------------------------------
    print("\n" + "=" * 74)
    print(f"VOLUME ADJUSTMENT CHECK -- {args.file}")
    print("=" * 74)
    print(json.dumps(summary, indent=2, default=str))

    print(f"\n--- T1: symbols with a persistent step > {STEP_RATIO_MIN}x "
          f"and > {NOISE_MULTIPLE}x their own noise ---")
    if len(flagged):
        print(flagged.head(25)[
            ["symbol", "step_ratio", "step_date", "step_over_noise",
             "nearest_clean_ratio", "max_price", "min_price"]
        ].to_string(index=False))
    else:
        print("  none")

    print("\n--- T2: the 15 highest adjusted prices ---")
    print("  dv_pctile is where that symbol's dollar volume sits in the universe.")
    print("  Under correct adjustment these should be scattered. Under the bug")
    print("  they cluster near 1.0, because inflation is proportional to k.")
    print(extreme.to_string(index=False))

    print(f"\n  Spearman(price_span, median_log_dv) = {rho:+.4f}")
    print("  Near zero is expected. Strongly positive is the bug's signature.")

    print(f"\nwritten to {outdir.resolve()}")
    print(f"\nVERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
