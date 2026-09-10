#!/usr/bin/env python3
"""
src/data/bars.py -- intrabar paths from the raw bar tree.

The handover carries entry_price (open of bar t+1) and exit_price (close of
15:55) and nothing in between. Four deferred pieces of work all need what is
in between:

    stops                   was a level touched, and when
    limit-fill modelling    did the entry bar's low reach the open
    per-trade cost          that bar's own high-low range
    replacement rebalance   a price at an arbitrary bar

This is the shared dependency. Built once, here.

THE NEXT-PRINT RULE (amendments §7.1)
-------------------------------------
4.2% of panel rows have NO bar at t+1. For every one of them, the panel's
entry_price is the open of the NEXT bar that printed -- 731 of 731 matched to
1e-6, median delay 5 minutes, p90 15, max 20.

Any path built here must start at the same bar the panel used. A loader that
assumes a bar exists at t+1 will silently disagree with the panel on 4% of
trades: the stop would be measured from a price the strategy never paid, and
nothing downstream would report a problem.

`validate_against_panel` checks it. Run it whenever the loader is used for
anything that will be believed.

Symbol resolution
-----------------
raw_russell first, then raw. The upstream src/data/paths.py cannot be
inherited: it hardcodes the S&P tree, has no raw_russell branch, and returns a
NON-EXISTENT path silently for any Russell symbol (upstream §6.3). This
returns None and the caller counts it.

Caching
-------
In-memory, keyed by (symbol, year), holding what was actually read from that
path in this process. Upstream §11.1's first incident was a cache keyed only by
FILENAME that served a 50-symbol file to a 503-symbol run and produced three
identical outputs that all looked plausible. This cache cannot do that -- the
key is the identity of the data, not a label for it -- but it is still a cache,
so `clear()` exists and `stats()` reports what it holds.

Usage
-----
    from src.data.bars import BarPaths

    bp = BarPaths()
    paths = bp.paths_for_trades(trades)          # long frame, one row per bar
    report = bp.validate_against_panel(paths, trades)
    assert report["max_rel_err"] < 1e-6
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from src.data.paths import load_paths

ET = "America/New_York"
EXIT_HHMM = "15:55"
FILL_OFFSET_MIN = 5      # bar t+1 is the next FIVE-MINUTE bar (upstream §1)

BAR_COLS = ["t_start", "open", "high", "low", "close", "volume"]


def hhmm_to_min(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


@dataclass
class BarPaths:
    """Loads and caches five-minute bars, and cuts intrabar paths from them."""

    P: dict = field(default_factory=load_paths)
    _cache: dict = field(default_factory=dict, repr=False)
    _missing: set = field(default_factory=set, repr=False)

    # ---------------------------------------------------------------- loading

    def symbol_dir(self, symbol: str) -> tuple[Path | None, str]:
        for root, name in ((self.P["bars"]["russell"], "russell"),
                           (self.P["bars"]["sp500"], "sp500")):
            d = Path(root) / symbol
            if d.is_dir():
                return d, name
        return None, "none"

    def load(self, symbol: str, year: int) -> pd.DataFrame | None:
        key = (symbol, int(year))
        if key in self._cache:
            return self._cache[key]
        if key in self._missing:
            return None

        d, _ = self.symbol_dir(symbol)
        f = None if d is None else d / f"5min_{year}.parquet"
        if f is None or not f.exists():
            self._missing.add(key)
            return None
        try:
            df = pq.read_table(f, columns=BAR_COLS).to_pandas()
        except Exception:  # noqa: BLE001
            self._missing.add(key)
            return None

        # t_start (unix seconds, bar START) is authoritative. Session labels
        # derive from it, not from ts_close, so the spec's wall-clock times map
        # onto stored rows.
        ts = pd.to_datetime(df["t_start"], unit="s", utc=True).dt.tz_convert(ET)
        df["d"] = ts.dt.date
        df["mins"] = ts.dt.hour * 60 + ts.dt.minute
        df["hhmm"] = ts.dt.strftime("%H:%M")
        df = df.sort_values(["d", "mins"], kind="mergesort")
        self._cache[key] = df
        return df

    def clear(self) -> None:
        self._cache.clear()
        self._missing.clear()

    def stats(self) -> dict:
        return {"cached_symbol_years": len(self._cache),
                "missing_symbol_years": len(self._missing),
                "cached_rows": int(sum(len(v) for v in self._cache.values()))}

    # ------------------------------------------------------------------ paths

    def paths_for_trades(self, trades: pd.DataFrame,
                         exit_hhmm: str = EXIT_HHMM,
                         progress: bool = True) -> pd.DataFrame:
        """One row per bar, from the ENTRY bar through the exit bar inclusive.

        The entry bar is the first bar at or after signal_bar + 5 minutes that
        actually printed -- the panel's next-print rule (§7.1), not a blind
        t+1 lookup.

        Columns: trade_id, symbol, date, signal_bar, bar_seq, hhmm, mins,
        open, high, low, close, volume, entry_open, side.
        """
        need = ["symbol", "date", "bar_time", "side", "entry_price"]
        missing = [c for c in need if c not in trades.columns]
        if missing:
            raise ValueError(f"trades is missing {missing}")

        t = trades.reset_index(drop=True).copy()
        t["trade_id"] = t.index
        # NOT underscore-prefixed: itertuples renames any column starting with
        # an underscore to a positional name (_1, _2), so r._date raises
        # AttributeError rather than returning the column.
        t["sess_date"] = pd.to_datetime(t["date"]).dt.date
        t["sess_year"] = pd.to_datetime(t["date"]).dt.year
        t["from_min"] = t["bar_time"].map(hhmm_to_min) + FILL_OFFSET_MIN
        exit_min = hhmm_to_min(exit_hhmm)

        out, n_no_bars, n_no_entry = [], 0, 0
        pairs = t[["symbol", "sess_year"]].drop_duplicates()
        if progress:
            print(f"  {len(t):,} trades across {len(pairs):,} symbol-years",
                  file=sys.stderr)

        for i, (sym, yr) in enumerate(pairs.itertuples(index=False), 1):
            if progress and i % 250 == 0:
                print(f"  ... {i}/{len(pairs)}", file=sys.stderr)
            bars = self.load(sym, int(yr))
            sub = t[(t["symbol"] == sym) & (t["sess_year"] == yr)]
            if bars is None:
                n_no_bars += len(sub)
                continue
            by_day = {d: g for d, g in bars.groupby("d", observed=True)}

            for r in sub.itertuples(index=False):
                day = by_day.get(r.sess_date)
                if day is None:
                    n_no_entry += 1
                    continue
                seg = day[(day["mins"] >= r.from_min) &
                          (day["mins"] <= exit_min) &
                          (day["open"] > 0)]
                if seg.empty:
                    n_no_entry += 1
                    continue
                seg = seg.copy()
                seg["trade_id"] = r.trade_id
                seg["symbol"] = sym
                seg["date"] = r.sess_date
                seg["signal_bar"] = r.bar_time
                seg["side"] = r.side
                seg["bar_seq"] = np.arange(len(seg))
                seg["entry_open"] = float(seg["open"].iloc[0])
                out.append(seg)

        if not out:
            raise RuntimeError("no paths built; check the bar tree and symbols")

        paths = pd.concat(out, ignore_index=True)
        paths.attrs["n_trades_no_bar_file"] = int(n_no_bars)
        paths.attrs["n_trades_no_entry_bar"] = int(n_no_entry)
        paths.attrs["n_trades_with_path"] = int(paths["trade_id"].nunique())
        return paths[["trade_id", "symbol", "date", "signal_bar", "side",
                      "bar_seq", "hhmm", "mins", "open", "high", "low",
                      "close", "volume", "entry_open"]]

    # ------------------------------------------------------------- validation

    def validate_against_panel(self, paths: pd.DataFrame,
                               trades: pd.DataFrame,
                               tol: float = 1e-6) -> dict:
        """Does each path start at the price the panel says we paid?

        This is the check that can fail. If the loader used a blind t+1 lookup
        while the panel used next-print, ~4% of trades would disagree -- and a
        stop measured from a price never paid produces plausible numbers with
        no error anywhere.
        """
        t = trades.reset_index(drop=True).copy()
        t["trade_id"] = t.index
        first = (paths.sort_values(["trade_id", "bar_seq"])
                      .groupby("trade_id", as_index=False).first()
                      [["trade_id", "open", "hhmm"]]
                      .rename(columns={"open": "path_entry_open",
                                       "hhmm": "path_entry_bar"}))
        j = t[["trade_id", "entry_price", "bar_time"]].merge(
            first, on="trade_id", how="inner")
        rel = (j["entry_price"] - j["path_entry_open"]).abs() / j["path_entry_open"]

        delay = (j["path_entry_bar"].map(hhmm_to_min)
                 - j["bar_time"].map(hhmm_to_min) - FILL_OFFSET_MIN)
        return {
            "n_checked": int(len(j)),
            "n_trades": int(len(t)),
            "n_without_path": int(len(t) - len(j)),
            "max_rel_err": float(rel.max()) if len(rel) else float("nan"),
            "n_over_tol": int((rel > tol).sum()),
            "frac_over_tol": round(float((rel > tol).mean()), 6) if len(rel) else None,
            "next_print_used_frac": round(float((delay > 0).mean()), 4),
            "delay_median_min": float(delay.median()),
            "delay_p90_min": float(delay.quantile(0.90)),
            "delay_max_min": int(delay.max()),
            "verdict": ("PASS -- paths start at the panel's entry price"
                        if len(rel) and (rel > tol).mean() < 1e-3 else
                        "FAIL -- paths disagree with the panel. Any stop or "
                        "fill result built on this is measured from a price "
                        "the strategy never paid."),
        }


if __name__ == "__main__":
    import json
    from src.data.handover import load_handover
    from src.portfolio.selection import NEEDED, SelectionConfig, select

    WIDE_AM = ["09:50", "10:10", "10:30", "10:50", "11:10", "11:30"]
    h = load_handover("development_2023_2024", columns=NEEDED)
    cfg = SelectionConfig(k=2, sides="both", bar_times=WIDE_AM,
                          max_positions=20, balance_sides=True)
    trades = select(h, cfg)

    bp = BarPaths()
    paths = bp.paths_for_trades(trades)
    print("\ncache:", json.dumps(bp.stats()))
    print("path build:", json.dumps({k: paths.attrs[k] for k in paths.attrs}))
    print(f"bars per trade: median "
          f"{paths.groupby('trade_id').size().median():.0f}, "
          f"total rows {len(paths):,}")
    print("\nvalidation:")
    print(json.dumps(bp.validate_against_panel(paths, trades), indent=2))
