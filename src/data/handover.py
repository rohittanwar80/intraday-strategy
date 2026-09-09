#!/usr/bin/env python3
"""
src/data/handover.py -- the only way this project reads the handover panel.

Amendments §11.2 rule 1: every loader validates and raises. A function that
reads a file returns verified data or nothing. Four times upstream a stale or
mismatched file produced plausible numbers that were acted on before the
problem was found, and two of those made results look BETTER than the truth.

This is not a re-run of scripts/00_verify_handover.py. That does the full,
expensive verification once. This does the cheap invariants on every load, so a
file that changes underneath the project is caught at the point of use rather
than at the point of surprise.

The spent-window guard
----------------------
validation_2025 is SPENT (upstream §16) and the holdout is FROZEN. Loading
either requires allow_spent=True passed explicitly at the call site. That is
deliberate friction: the failure mode is not someone deciding to spend a look,
it is someone typing a filename without noticing which window it is.

Provenance
----------
Every load returns a Handover carrying the file's sha256, the manifest's
recorded row count, and the columns actually read (§11.6). Attach `.meta` to
anything written to disk so an output can name what produced it.

Usage
-----
    from src.data.handover import load_handover, DEVELOPMENT

    h = load_handover("development_2023_2024")
    h = load_handover("development_2023_2024", columns=["symbol", "date", ...])
    for h in load_all(DEVELOPMENT):        # both development windows, separately
        ...

Files are never concatenated by this module. §8: report per period, never
pooled alone -- the upstream project's headline numbers repeatedly concealed
regime effects that only appeared per fold or per year. Pooling is a decision
the caller makes visibly, not a default the loader hands out.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from src.data.paths import load_paths

DEVELOPMENT = "development"
VALIDATION = "validation"

KEY_COLS = ["symbol", "date", "bar_time", "index_name"]

# Cross-section grouping. The model objective is LambdaRank grouped by
# (date, bar_time, index) -- score and score_pct are comparable only within one
# of these, never across.
SECTION = ["date", "bar_time", "index_name"]

RECOMPUTE_TOL = 1e-6


class HandoverError(RuntimeError):
    """Raised when a load fails validation. Never caught inside this module."""


@dataclass
class Handover:
    """A verified panel plus what produced it."""
    df: pd.DataFrame
    key: str
    phase: str
    meta: dict = field(default_factory=dict)

    @property
    def n_days(self) -> int:
        return int(self.df["date"].nunique())

    @property
    def n_sections(self) -> int:
        cols = [c for c in SECTION if c in self.df.columns]
        return int(self.df.groupby(cols, observed=True).ngroups)

    def sections(self):
        """Iterate cross-sections. The only unit within which ranks compare."""
        cols = [c for c in SECTION if c in self.df.columns]
        return self.df.groupby(cols, observed=True)

    def __repr__(self) -> str:
        return (f"<Handover {self.key} phase={self.phase} "
                f"rows={len(self.df):,} days={self.n_days} "
                f"cols={len(self.df.columns)}>")


def _sha256(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def manifest(P: dict | None = None) -> dict:
    P = P or load_paths()
    return json.loads(Path(P["handover"]["manifest"]).read_text())


def _validate(df: pd.DataFrame, key: str, spec: dict, columns: Sequence[str] | None) -> None:
    """Cheap invariants. Raises HandoverError on the first failure.

    Deliberately does NOT repeat the expensive checks in 00_verify_handover.py
    (full recompute, rank consistency, IC). Those run once. These run always.
    """
    problems: list[str] = []

    # Row count against the manifest. Catches a regenerated or truncated file.
    if len(df) != spec["rows"]:
        problems.append(
            f"row count {len(df):,} but the manifest records {spec['rows']:,}. "
            f"The upstream file changed. Re-run scripts/00_verify_handover.py "
            f"before using it (§11.4)."
        )

    have = set(df.columns)

    if {"date"} <= have:
        d = pd.to_datetime(df["date"])
        lo, hi = d.min().date().isoformat(), d.max().date().isoformat()
        want_lo, want_hi = spec["date_range"]
        if [lo, hi] != [want_lo, want_hi]:
            problems.append(f"date range {lo}..{hi} but the manifest says "
                            f"{want_lo}..{want_hi}")

    keys = [c for c in KEY_COLS if c in have]
    if keys:
        if df[keys].isna().any().any():
            bad = {c: int(df[c].isna().sum()) for c in keys if df[c].isna().any()}
            problems.append(f"null key values: {bad}")
        # Only meaningful when the full key is present.
        if set(KEY_COLS) <= have and df.duplicated(subset=KEY_COLS).any():
            n = int(df.duplicated(subset=KEY_COLS).sum())
            problems.append(f"{n:,} duplicate rows on {KEY_COLS}. A duplicated "
                            f"key means the same bar appears twice and every "
                            f"cross-sectional statistic is weighted wrong.")

    for c in ("entry_price", "exit_price"):
        if c in have:
            if df[c].isna().any() or (df[c] <= 0).any():
                problems.append(
                    f"{c}: {int(df[c].isna().sum()):,} null, "
                    f"{int((df[c] <= 0).sum()):,} non-positive")

    if "score_pct" in have:
        sp = df["score_pct"]
        if sp.isna().any() or (sp < 0).any() or (sp > 1).any():
            problems.append(
                f"score_pct outside [0,1] or null: min {sp.min()}, max {sp.max()}, "
                f"{int(sp.isna().sum()):,} null")

    # The check that matters most (§11.3). Cheap enough to run on every load
    # when all three columns were requested.
    if {"entry_price", "exit_price", "target"} <= have:
        diff = (df["exit_price"] / df["entry_price"] - 1.0 - df["target"]).abs()
        n_bad = int((diff > RECOMPUTE_TOL).sum())
        if n_bad:
            problems.append(
                f"target does not recompute from prices on {n_bad:,} rows "
                f"(max |diff| {float(diff.max()):.3e}). The prices and the label "
                f"came from different places; nothing downstream is meaningful. "
                f"Do not work around this (§11.5).")

    if problems:
        raise HandoverError(f"{key} failed validation:\n  - " + "\n  - ".join(problems))


def load_handover(
    key: str,
    *,
    columns: Sequence[str] | None = None,
    allow_spent: bool = False,
    P: dict | None = None,
) -> Handover:
    """Load one handover file, verified.

    Parameters
    ----------
    key
        A key from MANIFEST.json's `files` block, e.g. "development_2023_2024".
    columns
        Subset to read. Key columns are added automatically. Omitting columns
        also skips the invariants that need them -- the recompute check needs
        entry_price, exit_price and target together.
    allow_spent
        Required to load any non-development window. See the module docstring.
    """
    P = P or load_paths()
    mf = manifest(P)

    if key not in mf["files"]:
        raise HandoverError(
            f"unknown handover key {key!r}. Available: {sorted(mf['files'])}")

    spec = mf["files"][key]
    phase = spec.get("phase", "unknown")

    if phase != DEVELOPMENT and not allow_spent:
        raise HandoverError(
            f"{key!r} is phase={phase!r}, not development.\n"
            f"  validation_2025 is SPENT (1 look, upstream §16) and the holdout "
            f"is FROZEN.\n"
            f"  Loading it requires allow_spent=True at the call site, and any "
            f"look must be recorded in\n"
            f"  {P['upstream_artifacts']['holdout_ledger']}\n"
            f"  If you only need label statistics (drift, dispersion), those "
            f"spend nothing -- but pass the flag deliberately."
        )

    path = Path(P["handover"]["dir"]) / spec["file"]
    if not path.exists():
        raise HandoverError(f"{path} does not exist")

    read_cols = None
    if columns is not None:
        read_cols = list(dict.fromkeys(list(columns) + [c for c in KEY_COLS
                                                        if c in spec["columns"]]))
        missing = [c for c in read_cols if c not in spec["columns"]]
        if missing:
            raise HandoverError(
                f"{key}: requested columns not in the file: {missing}. "
                f"If a column is genuinely needed it is a JOIN against "
                f"{P['upstream_artifacts']['features_stage2_full']}, never a "
                f"model rerun (spec §4).")

    df = pq.read_table(path, columns=read_cols).to_pandas()

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    if "bar_time" in df.columns:
        df["bar_time"] = df["bar_time"].astype(str)

    _validate(df, key, spec, read_cols)

    meta = {
        "key": key,
        "phase": phase,
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "manifest_created": mf.get("created"),
        "manifest_rows": spec["rows"],
        "rows_loaded": int(len(df)),
        "date_range": spec["date_range"],
        "columns_read": list(df.columns),
        "spent_looks": mf.get("spent_looks"),
    }
    return Handover(df=df, key=key, phase=phase, meta=meta)


def development_keys(P: dict | None = None) -> list[str]:
    mf = manifest(P)
    return [k for k, v in mf["files"].items() if v.get("phase") == DEVELOPMENT]


def load_all(
    phase: str = DEVELOPMENT,
    *,
    columns: Sequence[str] | None = None,
    allow_spent: bool = False,
    P: dict | None = None,
) -> Iterator[Handover]:
    """Yield each file of a phase SEPARATELY, never concatenated (§8)."""
    P = P or load_paths()
    mf = manifest(P)
    for key, spec in mf["files"].items():
        if spec.get("phase") == phase:
            yield load_handover(key, columns=columns, allow_spent=allow_spent, P=P)


# ---------------------------------------------------------------------------
# Selection helper used by everything downstream
# ---------------------------------------------------------------------------

def rank_in_section(df: pd.DataFrame, ascending: bool = False) -> pd.Series:
    """Dense rank of `score` within each cross-section. 1 = best long.

    Needed because score_pct cannot express a cut finer than 1/n on the short
    side (amendments §7.6): it is rank/n spanning (0, 1], so its minimum is
    ~0.0011 and `score_pct <= 0.001` selects nothing while `>= 0.999` works.
    Any bottom cut finer than 1/n must use this, not a threshold.
    """
    cols = [c for c in SECTION if c in df.columns]
    return (df.groupby(cols, observed=True)["score"]
              .rank(ascending=ascending, method="first"))


def section_size(df: pd.DataFrame) -> pd.Series:
    cols = [c for c in SECTION if c in df.columns]
    return df.groupby(cols, observed=True)["score"].transform("size")


if __name__ == "__main__":
    # Smoke test: load both development files and print what came back.
    for h in load_all(DEVELOPMENT):
        print(h)
        print(f"  sections {h.n_sections:,}  sha {h.meta['sha256'][:12]}")
    try:
        load_handover("validation_2025")
    except HandoverError as exc:
        print("\nspent-window guard works:\n", exc)
