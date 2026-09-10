#!/usr/bin/env python3
"""
src/data/paths.py -- the only place that reads config/paths.yaml.

Nothing else in this project should contain an absolute path. Spec §10: the
upstream venv lives in a third repository and that has already broken once.

Two jobs:

1. Resolve ``${roots.x}`` interpolation, which is not native YAML.
2. Validate. Every required path must exist, and two invariants must hold:
     - common_end_date == min(panel_end_dates)
     - holdout.effective_end == common_end_date
   Both are checks that can FAIL. If someone later edits one date without the
   other, this says so instead of silently scoring a window that has no
   context-ETF data behind it.

Run it directly to check the config:

    python src/data/paths.py

Exits non-zero if anything is wrong. Import it everywhere else:

    from src.data.paths import load_paths
    P = load_paths()
    df = pd.read_parquet(P["handover"]["files"]["development_2023_2024"])
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "paths.yaml"

_INTERP = re.compile(r"\$\{([a-zA-Z0-9_.]+)\}")
_MAX_DEPTH = 10

# Paths that must exist for Phases 0-4. Dotted keys into the resolved tree.
REQUIRED = [
    "roots.project",
    "roots.upstream",
    "roots.data",
    "handover.dir",
    "handover.manifest",
    "handover.files.development_2020_2022",
    "handover.files.development_2023_2024",
    "handover.files.validation_2025",
    "bars.russell",
    "bars.sp500",
    "bars.sessions",
]

# Needed only for specific later work. Missing is reported, not fatal.
OPTIONAL = [
    "upstream_artifacts.holdout_ledger",   # written on any validation/holdout look
    "upstream_artifacts.features_stage2_full",  # column joins; 2020-2025 only
    "upstream_artifacts.labels_stage2",    # the deprioritised 1.33x work
    "upstream_artifacts.src",              # Phase 5 holdout rerun ONLY
    "bars.adjusted",                       # 16 symbols, S&P only (§12.1)
    "bars.universe_russell",
]


def _dig(tree: dict, dotted: str) -> Any:
    cur: Any = tree
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"paths.yaml has no key '{dotted}' (failed at '{part}')")
        cur = cur[part]
    return cur


def _resolve(tree: dict) -> dict:
    """Expand ${a.b} references against the tree itself, repeatedly."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            for _ in range(_MAX_DEPTH):
                if not _INTERP.search(node):
                    return node
                node = _INTERP.sub(lambda m: str(_dig(tree, m.group(1))), node)
            raise ValueError(f"unresolved interpolation after {_MAX_DEPTH} passes: {node!r}")
        return node

    return walk(tree)


def _as_date(v: Any, label: str) -> date:
    """PyYAML parses unquoted 2026-08-25 as a date. Quoted, it stays a string."""
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"{label}: cannot parse {v!r} as a date") from exc
    raise TypeError(f"{label}: expected a date, got {type(v).__name__}")


def check_invariants(P: dict) -> list[str]:
    """Return a list of failure messages. Empty means everything holds."""
    problems: list[str] = []

    ends = {k: _as_date(v, f"panel_end_dates.{k}")
            for k, v in P["panel_end_dates"].items()}
    common = _as_date(P["common_end_date"], "common_end_date")

    # Only the panels this strategy actually consumes bind the end date. The
    # S&P names stop a day earlier than the context ETFs and are irrelevant to
    # a Russell strategy -- checking against every panel would cost a session
    # for no reason. Naming the binding set forces that judgement to be
    # explicit rather than implied by a min() over everything.
    binding = P.get("binding_panels")
    if not binding:
        problems.append(
            "binding_panels is missing. It must name which entries of "
            "panel_end_dates constrain common_end_date -- without it the "
            "check silently reverts to 'earliest of everything', which is "
            "wrong whenever a panel is present but unused.")
    else:
        unknown = [b for b in binding if b not in ends]
        if unknown:
            problems.append(f"binding_panels names entries absent from "
                            f"panel_end_dates: {unknown}")
        else:
            sub = {k: ends[k] for k in binding}
            earliest_key = min(sub, key=lambda k: sub[k])
            earliest = sub[earliest_key]
            if common != earliest:
                problems.append(
                    f"common_end_date is {common}, but the earliest BINDING "
                    f"panel end date is {earliest} ({earliest_key}); binding "
                    f"panels are {binding}. A bar with Russell prices and no "
                    f"context-ETF data is not a scoreable bar."
                )

    h = P["windows"]["holdout"]
    eff_end = _as_date(h["effective_end"], "holdout.effective_end")
    eff_start = _as_date(h["effective_start"], "holdout.effective_start")

    if eff_end != common:
        problems.append(
            f"holdout.effective_end is {eff_end} but common_end_date is {common}. "
            f"These must agree."
        )
    if eff_start >= eff_end:
        problems.append(f"holdout window is empty or inverted: {eff_start} .. {eff_end}")
    if h.get("status") != "FROZEN":
        problems.append(f"holdout.status is {h.get('status')!r}, expected 'FROZEN'")
    if h.get("looks_spent", 0) != 0:
        problems.append(
            f"holdout.looks_spent is {h.get('looks_spent')}. If a look has genuinely "
            f"been taken, this is correct and the ledger should agree. If not, "
            f"something edited it."
        )
    return problems


def load_paths(config_path: Path | str = DEFAULT_CONFIG, *, validate: bool = True) -> dict:
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"no config at {config_path}")

    raw = yaml.safe_load(config_path.read_text())
    P = _resolve(raw)

    if validate:
        problems = check_invariants(P)
        missing = [k for k in REQUIRED if not Path(_dig(P, k)).exists()]
        if missing:
            problems.append("required paths do not exist: " + ", ".join(missing))
        if problems:
            raise RuntimeError(
                "config/paths.yaml failed validation:\n  - " + "\n  - ".join(problems)
            )
    return P


def main() -> int:
    cfg = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    print(f"config: {cfg}")

    try:
        P = load_paths(cfg, validate=False)
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED to load: {exc}")
        return 2

    print("\n--- paths ---")
    bad_required = []
    for key in REQUIRED + OPTIONAL:
        try:
            p = Path(_dig(P, key))
        except KeyError as exc:
            print(f"  MISSING KEY  {key}  ({exc})")
            if key in REQUIRED:
                bad_required.append(key)
            continue
        ok = p.exists()
        tag = "ok     " if ok else ("ABSENT " if key in OPTIONAL else "MISSING")
        print(f"  {tag}  {key:<45} {p}")
        if not ok and key in REQUIRED:
            bad_required.append(key)

    print("\n--- invariants ---")
    problems = check_invariants(P)
    if problems:
        for p in problems:
            print(f"  FAIL  {p}")
    else:
        ends = {k: _as_date(v, k) for k, v in P["panel_end_dates"].items()}
        binding = P.get("binding_panels", [])
        sub = {k: ends[k] for k in binding if k in ends}
        h = P["windows"]["holdout"]
        print(f"  ok    common_end_date {P['common_end_date']} == earliest of "
              f"binding panels {binding} "
              f"({min(sub, key=lambda k: sub[k]) if sub else '?'})")
        print(f"  ok    holdout {h['effective_start']} .. {h['effective_end']}, "
              f"{h['status']}, {h['looks_spent']} looks spent")

    if bad_required or problems:
        print("\nRESULT: FAILED. Fix config/paths.yaml before running anything else.")
        return 1

    print("\nRESULT: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
