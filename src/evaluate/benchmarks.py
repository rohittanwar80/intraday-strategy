#!/usr/bin/env python3
"""
src/evaluate/benchmarks.py -- what the strategy must beat (spec §7).

    Buy and hold IWM      the honest passive alternative
    Random selection      same count and timing -- isolates the RANKING
    Bottom tranche        if the model ranks, this should lose symmetrically
    ORB rules-only        -60.4% over five years, Sharpe -1.25 (upstream)

Spec §7: "Random and bottom are the discriminating ones. Model-versus-buy-and-
hold cannot distinguish a strategy that ranks from one that happens to hold a
favourable subset."

That distinction is the whole point here. The universe applies CURRENT Russell
membership to all dates (§6.3), so every name in the panel survived to today.
A long book of extreme small-cap movers drawn from a survivor-only universe
will make money whether or not the model ranks. Random selection is the control
that separates the two, and it is the only one of these benchmarks that can.

How random is constructed
-------------------------
`score` is replaced with uniform noise and the SAME selection pipeline runs --
same cut, same bars, same cap, same dedupe, same costs. Identical machinery,
so any difference is the ranking and nothing else.

One confound to watch, and it is reported rather than hidden: random names do
not persist across bars the way top-ranked names do, so `unique_symbols` culls
fewer of them and random fills MORE slots. Higher utilisation means more
capital deployed, which affects annualised return and Sharpe for a reason that
has nothing to do with ranking. So the comparison table shows utilisation for
every row, and `per_trade_bps` alongside, which is count-invariant.

Read the per-trade column when asking "does the ranking work". Read Sharpe when
asking "is this book worth running".

Random is run over many seeds. A single draw is a coin flip; the distribution
is the benchmark. The strategy's percentile within it is the actual test.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from src.backtest.engine import TRADING_DAYS, run_backtest
from src.data.handover import Handover
from src.data.paths import load_paths
from src.execution.costs import CostConfig
from src.portfolio.selection import SelectionConfig

# Upstream reference. A plausible, well-specified rules-only strategy that
# LOSES money. Spec §7: any result here should be read against this floor.
ORB_REFERENCE = {"benchmark": "ORB rules-only (upstream)", "total_pct": -60.4,
                 "sharpe": -1.25, "note": "5 years, negative even at zero spread"}

ET = "America/New_York"


def _with_scores(h: Handover, scores: np.ndarray) -> Handover:
    return Handover(df=h.df.assign(score=scores), key=h.key, phase=h.phase,
                    meta=h.meta)


def random_benchmark(h: Handover, sel_cfg: SelectionConfig,
                     cost_cfg: CostConfig, n_seeds: int = 20) -> pd.DataFrame:
    """Same pipeline, random scores. One row per seed."""
    rows = []
    for seed in range(n_seeds):
        rng = np.random.default_rng(seed)
        hr = _with_scores(h, rng.random(len(h.df)))
        try:
            res = run_backtest(hr, sel_cfg, cost_cfg)
        except ValueError as exc:
            # The timing guard can fire on random when it did not on the real
            # scores, because dedupe culls fewer names. That is informative.
            rows.append({"seed": seed, "error": str(exc)[:90]})
            continue
        m = res.metrics
        rows.append({
            "seed": seed,
            "sharpe": m["sharpe"],
            "ann_pct": m["ann_return_pct"],
            "ann_vol_pct": m["ann_vol_pct"],
            "per_trade_bps": m["per_trade_bps_net"],
            "n_trades": m["n_trades"],
            "utilisation": m["mean_utilisation"],
            "total_pct": m["total_return_pct"],
            "max_dd_pct": m["max_drawdown_pct"],
        })
    return pd.DataFrame(rows)


def bottom_tranche(h: Handover, sel_cfg: SelectionConfig,
                   cost_cfg: CostConfig):
    """Go LONG the worst-ranked names. Negating score inverts the ranking, so
    the existing long path selects the bottom.

    Distinct from sides='short', which SHORTS the bottom and should make money.
    This buys the bottom and should lose roughly symmetrically. If it does not,
    the ranking is one-sided.
    """
    hb = _with_scores(h, -h.df["score"].to_numpy())
    cfg = replace(sel_cfg, sides="long", balance_sides=False)
    return run_backtest(hb, cfg, cost_cfg)


def iwm_buy_hold(h: Handover, P: dict | None = None) -> dict:
    """Passive IWM over the same sessions.

    Two figures, because they answer different questions:
      close-to-close  true buy and hold, carries overnight risk
      intraday only   open to 15:55, the window this strategy actually occupies
    """
    P = P or load_paths()
    dates = set(pd.to_datetime(h.df["date"]).dt.date.unique())
    years = sorted({d.year for d in dates})

    frames = []
    for root in (P["bars"]["sp500"], P["bars"]["russell"]):
        d = Path(root) / "IWM"
        if not d.is_dir():
            continue
        for y in years:
            f = d / f"5min_{y}.parquet"
            if f.exists():
                frames.append(pq.read_table(
                    f, columns=["t_start", "open", "close"]).to_pandas())
        break

    if not frames:
        return {"benchmark": "IWM buy and hold",
                "error": "IWM not found in either bar tree; cannot compute"}

    bars = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(bars["t_start"], unit="s", utc=True).dt.tz_convert(ET)
    bars["d"] = ts.dt.date
    bars["hhmm"] = ts.dt.strftime("%H:%M")
    bars = bars[bars["d"].isin(dates)]
    if bars.empty:
        return {"benchmark": "IWM buy and hold", "error": "no overlapping sessions"}

    day = bars.sort_values(["d", "hhmm"]).groupby("d").agg(
        open=("open", "first"), close=("close", "last"))
    exit_bar = bars[bars["hhmm"] == "15:55"].set_index("d")["close"]

    c2c = day["close"].pct_change().dropna()
    intraday = ((exit_bar / day["open"]) - 1).dropna()

    def pack(r: pd.Series, label: str) -> dict:
        sd = float(r.std(ddof=1))
        return {
            "benchmark": label,
            "n_sessions": int(len(r)),
            "total_pct": round(float(r.sum()) * 100, 2),
            "ann_pct": round(float(r.mean()) * TRADING_DAYS * 100, 2),
            "ann_vol_pct": round(sd * np.sqrt(TRADING_DAYS) * 100, 2),
            "sharpe": round(float(r.mean()) / sd * np.sqrt(TRADING_DAYS), 3)
            if sd > 0 else None,
            "max_dd_pct": round(float((r.cumsum() - r.cumsum().cummax()).min()) * 100, 2),
        }

    return {"close_to_close": pack(c2c, "IWM buy and hold (close to close)"),
            "intraday": pack(intraday, "IWM intraday (open to 15:55)")}


def compare(h: Handover, sel_cfg: SelectionConfig, cost_cfg: CostConfig,
            n_seeds: int = 20) -> tuple[pd.DataFrame, dict]:
    """Strategy against every benchmark. Returns (table, extras)."""
    strat = run_backtest(h, sel_cfg, cost_cfg)
    m = strat.metrics

    rows = [{
        "benchmark": f"STRATEGY ({sel_cfg.sides})",
        "sharpe": m["sharpe"], "ann_pct": m["ann_return_pct"],
        "ann_vol_pct": m["ann_vol_pct"], "per_trade_bps": m["per_trade_bps_net"],
        "n_trades": m["n_trades"], "utilisation": m["mean_utilisation"],
        "total_pct": m["total_return_pct"], "max_dd_pct": m["max_drawdown_pct"],
    }]

    rnd = random_benchmark(h, sel_cfg, cost_cfg, n_seeds)
    ok = rnd[rnd.get("error").isna()] if "error" in rnd.columns else rnd
    if len(ok):
        rows.append({
            "benchmark": f"random (mean of {len(ok)})",
            "sharpe": round(float(ok.sharpe.mean()), 3),
            "ann_pct": round(float(ok.ann_pct.mean()), 2),
            "ann_vol_pct": round(float(ok.ann_vol_pct.mean()), 2),
            "per_trade_bps": round(float(ok.per_trade_bps.mean()), 2),
            "n_trades": int(ok.n_trades.mean()),
            "utilisation": round(float(ok.utilisation.mean()), 3),
            "total_pct": round(float(ok.total_pct.mean()), 2),
            "max_dd_pct": round(float(ok.max_dd_pct.mean()), 2),
        })
        rows.append({
            "benchmark": f"random (best of {len(ok)})",
            "sharpe": round(float(ok.sharpe.max()), 3),
            "ann_pct": round(float(ok.ann_pct.max()), 2),
            "ann_vol_pct": None,
            "per_trade_bps": round(float(ok.per_trade_bps.max()), 2),
            "n_trades": None, "utilisation": None,
            "total_pct": round(float(ok.total_pct.max()), 2), "max_dd_pct": None,
        })

    bot = bottom_tranche(h, sel_cfg, cost_cfg)
    bm = bot.metrics
    rows.append({
        "benchmark": "bottom tranche (LONG the worst)",
        "sharpe": bm["sharpe"], "ann_pct": bm["ann_return_pct"],
        "ann_vol_pct": bm["ann_vol_pct"], "per_trade_bps": bm["per_trade_bps_net"],
        "n_trades": bm["n_trades"], "utilisation": bm["mean_utilisation"],
        "total_pct": bm["total_return_pct"], "max_dd_pct": bm["max_drawdown_pct"],
    })

    iwm = iwm_buy_hold(h)
    for key in ("close_to_close", "intraday"):
        if key in iwm:
            b = iwm[key]
            rows.append({"benchmark": b["benchmark"], "sharpe": b["sharpe"],
                         "ann_pct": b["ann_pct"], "ann_vol_pct": b["ann_vol_pct"],
                         "per_trade_bps": None, "n_trades": None,
                         "utilisation": 1.0, "total_pct": b["total_pct"],
                         "max_dd_pct": b["max_dd_pct"]})

    extras = {"iwm": iwm, "orb": ORB_REFERENCE, "random_seeds": rnd.to_dict("records")}
    if len(ok):
        beat = float((ok.sharpe >= m["sharpe"]).mean())
        extras["strategy_vs_random"] = {
            "strategy_sharpe": m["sharpe"],
            "random_sharpe_mean": round(float(ok.sharpe.mean()), 3),
            "random_sharpe_max": round(float(ok.sharpe.max()), 3),
            "random_sharpe_sd": round(float(ok.sharpe.std(ddof=1)), 3),
            "frac_random_beating_strategy": beat,
            "z_vs_random": round((m["sharpe"] - float(ok.sharpe.mean()))
                                 / float(ok.sharpe.std(ddof=1)), 1)
            if float(ok.sharpe.std(ddof=1)) > 0 else None,
        }
    return pd.DataFrame(rows), extras


if __name__ == "__main__":
    import json
    from src.data.handover import load_handover
    from src.portfolio.selection import NEEDED

    cfg = SelectionConfig(k=5, sides="both", balance_sides=True,
                          bar_times=["10:50", "11:10"])
    cost = CostConfig(spread_bps=5.0)
    pd.set_option("display.width", 200)

    for key in ("development_2023_2024", "development_2020_2022"):
        h = load_handover(key, columns=NEEDED)
        print("\n" + "=" * 96)
        print(f"{key}   {h.meta['date_range'][0]} .. {h.meta['date_range'][1]}"
              f"   cost {cost.spread_bps} bps")
        print("=" * 96)
        table, extras = compare(h, cfg, cost, n_seeds=20)
        print(table.to_string(index=False))
        if "strategy_vs_random" in extras:
            print("\nvs random:", json.dumps(extras["strategy_vs_random"]))
        print(f"ORB floor: {ORB_REFERENCE['total_pct']}% total, "
              f"Sharpe {ORB_REFERENCE['sharpe']}")
