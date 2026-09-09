#!/usr/bin/env python3
"""
scripts/02_tail_tradeability.py

Spec §12 task 2, first half: are the tail picks tradeable?

Amendments §12.4 hands this question to this project and records it as
unmeasured. There is no quote data in either project, so spread cannot be
measured. But one component of it can be COMPUTED exactly, with no quotes at
all, and it is the one that binds hardest on small caps:

    minimum round-trip cost >= one tick = (0.01 / price) x 10,000 bps

A $2.50 stock cannot trade tighter than 40 bps round trip. A $50 stock cannot
trade tighter than 2 bps. This is a floor, not an estimate -- the true cost is
worse, because real markets on thin names are many ticks wide.

Against that floor: the 1% cut realises +46.49 bps and the 0.1% cut +92.17.
If the tail concentrates in low-priced names, the floor alone can eat the edge,
and no amount of execution skill recovers it.

What this measures, per selectivity cut
---------------------------------------
    names/bar            realised, against the spec's ~1 / ~4 / ~8 / ~38
    entry_price          p10 / median / p90, and the share under $5 / $2 / $1
    min_roundtrip_bps    (0.01 / price) x 1e4 -- the tick floor
    dollar volume/bar    from ibar_log_dollar_volume (base inferred, ln)
    ibar_atr_pct_14      typical bar range, a proxy for how far a fill can slip
    edge                 mean target, day-aggregated before the t-stat (§8)
    edge_net_of_tick     edge minus the tick floor. Can be negative.
    capacity             dollars per name per bar at a participation cap

Also a per-bar-time table for the 1% cut, because the edge is ~20x larger at
10:30 than 15:30 while liquidity is U-shaped through the session. A cut that
looks tradeable pooled may be tradeable only in the afternoon, when there is
no edge left to collect.

Scope
-----
DEVELOPMENT ONLY. validation_2025 is spent (§16); this script joins score to
target, which is an evaluation, so it does not touch it.

Files are reported SEPARATELY, never pooled. §8: the upstream project's
headline numbers repeatedly concealed regime effects that only appeared per
fold or per year.

What this CANNOT answer
-----------------------
  * actual spread. No quotes exist. The tick floor is a lower bound only.
  * print availability at the entry bar. Every row in the handover has a
    non-null entry_price by construction (verified: G4, zero nulls), so the
    10.1%-of-days-with-no-09:35-print figure is about days that never entered
    the panel. Measuring it needs the eligibility universe and the bar tree.
  * intrabar path. Entry and exit only.

Usage
-----
    python scripts/02_tail_tradeability.py
    python scripts/02_tail_tradeability.py --participation 0.02
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

TICK = 0.01  # US equities above $1. Sub-dollar names quote finer; flagged below.

# (label, side, threshold on score_pct). score_pct is ALREADY the within-section
# percentile, so no groupby rank is needed -- selection is a threshold.
CUTS = [
    ("bottom 0.1%", "short", 0.001),
    ("bottom 0.5%", "short", 0.005),
    ("bottom 1%",   "short", 0.010),
    ("bottom 5%",   "short", 0.050),
    ("universe",    "none",  None),
    ("top 5%",      "long",  0.950),
    ("top 1%",      "long",  0.990),
    ("top 0.5%",    "long",  0.995),
    ("top 0.1%",    "long",  0.999),
]

COLS = ["date", "bar_time", "index_name", "score_pct", "target",
        "entry_price", "ibar_log_dollar_volume", "ibar_atr_pct_14",
        "ibar_parkinson_12"]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def select(df: pd.DataFrame, side: str, thr: float | None) -> pd.DataFrame:
    if side == "none":
        return df
    return df[df.score_pct <= thr] if side == "short" else df[df.score_pct >= thr]


def day_t(g: pd.DataFrame) -> tuple[float, float, int]:
    """Mean target in bps and its t-stat, aggregated to the DAY first.

    §8: bars within a day share market moves, so a bar-level t-stat overstates
    significance by roughly sqrt(bars per day).
    """
    daily = g.groupby("date", observed=True)["target"].mean() * 1e4
    n = len(daily)
    if n < 2:
        return float(daily.mean()) if n else np.nan, np.nan, n
    t = float(daily.mean() / (daily.std(ddof=1) / np.sqrt(n)))
    return float(daily.mean()), t, n


def profile(g: pd.DataFrame, n_bars: int, participation: float, base: float) -> dict:
    if g.empty:
        return {}
    price = g["entry_price"]
    tick_bps = (TICK / price) * 1e4
    dv = base ** g["ibar_log_dollar_volume"]
    edge, t, n_days = day_t(g)
    med_tick = float(tick_bps.median())

    # Cost is paid on the absolute move; the short leg earns a negative target.
    gross = abs(edge)

    return {
        "n_obs": int(len(g)),
        "names_per_bar": round(len(g) / n_bars, 2),
        "price_p10": round(float(price.quantile(0.10)), 2),
        "price_med": round(float(price.median()), 2),
        "price_p90": round(float(price.quantile(0.90)), 2),
        "pct_under_5": round(float((price < 5).mean()) * 100, 1),
        "pct_under_2": round(float((price < 2).mean()) * 100, 1),
        "pct_under_1": round(float((price < 1).mean()) * 100, 1),
        "tick_bps_med": round(med_tick, 1),
        "tick_bps_p90": round(float(tick_bps.quantile(0.90)), 1),
        "dollar_vol_med": int(dv.median()),
        "dollar_vol_p10": int(dv.quantile(0.10)),
        "atr_pct_bps_med": round(float(g["ibar_atr_pct_14"].median()) * 1e4, 1),
        "parkinson_med": round(float(g["ibar_parkinson_12"].median()), 6),
        "edge_bps": round(edge, 2),
        "edge_t": round(t, 2),
        "n_days": n_days,
        "edge_net_of_tick": round(gross - med_tick, 2),
        "capacity_per_name_bar": int(dv.median() * participation),
    }


def run_file(tag: str, path: Path, participation: float) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    print(f"reading {path.name} ...", file=sys.stderr)
    df = pq.read_table(path, columns=COLS).to_pandas()
    df = df.dropna(subset=["score_pct", "target", "entry_price"])

    med_log = float(df["ibar_log_dollar_volume"].median())
    base = np.e if med_log > 10 else 10.0
    base_name = "ln" if base > 3 else "log10"

    n_bars = int(df.groupby(["date", "bar_time"], observed=True).ngroups)

    rows = []
    for label, side, thr in CUTS:
        prof = profile(select(df, side, thr), n_bars, participation, base)
        if prof:
            rows.append({"cut": label, **prof})
    by_cut = pd.DataFrame(rows)

    # per bar_time, 1% cut both sides
    bt_rows = []
    for label, side, thr in [("top 1%", "long", 0.990), ("bottom 1%", "short", 0.010)]:
        sub = select(df, side, thr)
        for bt, g in sub.groupby("bar_time", observed=True):
            nb = int(df[df.bar_time == bt].groupby("date", observed=True).ngroups)
            p = profile(g, max(nb, 1), participation, base)
            bt_rows.append({"cut": label, "bar_time": str(bt), **p})
    by_bar = pd.DataFrame(bt_rows).sort_values(["cut", "bar_time"])

    meta = {"log_base": base_name, "n_cross_sections": n_bars,
            "n_rows": int(len(df)), "n_days": int(df.date.nunique())}
    del df
    return by_cut, by_bar, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--participation", type=float, default=0.01,
                    help="share of a bar's dollar volume assumed takeable (default 1%%)")
    ap.add_argument("--outdir", type=Path, default=None)
    args = ap.parse_args()

    P = load_paths()
    manifest = json.loads(Path(P["handover"]["manifest"]).read_text())
    outdir = args.outdir or Path(P["outputs"]["artifacts"]) / "02_tail_tradeability"

    dev = {k: v for k, v in manifest["files"].items() if v.get("phase") == "development"}
    if not dev:
        print("FATAL: no development files in the manifest", file=sys.stderr)
        return 2

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "command": " ".join(sys.argv),
        "participation": args.participation,
        "tick_assumed": TICK,
        "scope": "development only; validation_2025 is spent (§16) and is not read",
        "files": {},
    }
    all_cuts, all_bars = [], []

    for tag, spec in dev.items():
        path = Path(P["handover"]["dir"]) / spec["file"]
        by_cut, by_bar, meta = run_file(tag, path, args.participation)
        by_cut.insert(0, "file", tag)
        by_bar.insert(0, "file", tag)
        all_cuts.append(by_cut)
        all_bars.append(by_bar)
        summary["files"][tag] = meta

    cuts = pd.concat(all_cuts, ignore_index=True)
    bars = pd.concat(all_bars, ignore_index=True)

    # --- write BEFORE printing ---
    outdir.mkdir(parents=True, exist_ok=True)
    cuts.to_csv(outdir / "by_cut.csv", index=False)
    bars.to_csv(outdir / "by_bar_time.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 200)
    print("\n" + "=" * 100)
    print("TAIL TRADEABILITY -- development only, files reported separately")
    print("=" * 100)
    print(json.dumps(summary, indent=2, default=str))

    show = ["cut", "names_per_bar", "price_p10", "price_med", "pct_under_5",
            "tick_bps_med", "tick_bps_p90", "dollar_vol_med", "atr_pct_bps_med",
            "edge_bps", "edge_t", "edge_net_of_tick", "capacity_per_name_bar"]
    for tag, sub in cuts.groupby("file", sort=False):
        print(f"\n--- {tag} ---")
        print(sub[show].to_string(index=False))

    print("\n--- 1% cuts by bar time ---")
    bshow = ["file", "cut", "bar_time", "names_per_bar", "price_med",
             "tick_bps_med", "dollar_vol_med", "edge_bps", "edge_t",
             "edge_net_of_tick"]
    print(bars[bshow].to_string(index=False))

    print("\nReading notes:")
    print("  tick_bps_med is the FLOOR on round-trip cost, not an estimate of it.")
    print("  A real market on a thin small cap is many ticks wide.")
    print("  edge_net_of_tick uses |edge|, so the short leg is charged the same.")
    print("  Negative edge_net_of_tick means the cut cannot pay its own minimum")
    print("  spread even in the best case.")
    print(f"\nwritten to {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
