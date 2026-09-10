#!/usr/bin/env python3
"""
scripts/13_holdout_prereq.py -- can the holdout actually be scored?

Two things gate the Phase 5 feature build, and both are currently ASSUMPTIONS
(amendments §12):

1. **The `raw/` end date of 2026-08-25 is inherited, never measured.** It comes
   from upstream §12.5. Our own inventory scan walked `raw_russell` only, so
   the number that actually determines the holdout's end has never been checked
   by this project. A gap here produces a holdout that looks scoreable and is
   not.

2. **The 2026 `no_data` recovery is unverified.** Upstream §5 records a
   spurious no-data bug concentrated in the 2026 pull -- 74% of that year
   against 0.6% elsewhere -- and 99 files recovered. Whether that recovery was
   complete is unknown. **2026 IS the holdout year.**

What this checks
----------------
A. `raw/` -- every context ETF and S&P symbol's last session, and SPY and VXX
   specifically. The regime block (spy_above_200sma, spy_ret_prev, vxx_vol_63,
   vxx_vol_pct, spy_intraday) comes from here. `spy_intraday` is SPY's open to
   the current bar, so SPY must have SAME-DAY data on every scoring session --
   it is the hard constraint, not a lagged one.

B. `raw_russell/` across the holdout -- per-session symbol counts, so the
   effective end date is measured rather than inferred, and the median
   cross-section size. Validation 2025 ran a median of 900 names per bar; a
   holdout running far below that is not the same universe and the ranking
   would be computed over a different population.

C. 2026 recovery -- symbols with normal 2025 coverage and anomalously low 2026
   coverage. A residual no-data failure looks exactly like that. Reported as a
   ratio so it is a distribution, not a threshold.

Read-only. Touches no scores. Spends nothing.

Usage
-----
    python scripts/13_holdout_prereq.py
    python scripts/13_holdout_prereq.py --limit 100     # smoke test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.paths import load_paths  # noqa: E402

ET = "America/New_York"
HOLDOUT_START = date(2025, 9, 1)
HOLDOUT_END_NOMINAL = date(2026, 8, 31)
CLAIMED_RAW_END = date(2026, 8, 25)     # upstream §12.5, to be verified

# Named explicitly because the regime block depends on them.
CRITICAL = ["SPY", "VXX", "IWM"]


def sessions_in(path: Path) -> list[date]:
    try:
        names = set(pq.read_schema(path).names)
    except Exception:  # noqa: BLE001
        return []
    col = "t_start" if "t_start" in names else ("ts_close" if "ts_close" in names else None)
    if col is None:
        return []
    try:
        s = pq.read_table(path, columns=[col]).column(col).to_pandas()
    except Exception:  # noqa: BLE001
        return []
    if s.empty:
        return []
    if col == "t_start":
        ts = pd.to_datetime(s, unit="s", utc=True).dt.tz_convert(ET)
    else:
        ts = pd.to_datetime(s, utc=True).dt.tz_convert(ET) - pd.Timedelta(minutes=5)
    return sorted(set(ts.dt.date))


def scan(root: Path, years: list[int], limit: int | None,
         label: str, always: list[str] | None = None
         ) -> dict[str, dict[int, list[date]]]:
    syms = sorted(d.name for d in root.iterdir() if d.is_dir())
    if limit:
        # `always` symbols are never truncated away: under --limit they sit
        # alphabetically past the cut, the critical check finds nothing, and
        # the result reports a quiet False that reads like a real
        # disagreement. A check must not be able to lose its own inputs.
        keep = set(syms[:limit]) | {a for a in (always or []) if a in set(syms)}
        syms = [x for x in syms if x in keep]
    print(f"{label}: {len(syms)} symbols x {years}", file=sys.stderr)
    out: dict[str, dict[int, list[date]]] = {}
    for i, sym in enumerate(syms, 1):
        if i % 250 == 0:
            print(f"  ... {i}/{len(syms)}", file=sys.stderr)
        per_year = {}
        for y in years:
            f = root / sym / f"5min_{y}.parquet"
            per_year[y] = sessions_in(f) if f.exists() else []
        out[sym] = per_year
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--outdir", type=Path, default=None)
    args = ap.parse_args()

    P = load_paths()
    outdir = args.outdir or Path(P["outputs"]["artifacts"]) / "13_holdout_prereq"

    # ------------------------------------------------------------------- A
    raw_root = Path(P["bars"]["sp500"])
    raw = scan(raw_root, [2026], args.limit, "raw/ (S&P + context ETFs)",
               always=CRITICAL)
    raw_rows = []
    for sym, per in raw.items():
        d = per[2026]
        raw_rows.append({"symbol": sym, "n_sessions_2026": len(d),
                         "last_2026": max(d).isoformat() if d else None,
                         "critical": sym in CRITICAL})
    rawdf = pd.DataFrame(raw_rows)
    rawdf["last_dt"] = pd.to_datetime(rawdf["last_2026"], errors="coerce")
    have_last = rawdf.dropna(subset=["last_dt"])
    measured_raw_end = (have_last["last_dt"].max().date().isoformat()
                        if len(have_last) else None)
    # The binding date is the EARLIEST last-session among symbols that matter.
    crit = rawdf[rawdf.critical].dropna(subset=["last_dt"])
    crit_end = (crit["last_dt"].min().date().isoformat() if len(crit) else None)
    crit_found = sorted(crit["symbol"].tolist())
    crit_missing = sorted(set(CRITICAL) - set(crit_found))

    # ------------------------------------------------------------------- B
    rus_root = Path(P["bars"]["russell"])
    rus = scan(rus_root, [2025, 2026], args.limit, "raw_russell/")

    from collections import Counter
    per_session: Counter = Counter()
    rus_rows = []
    for sym, per in rus.items():
        d25, d26 = per[2025], per[2026]
        allsess = set(d25) | set(d26)
        hold = {x for x in allsess if HOLDOUT_START <= x <= HOLDOUT_END_NOMINAL}
        per_session.update(hold)
        n25 = len([x for x in d25 if x < HOLDOUT_START])
        rus_rows.append({"symbol": sym,
                         "n_2025_pre_holdout": n25,
                         "n_2026": len(d26),
                         "n_in_holdout": len(hold),
                         "last": max(allsess).isoformat() if allsess else None})
    rusdf = pd.DataFrame(rus_rows)

    n_syms = max(len(rusdf), 1)
    cov = (pd.Series(per_session, name="n_symbols").sort_index()
             .rename_axis("date").reset_index())
    cov["coverage"] = cov.n_symbols / n_syms

    def end_at(floor: float):
        ok = cov[cov.coverage >= floor]
        return ok.date.max().isoformat() if len(ok) else None

    # ------------------------------------------------------------------- C
    # A residual no-data failure: normal 2025, anomalously little 2026.
    active = rusdf[rusdf.n_2025_pre_holdout >= 100].copy()
    med26 = float(active.n_2026.median()) if len(active) else np.nan
    active["ratio_2026"] = active.n_2026 / max(med26, 1)
    suspect = active[active.ratio_2026 < 0.5].sort_values("ratio_2026")

    summary = {
        "A_raw_end_date": {
            "claimed_upstream": CLAIMED_RAW_END.isoformat(),
            "measured_max_last_session": measured_raw_end,
            "critical_symbols": CRITICAL,
            "critical_earliest_last_session": crit_end,
            "critical_detail": crit[["symbol", "last_2026", "n_sessions_2026"]]
                               .to_dict("records"),
            "critical_found": crit_found,
            "critical_missing": crit_missing,
            "verdict": ("CANNOT CHECK -- critical symbols absent from the scan: "
                        f"{crit_missing}" if crit_missing else
                        "AGREES with upstream §12.5" if crit_end == CLAIMED_RAW_END.isoformat()
                        else f"DISAGREES -- measured {crit_end}, upstream claims "
                             f"{CLAIMED_RAW_END.isoformat()}"),
            "note": "spy_intraday is SPY open-to-bar, so SPY needs SAME-DAY "
                    "data on every scoring session. The critical minimum is "
                    "the binding constraint, not the max across all symbols.",
        },
        "B_russell_holdout_coverage": {
            "symbols_scanned": int(n_syms),
            "sessions_seen": int(len(cov)),
            "effective_end_100pct": end_at(1.00),
            "effective_end_95pct": end_at(0.95),
            "effective_end_90pct": end_at(0.90),
            "median_symbols_per_session": int(cov.n_symbols.median()) if len(cov) else None,
            "validation_2025_reference": 900,
            "holdout_sessions_per_symbol": {
                "min": int(rusdf.n_in_holdout.min()) if len(rusdf) else None,
                "p05": int(rusdf.n_in_holdout.quantile(0.05)) if len(rusdf) else None,
                "median": int(rusdf.n_in_holdout.median()) if len(rusdf) else None,
                "max": int(rusdf.n_in_holdout.max()) if len(rusdf) else None,
            },
        },
        "C_2026_recovery": {
            "symbols_active_in_2025": int(len(active)),
            "median_2026_sessions": med26,
            "n_below_half_median": int(len(suspect)),
            "frac_below_half_median": round(float(len(suspect) / max(len(active), 1)), 4),
            "n_with_zero_2026": int((active.n_2026 == 0).sum()),
            "note": "A residual no-data failure looks like normal 2025 coverage "
                    "and little or no 2026. Some of these are genuine "
                    "delistings; the ratio is a screen, not a verdict.",
        },
    }

    outdir.mkdir(parents=True, exist_ok=True)
    rawdf.to_csv(outdir / "raw_end_dates.csv", index=False)
    rusdf.to_csv(outdir / "russell_holdout.csv", index=False)
    cov.to_csv(outdir / "session_coverage.csv", index=False)
    suspect.to_csv(outdir / "suspect_2026.csv", index=False)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    pd.set_option("display.width", 200)
    print("\n" + "=" * 88)
    print("HOLDOUT PREREQUISITES")
    print("=" * 88)
    print(json.dumps(summary, indent=2, default=str))

    print("\n--- last 12 holdout sessions, Russell coverage ---")
    tail = cov.tail(12).copy()
    tail["coverage"] = (tail.coverage * 100).round(1).astype(str) + "%"
    print(tail.to_string(index=False))

    print("\n--- raw/: 12 earliest last-sessions (the binding end) ---")
    print(have_last.nsmallest(12, "last_dt")[
        ["symbol", "last_2026", "n_sessions_2026"]].to_string(index=False))

    if len(suspect):
        print(f"\n--- 20 of {len(suspect)} symbols with <50% of median 2026 "
              f"coverage despite normal 2025 ---")
        print(suspect.head(20)[["symbol", "n_2025_pre_holdout", "n_2026",
                                "ratio_2026"]].to_string(index=False))

    print(f"\nwritten to {outdir.resolve()}")
    print(f"\nA: {summary['A_raw_end_date']['verdict']}")
    print(f"   measured critical end: {crit_end}  "
          f"(found {crit_found}, missing {crit_missing})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
