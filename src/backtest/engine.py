#!/usr/bin/env python3
"""
src/backtest/engine.py -- trades in, daily capital returns out.

The capital model
-----------------
Capital is `max_positions` equal slots, fixed before the session. A position
takes one slot at weight 1/max_positions. **Unfilled slots earn zero and stay
in the denominator.** A book that fills 8 of 20 slots is 40% deployed and its
return says so.

That is the whole point of the fixed-slot design: it makes idle capital visible.
A per-trade edge of 68 bps at 40% utilisation is 27 bps on capital, and only
the second number can be compared against buy-and-hold.

Three things §8 requires, built in rather than optional
-------------------------------------------------------
1. **Non-traded days are padded with ZERO, not omitted.** Annualising only
   traded days overstates a selective strategy by sqrt(window / traded). The
   day universe is every session in the panel, not every session with a trade.

2. **Total edge is reported beside per-trade edge.** total = per_trade x
   trades_taken. Upstream, every regime filter that raised per-trade return
   cut days traded enough to produce less money, and that trade-off is
   invisible in a per-trade number. Both appear in every metrics dict.

3. **Per year and per month, never pooled alone.** The upstream project's
   headline numbers repeatedly concealed regime effects that appeared only per
   fold or per year.

Arithmetic, not compounded
--------------------------
Daily returns are summed, not compounded, and drawdown runs on the cumulative
sum. For a constant-capital book that is rebuilt from cash every morning, this
is the right convention: there is no reinvestment, and compounding would
attribute path-dependent growth to a strategy that does not have it. Compounded
figures are reported alongside for reference and should not be used for Sharpe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.data.handover import Handover
from src.execution.costs import (CostConfig, DEFAULT_SWEEP, apply_costs,
                                 feasibility)
from src.portfolio.selection import SelectionConfig, select, selection_summary

TRADING_DAYS = 252


@dataclass
class BacktestResult:
    daily: pd.DataFrame          # one row per SESSION, including non-traded
    trades: pd.DataFrame         # one row per position, with costs applied
    metrics: dict
    sel_cfg: SelectionConfig
    cost_cfg: CostConfig
    meta: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        m = self.metrics
        return (f"<Backtest sharpe={m['sharpe']:.2f} "
                f"ann_ret={m['ann_return_pct']:.1f}% "
                f"traded={m['n_traded_days']}/{m['n_sessions']} "
                f"cost={self.cost_cfg.spread_bps}bps>")


def _daily_frame(trades: pd.DataFrame, all_dates: pd.Series,
                 max_positions: int) -> pd.DataFrame:
    """Aggregate positions to sessions, padding untraded sessions with zero."""
    t = trades.copy()
    t["contrib_gross"] = t["ret"] * t["weight"]
    t["contrib_net"] = t["net_ret"] * t["weight"]
    t["is_long"] = (t["side"] == "long").astype(int)
    t["is_short"] = (t["side"] == "short").astype(int)

    g = t.groupby("date", observed=True).agg(
        gross=("contrib_gross", "sum"),
        net=("contrib_net", "sum"),
        n_positions=("weight", "size"),
        n_long=("is_long", "sum"),
        n_short=("is_short", "sum"),
        cost_bps=("cost_bps", "mean"),
    )

    # THE padding step. Every session in the panel appears, traded or not.
    idx = pd.Index(sorted(pd.to_datetime(all_dates).unique()), name="date")
    d = g.reindex(idx).fillna(0.0)

    d["n_positions"] = d["n_positions"].astype(int)
    d["n_long"] = d["n_long"].astype(int)
    d["n_short"] = d["n_short"].astype(int)
    d["utilisation"] = d["n_positions"] / max_positions
    d["gross_exposure"] = d["utilisation"]
    d["net_exposure"] = (d["n_long"] - d["n_short"]) / max_positions
    d["traded"] = d["n_positions"] > 0
    d["cum_net"] = d["net"].cumsum()

    # Drawdown is a statement about CAPITAL, so it runs on the compounded
    # equity curve even though returns are otherwise arithmetic. On the
    # arithmetic path a losing book sums past -100% and reports impossible
    # figures: the bottom-tranche benchmark came back at -102.94% drawdown
    # against -100.68% total, which is an accounting artefact, not a result.
    # Compounded equity is bounded at -100% by construction.
    d["equity"] = (1.0 + d["net"]).cumprod()
    d["drawdown"] = d["equity"] / d["equity"].cummax() - 1.0
    d["wiped_out"] = d["equity"] <= 0.0
    return d


def _metrics(d: pd.DataFrame, trades: pd.DataFrame, max_positions: int) -> dict:
    r = d["net"]
    n = len(r)
    mean, sd = float(r.mean()), float(r.std(ddof=1))

    sharpe = (mean / sd) * np.sqrt(TRADING_DAYS) if sd > 0 else np.nan
    t_stat = mean / (sd / np.sqrt(n)) if sd > 0 and n > 1 else np.nan

    # Traded-days-only Sharpe, for contrast. NOT the number to quote --
    # included so the size of the overstatement is visible rather than
    # theoretical (§8).
    rt = r[d["traded"]]
    sharpe_traded = ((float(rt.mean()) / float(rt.std(ddof=1))) * np.sqrt(TRADING_DAYS)
                     if len(rt) > 1 and rt.std(ddof=1) > 0 else np.nan)

    per_trade_bps = float(trades["net_ret"].mean()) * 1e4 if len(trades) else np.nan
    gross_per_trade_bps = float(trades["ret"].mean()) * 1e4 if len(trades) else np.nan

    return {
        "n_sessions": int(n),
        "n_traded_days": int(d["traded"].sum()),
        "traded_frac": round(float(d["traded"].mean()), 3),
        "n_trades": int(len(trades)),

        "mean_daily_bps": round(mean * 1e4, 2),
        "ann_return_pct": round(mean * TRADING_DAYS * 100, 2),
        "ann_vol_pct": round(sd * np.sqrt(TRADING_DAYS) * 100, 2),
        "sharpe": round(float(sharpe), 3),
        "sharpe_traded_days_only": round(float(sharpe_traded), 3),
        "t_stat_daily": round(float(t_stat), 2),

        # per-trade AND total, always together (§8)
        "per_trade_bps_net": round(per_trade_bps, 2),
        "per_trade_bps_gross": round(gross_per_trade_bps, 2),
        "total_return_pct": round(float(d["net"].sum()) * 100, 2),
        "total_return_compounded_pct": round(float((1 + d["net"]).prod() - 1) * 100, 2),

        "hit_rate_days": round(float((d.loc[d["traded"], "net"] > 0).mean()), 3),
        "max_drawdown_pct": round(float(d["drawdown"].min()) * 100, 2),
        # Arithmetic total has no floor. Flagged when it passes the point at
        # which a real book no longer exists, so a nonsensical number
        # announces itself instead of sitting in a table looking like data.
        "total_exceeds_capital": bool(d["cum_net"].min() <= -1.0),
        "ruined": bool(d["wiped_out"].any()),

        "mean_utilisation": round(float(d.loc[d["traded"], "utilisation"].mean()), 3),
        "mean_gross_exposure": round(float(d["gross_exposure"].mean()), 3),
        "mean_net_exposure": round(float(d["net_exposure"].mean()), 3),
        "positions_per_traded_day": round(
            float(d.loc[d["traded"], "n_positions"].mean()), 2),
    }


def run_backtest(h: Handover, sel_cfg: SelectionConfig,
                 cost_cfg: CostConfig | None = None,
                 trades: pd.DataFrame | None = None) -> BacktestResult:
    """Panel -> daily capital returns and metrics.

    `trades` may be passed in to reuse a selection across a cost sweep --
    selection is the expensive step and does not depend on cost.
    """
    cost_cfg = cost_cfg or CostConfig()
    if trades is None:
        trades = select(h, sel_cfg)

    priced = apply_costs(trades, cost_cfg)
    daily = _daily_frame(priced, h.df["date"], sel_cfg.max_positions)
    m = _metrics(daily, priced, sel_cfg.max_positions)

    meta = {
        "handover": h.meta,
        "selection": selection_summary(trades, sel_cfg),
        "cost": cost_cfg.describe(),
        "feasibility": feasibility(trades, cost_cfg),
    }
    return BacktestResult(daily=daily, trades=priced, metrics=m,
                          sel_cfg=sel_cfg, cost_cfg=cost_cfg, meta=meta)


def by_period(daily: pd.DataFrame, freq: str = "YE") -> pd.DataFrame:
    """Per-year or per-month breakdown. §8: never report pooled alone."""
    g = daily.groupby(pd.Grouper(freq=freq))
    out = pd.DataFrame({
        "sessions": g.size(),
        "traded": g["traded"].sum(),
        "total_pct": g["net"].sum() * 100,
        "mean_bps": g["net"].mean() * 1e4,
        "sharpe": g["net"].apply(
            lambda s: (s.mean() / s.std(ddof=1)) * np.sqrt(TRADING_DAYS)
            if len(s) > 1 and s.std(ddof=1) > 0 else np.nan),
        "hit_rate": g["net"].apply(lambda s: (s[s != 0] > 0).mean()),
        "max_dd_pct": g["net"].apply(
            lambda s: (s.cumsum() - s.cumsum().cummax()).min() * 100),
    })
    out.index = out.index.strftime("%Y" if freq.startswith("Y") else "%Y-%m")
    return out.round(3)


def sweep_costs(h: Handover, sel_cfg: SelectionConfig,
                levels: list[float] | None = None,
                borrow_bps: float = 0.0) -> pd.DataFrame:
    """Run the same selection across cost levels. THE headline output (§8).

    Selection runs once and is reused: cost does not change which rows are
    selected, only what they earn.
    """
    levels = DEFAULT_SWEEP if levels is None else levels
    trades = select(h, sel_cfg)

    rows = []
    for lv in levels:
        cc = CostConfig(spread_bps=lv, borrow_bps=borrow_bps)
        res = run_backtest(h, sel_cfg, cc, trades=trades)
        rows.append({
            "spread_bps": lv,
            "feasible": res.meta["feasibility"]["label"],
            "frac_below_floor": res.meta["feasibility"]["frac_below_tick_floor"],
            "per_trade_bps": res.metrics["per_trade_bps_net"],
            "n_trades": res.metrics["n_trades"],
            "total_pct": res.metrics["total_return_pct"],
            "ann_pct": res.metrics["ann_return_pct"],
            "ann_vol_pct": res.metrics["ann_vol_pct"],
            "sharpe": res.metrics["sharpe"],
            "t_stat": res.metrics["t_stat_daily"],
            "max_dd_pct": res.metrics["max_drawdown_pct"],
            "hit_rate": res.metrics["hit_rate_days"],
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    from src.data.handover import load_handover
    from src.portfolio.selection import NEEDED

    h = load_handover("development_2023_2024", columns=NEEDED)
    cfg = SelectionConfig(k=5, sides="both", balance_sides=True,
                          bar_times=["10:50", "11:10"])

    res = run_backtest(h, cfg, CostConfig(spread_bps=5.0))
    print(res)
    print("\nmetrics at 5 bps:")
    for k, v in res.metrics.items():
        print(f"  {k:32s} {v}")

    pd.set_option("display.width", 200)
    print("\ncost sweep:")
    print(sweep_costs(h, cfg).to_string(index=False))

    print("\nby year:")
    print(by_period(res.daily, "YE").to_string())
    print("\nby month:")
    print(by_period(res.daily, "ME").to_string())
