#!/usr/bin/env python3
"""
scripts/00_verify_handover.py -- runs before any analysis. Nothing else first.

Amendments §11.1: four times upstream, a stale or mismatched file produced
plausible numbers that were acted on before the problem was found. Two of the
four made results look BETTER than the truth, which is the direction that does
not prompt suspicion. Every one was detectable from the data itself.

Eight check groups:

    G1 structure          files exist, readable, columns match the manifest
    G2 identity           row counts, date ranges, bar grids, names/bar
    G3 keys               no duplicate or null (symbol, date, bar_time, index)
    G4 prices + RECOMPUTE the check that matters most (§11.3)
    G5 signal             score_pct in [0,1], spans its cross-section, ranks score
    G6 grid               stride-4 bar times, no 09:35, sessions are weekdays
    G7 reconciliation     totals against MANIFEST.json
    G8 distribution       target moments; IC on DEVELOPMENT ONLY

§11.3 is the one to read first. If

    target == exit_price / entry_price - 1

does not hold, the prices and the label came from different places and nothing
downstream is meaningful -- sizing, P&L, cost impact all rest on those two
prices being the ones the label was built from. This script tests it on every
row of all 17.9M, not a sample.

THE LOOK BOUNDARY
-----------------
validation_2025.parquet is SPENT (amendments §16). This script therefore
computes NO score-versus-target statistic on it. The line drawn:

  * statistics of `target` ALONE spend nothing -- they are properties of the
    labels, not of the model. §17 item 2 says so explicitly about drift and
    dispersion.
  * any statistic JOINING `score` to `target` is an evaluation, and on
    validation that is a second look.

So G8 reports target moments for all three files and IC for development only.

Usage
-----
    python scripts/00_verify_handover.py
    python scripts/00_verify_handover.py --strict     # warnings become failures
    python scripts/00_verify_handover.py --no-hash    # skip sha256 (faster)

Exit codes: 0 ok, 1 checks failed, 2 could not run.

Peak memory is roughly 2 GB on the largest file; columns are read in subsets
rather than loading the panel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data.paths import load_paths  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

KEY_COLS = ["symbol", "date", "bar_time", "index_name"]
RECOMPUTE_TIGHT = 1e-9    # float64 round-trip; anything above is suspicious
RECOMPUTE_LOOSE = 1e-6    # beyond this the label did not come from these prices
LOOSE_FRAC_FAIL = 1e-4    # >0.01% of rows past the loose bound -> FAIL

# Universe intraday drift recorded upstream (amendments §16, spec §3).
# Development periods all negative; validation flipped positive. These are
# reconciliation targets, not thresholds -- a mismatch means we are reading a
# different file than the one those numbers came from.
EXPECTED_DRIFT_BPS = {
    "development_2020_2022": (-4.0, -1.0),
    "development_2023_2024": (-4.0, -1.0),
    "validation_2025": (2.0, 6.0),
}
EXPECTED_IC = {  # development only; validation IC is not computed here
    "development_2020_2022": 0.0413,
    "development_2023_2024": 0.0344,
}


@dataclass
class Check:
    group: str
    file: str
    name: str
    status: str
    detail: str


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, group: str, file: str, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(group, file, name, status, detail))
        if status != PASS:
            print(f"    {status}  {name}: {detail}", file=sys.stderr)

    def ok(self, group, file, name, detail=""):
        self.add(group, file, name, PASS, detail)

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, WARN: 0, FAIL: 0}
        for c in self.checks:
            out[c.status] += 1
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def as_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.date if not np.issubdtype(s.dtype, np.datetime64) \
        else s.dt.date


# ---------------------------------------------------------------------------
# check groups
# ---------------------------------------------------------------------------

def g1_structure(rep: Report, tag: str, path: Path, spec: dict) -> set[str] | None:
    if not path.exists():
        rep.add("G1", tag, "file_exists", FAIL, f"{path} not found")
        return None
    try:
        schema = pq.read_schema(path)
    except Exception as exc:  # noqa: BLE001
        rep.add("G1", tag, "readable", FAIL, str(exc))
        return None

    have = set(schema.names)
    want = set(spec["columns"])
    missing, extra = want - have, have - want

    if missing:
        rep.add("G1", tag, "columns_present", FAIL,
                f"manifest lists {len(missing)} column(s) not in the file: {sorted(missing)}")
    else:
        rep.ok("G1", tag, "columns_present", f"{len(want)} manifest columns all present")

    if extra:
        rep.add("G1", tag, "columns_extra", WARN,
                f"file has {len(extra)} column(s) the manifest does not list: {sorted(extra)}")

    # The model groups by (date, bar_time, index). Spec §4 lists index_name as a
    # key column. If it is absent the cross-section is not what the spec says.
    if "index_name" not in have:
        rep.add("G1", tag, "index_name_present", FAIL,
                "no index_name column, but the objective is LambdaRank grouped by "
                "(date, bar_time, index) and spec §4 lists it as a key")
    return have


def g2_identity(rep: Report, tag: str, path: Path, spec: dict, have: set[str]) -> None:
    n_rows = pq.ParquetFile(path).metadata.num_rows
    if n_rows == spec["rows"]:
        rep.ok("G2", tag, "row_count", f"{n_rows:,}")
    else:
        rep.add("G2", tag, "row_count", FAIL,
                f"file has {n_rows:,}, manifest says {spec['rows']:,} "
                f"(difference {n_rows - spec['rows']:+,})")

    cols = [c for c in ("date", "bar_time") if c in have]
    df = pq.read_table(path, columns=cols).to_pandas()

    d = as_date_series(df["date"])
    lo, hi = d.min().isoformat(), d.max().isoformat()
    want_lo, want_hi = spec["date_range"]
    if [lo, hi] == [want_lo, want_hi]:
        rep.ok("G2", tag, "date_range", f"{lo} .. {hi}")
    else:
        rep.add("G2", tag, "date_range", FAIL,
                f"file spans {lo} .. {hi}, manifest says {want_lo} .. {want_hi}")

    n_days = d.nunique()
    if n_days == spec["n_days"]:
        rep.ok("G2", tag, "n_days", str(n_days))
    else:
        rep.add("G2", tag, "n_days", FAIL,
                f"file has {n_days} sessions, manifest says {spec['n_days']}")

    have_bars = sorted(df["bar_time"].astype(str).unique())
    want_bars = sorted(spec["bar_times"])
    if have_bars == want_bars:
        rep.ok("G2", tag, "bar_times", f"{len(have_bars)} bars")
    else:
        rep.add("G2", tag, "bar_times", FAIL,
                f"grid mismatch. only in file: {sorted(set(have_bars) - set(want_bars))}; "
                f"only in manifest: {sorted(set(want_bars) - set(have_bars))}")

    per_bar = df.groupby(["date", "bar_time"], observed=True).size()
    med = int(per_bar.median())
    want_med = spec["median_names_per_bar"]
    if med == want_med:
        rep.ok("G2", tag, "median_names_per_bar", str(med))
    else:
        rep.add("G2", tag, "median_names_per_bar", WARN,
                f"file median {med}, manifest says {want_med}")


def g3_keys(rep: Report, tag: str, path: Path, have: set[str]) -> None:
    cols = [c for c in KEY_COLS if c in have]
    df = pq.read_table(path, columns=cols).to_pandas()

    nulls = {c: int(df[c].isna().sum()) for c in cols}
    bad = {c: n for c, n in nulls.items() if n}
    if bad:
        rep.add("G3", tag, "no_null_keys", FAIL, f"null key values: {bad}")
    else:
        rep.ok("G3", tag, "no_null_keys")

    n_dup = int(df.duplicated(subset=cols).sum())
    if n_dup == 0:
        rep.ok("G3", tag, "no_duplicate_keys", f"on {cols}")
    else:
        rep.add("G3", tag, "no_duplicate_keys", FAIL,
                f"{n_dup:,} duplicate rows on {cols}. A duplicated key means the "
                f"same bar appears twice and every cross-sectional statistic is "
                f"weighted wrong.")
    del df


def g4_prices_recompute(rep: Report, tag: str, path: Path, have: set[str]) -> None:
    """§11.3 -- the single strongest integrity check available, and it is cheap."""
    need = ["entry_price", "exit_price", "target"]
    if not set(need) <= have:
        rep.add("G4", tag, "recompute_target", FAIL,
                f"missing {sorted(set(need) - have)}")
        return

    df = pq.read_table(path, columns=need).to_pandas()

    for c in ("entry_price", "exit_price"):
        n_null = int(df[c].isna().sum())
        n_nonpos = int((df[c] <= 0).sum())
        if n_null or n_nonpos:
            rep.add("G4", tag, f"{c}_valid", FAIL,
                    f"{n_null:,} null, {n_nonpos:,} non-positive")
        else:
            rep.ok("G4", tag, f"{c}_valid",
                   f"min {df[c].min():.4f}, max {df[c].max():.2f}")

    n_null_t = int(df["target"].isna().sum())
    if n_null_t:
        rep.add("G4", tag, "target_not_null", WARN, f"{n_null_t:,} null targets")

    recomputed = df["exit_price"] / df["entry_price"] - 1.0
    diff = (recomputed - df["target"]).abs()
    valid = diff.notna()
    n = int(valid.sum())

    n_tight = int((diff > RECOMPUTE_TIGHT).sum())
    n_loose = int((diff > RECOMPUTE_LOOSE).sum())
    frac_loose = n_loose / max(n, 1)
    max_diff = float(diff.max()) if n else float("nan")

    detail = (f"n={n:,}  max|diff|={max_diff:.3e}  "
              f">{RECOMPUTE_TIGHT:.0e}: {n_tight:,}  "
              f">{RECOMPUTE_LOOSE:.0e}: {n_loose:,} ({frac_loose:.6%})")

    if frac_loose > LOOSE_FRAC_FAIL:
        rep.add("G4", tag, "recompute_target", FAIL,
                detail + "  -- the label did not come from these prices. "
                         "STOP. Do not work around this (§11.5).")
    elif n_loose:
        rep.add("G4", tag, "recompute_target", WARN,
                detail + "  -- a small subset disagrees; find out which rows before "
                         "trusting P&L on them")
    elif n_tight:
        rep.add("G4", tag, "recompute_target", WARN,
                detail + "  -- within 1e-6 but above float64 round-trip")
    else:
        rep.ok("G4", tag, "recompute_target", detail)
    del df


def g5_signal(rep: Report, tag: str, path: Path, have: set[str], n_groups: int) -> None:
    need = ["score_pct", "score", "date", "bar_time"]
    if not set(need) <= have:
        rep.add("G5", tag, "score_pct_range", FAIL, f"missing {sorted(set(need) - have)}")
        return

    cols = need + (["index_name"] if "index_name" in have else [])
    df = pq.read_table(path, columns=cols).to_pandas()

    sp = df["score_pct"]
    n_out = int(((sp < 0) | (sp > 1)).sum())
    n_null = int(sp.isna().sum())
    if n_out or n_null:
        rep.add("G5", tag, "score_pct_range", FAIL,
                f"{n_out:,} outside [0,1], {n_null:,} null")
    else:
        rep.ok("G5", tag, "score_pct_range", f"[{sp.min():.4f}, {sp.max():.4f}]")

    gcols = [c for c in ("date", "bar_time", "index_name") if c in df.columns]
    spans = df.groupby(gcols, observed=True)["score_pct"].agg(["min", "max"])
    bad_span = int(((spans["max"] - spans["min"]) < 0.5).sum())
    if bad_span:
        rep.add("G5", tag, "score_pct_spans_section", WARN,
                f"{bad_span:,} of {len(spans):,} cross-sections span <0.5 of [0,1]. "
                f"A percentile that does not span its section was computed over the "
                f"wrong group.")
    else:
        rep.ok("G5", tag, "score_pct_spans_section", f"{len(spans):,} sections")

    # score_pct must be the within-section rank of score. Sampled: a full
    # groupby-rank over 17.9M rows is expensive and a systematic break shows up
    # in any sample.
    rng = np.random.default_rng(0)
    keys = spans.index.to_frame(index=False)
    take = keys.iloc[rng.choice(len(keys), size=min(n_groups, len(keys)), replace=False)]
    sample = df.merge(take, on=gcols, how="inner")
    corrs = (sample.groupby(gcols, observed=True)[["score", "score_pct"]]
                   .apply(lambda g: g["score"].corr(g["score_pct"], method="spearman")))
    worst = float(corrs.min()) if len(corrs) else float("nan")
    if len(corrs) and worst > 0.999:
        rep.ok("G5", tag, "score_pct_ranks_score",
               f"{len(corrs)} sampled sections, min rank-corr {worst:.6f}")
    else:
        rep.add("G5", tag, "score_pct_ranks_score", FAIL,
                f"min rank-corr {worst:.6f} over {len(corrs)} sampled sections. "
                f"score_pct is not the within-section rank of score.")
    del df, sample


def g6_grid(rep: Report, tag: str, path: Path, have: set[str]) -> None:
    df = pq.read_table(path, columns=["date", "bar_time"]).to_pandas()
    bars = sorted(df["bar_time"].astype(str).unique())

    if "09:35" in bars:
        rep.add("G6", tag, "no_0935_bar", FAIL,
                "09:35 is present, but amendments §12.6 says a stride-4 grid "
                "anchored at 09:30 never samples it. Either the grid changed or "
                "this is not the file we think it is.")
    else:
        rep.ok("G6", tag, "no_0935_bar", f"earliest bar {bars[0]}")

    mins = [int(b[:2]) * 60 + int(b[3:]) for b in bars]
    gaps = sorted(set(np.diff(sorted(mins))))
    if gaps == [20]:
        rep.ok("G6", tag, "stride_4_grid", "uniform 20-minute spacing")
    else:
        rep.add("G6", tag, "stride_4_grid", WARN,
                f"spacings present: {gaps} minutes, expected only [20]")

    d = pd.to_datetime(pd.Series(sorted(as_date_series(df["date"]).unique())))
    n_weekend = int((d.dt.dayofweek >= 5).sum())
    if n_weekend:
        rep.add("G6", tag, "sessions_are_weekdays", FAIL,
                f"{n_weekend} weekend session(s): "
                f"{[x.date().isoformat() for x in d[d.dt.dayofweek >= 5][:5]]}")
    else:
        rep.ok("G6", tag, "sessions_are_weekdays", f"{len(d)} sessions")
    del df


def g8_distribution(rep: Report, tag: str, path: Path, have: set[str],
                    spend_looks_ok: bool) -> None:
    """Target moments for every file. IC for development only -- see the look
    boundary in the module docstring."""
    df = pq.read_table(path, columns=["target", "date"]).to_pandas()
    t = df["target"].dropna()

    mean_bps = float(t.mean() * 1e4)
    sd_bps = float(t.std() * 1e4)
    lo, hi = EXPECTED_DRIFT_BPS.get(tag, (-np.inf, np.inf))
    detail = f"mean {mean_bps:+.2f} bps, sd {sd_bps:.1f} bps, n={len(t):,}"
    if lo <= mean_bps <= hi:
        rep.ok("G8", tag, "universe_drift", detail + f"  (expected {lo:+g}..{hi:+g})")
    else:
        rep.add("G8", tag, "universe_drift", WARN,
                detail + f"  -- outside the recorded range {lo:+g}..{hi:+g} bps. "
                         f"Upstream recorded development negative and validation "
                         f"+3.84. A mismatch means a different file.")

    n_extreme = int((t.abs() > 1.0).sum())
    if n_extreme:
        rep.add("G8", tag, "no_absurd_returns", WARN,
                f"{n_extreme:,} intraday returns beyond +/-100%")
    else:
        rep.ok("G8", tag, "no_absurd_returns")

    if not spend_looks_ok:
        rep.ok("G8", tag, "ic_skipped",
               "validation is SPENT -- no score-versus-target statistic computed "
               "here (§16). Target moments above use labels only and spend nothing.")
        del df
        return

    sc = pq.read_table(path, columns=["score_pct"]).to_pandas()["score_pct"]
    sub = pd.DataFrame({"date": df["date"], "s": sc, "t": df["target"]}).dropna()
    # Aggregate to the DAY before saying anything about significance: bars within
    # a day share market moves (spec §8).
    daily = sub.groupby("date").apply(
        lambda g: g["s"].corr(g["t"], method="spearman"), include_groups=False)
    ic = float(daily.mean())
    want = EXPECTED_IC.get(tag)
    detail = f"mean daily rank IC {ic:+.4f} over {len(daily)} days (manifest {want})"
    if want is None:
        rep.ok("G8", tag, "sampled_ic", detail)
    elif abs(ic - want) < 0.010:
        rep.ok("G8", tag, "sampled_ic", detail)
    else:
        rep.add("G8", tag, "sampled_ic", WARN,
                detail + " -- daily-mean IC differs from the pooled figure the "
                         "manifest records; they are not the same estimator, but a "
                         "large gap means scores and targets may be misaligned.")
    del df, sub


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="warnings become failures")
    ap.add_argument("--no-hash", action="store_true", help="skip sha256 of inputs")
    ap.add_argument("--sample-groups", type=int, default=2000,
                    help="cross-sections sampled for the score_pct rank check")
    ap.add_argument("--outdir", type=Path, default=None)
    args = ap.parse_args()

    try:
        P = load_paths()
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    manifest_path = Path(P["handover"]["manifest"])
    manifest = json.loads(manifest_path.read_text())
    outdir = args.outdir or Path(P["outputs"]["artifacts"]) / "00_verify"

    rep = Report()
    provenance = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "git_commit": git_commit(),
        "manifest": str(manifest_path),
        "manifest_created": manifest.get("created"),
        "inputs": {},
    }

    for tag, spec in manifest["files"].items():
        path = Path(P["handover"]["dir"]) / spec["file"]
        is_validation = spec.get("phase") == "validation"
        print(f"\n=== {tag}  ({'VALIDATION - SPENT' if is_validation else 'development'})",
              file=sys.stderr)

        have = g1_structure(rep, tag, path, spec)
        if have is None:
            continue

        provenance["inputs"][tag] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": None if args.no_hash else sha256(path),
        }

        for label, fn in (
            ("G2 identity", lambda: g2_identity(rep, tag, path, spec, have)),
            ("G3 keys", lambda: g3_keys(rep, tag, path, have)),
            ("G4 prices + recompute", lambda: g4_prices_recompute(rep, tag, path, have)),
            ("G5 signal", lambda: g5_signal(rep, tag, path, have, args.sample_groups)),
            ("G6 grid", lambda: g6_grid(rep, tag, path, have)),
            ("G8 distribution", lambda: g8_distribution(rep, tag, path, have,
                                                        spend_looks_ok=not is_validation)),
        ):
            print(f"  {label} ...", file=sys.stderr)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                rep.add(label.split()[0], tag, "check_raised", FAIL, repr(exc))

    # G7: totals across files
    total_manifest = sum(s["rows"] for s in manifest["files"].values())
    total_actual = sum(
        pq.ParquetFile(Path(P["handover"]["dir"]) / s["file"]).metadata.num_rows
        for s in manifest["files"].values()
        if (Path(P["handover"]["dir"]) / s["file"]).exists()
    )
    if total_actual == total_manifest:
        rep.ok("G7", "ALL", "total_rows", f"{total_actual:,}")
    else:
        rep.add("G7", "ALL", "total_rows", FAIL,
                f"{total_actual:,} on disk against {total_manifest:,} in the manifest")

    for name, want in (("validation_stage2", 1), ("validation_stage1", 1), ("holdout", 0)):
        got = manifest["spent_looks"].get(name)
        if got == want:
            rep.ok("G7", "ALL", f"spent_looks.{name}", str(got))
        else:
            rep.add("G7", "ALL", f"spent_looks.{name}", FAIL,
                    f"manifest says {got}, expected {want}. If a look was genuinely "
                    f"taken, update this project's spec too.")

    # --- write BEFORE printing (§13: a 69-minute build was lost this way) ---
    counts = rep.counts()
    failed = counts[FAIL] > 0 or (args.strict and counts[WARN] > 0)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "verify_report.json").write_text(json.dumps({
        "provenance": provenance,
        "strict": args.strict,
        "counts": counts,
        "result": "FAILED" if failed else "ok",
        "checks": [asdict(c) for c in rep.checks],
    }, indent=2))
    pd.DataFrame([asdict(c) for c in rep.checks]).to_csv(
        outdir / "verify_checks.csv", index=False)

    # --- report ---
    print("\n" + "=" * 74)
    print("HANDOVER VERIFICATION")
    print("=" * 74)
    df = pd.DataFrame([asdict(c) for c in rep.checks])
    for tag, sub in df.groupby("file", sort=False):
        print(f"\n{tag}")
        for _, r in sub.iterrows():
            mark = {PASS: "  ok ", WARN: " WARN", FAIL: " FAIL"}[r.status]
            print(f"  {mark}  {r.group}  {r['name']:<28} {r.detail}")

    print(f"\n{counts[PASS]} passed, {counts[WARN]} warnings, {counts[FAIL]} failures")
    print(f"written to {outdir.resolve()}")

    if failed:
        print("\nRESULT: FAILED. Do not proceed to analysis.")
        print("§11.5: do not work around this. Find what produced the bad file,")
        print("fix the producer, regenerate, re-verify.")
        return 1

    print("\nRESULT: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
