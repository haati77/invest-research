"""Peer-Vergleich: Momentaufnahme je Unternehmen plus Rang/Perzentil innerhalb der Branche."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .engine import RATIOS
from .models import Company, Metric

SNAPSHOT_EXTRA = ("revenue", "net_income", "fcf", "net_debt")


def snapshot(session: Session, basis: str = "FY") -> pd.DataFrame:
    """Neueste Periode je Unternehmen (basis 'FY' oder 'TTM'); Index = Ticker.

    Die Spalte 'period_end' zeigt den Stichtag; Geschäftsjahre sind nicht kalendergleich,
    der Vergleich ist daher 'letztes verfügbares Geschäftsjahr' bzw. 'letzte 12 Monate'.
    """
    if basis not in ("FY", "TTM"):
        raise ValueError("basis muss 'FY' oder 'TTM' sein")
    wanted = list(RATIOS) + list(SNAPSHOT_EXTRA)
    rows = []
    for c in session.scalars(select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker)):
        ends = [m.period_end for m in session.scalars(
            select(Metric).where(Metric.company_id == c.id, Metric.name == "revenue",
                                 Metric.period_type == basis))]
        if not ends:
            continue
        end = max(ends)
        vals = {m.name: m.value for m in session.scalars(
            select(Metric).where(Metric.company_id == c.id, Metric.period_end == end,
                                 Metric.period_type == basis, Metric.name.in_(wanted)))}
        rows.append({"ticker": c.ticker, "sector": c.sector, "period_end": end, **vals})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in wanted:
        if col not in df:
            df[col] = float("nan")
    return df.set_index("ticker")


def _pct_rank(series: pd.Series, higher_is_better: bool) -> pd.Series:
    """0 = schlechtester, 1 = bester Wert der Gruppe; NaN bei <2 Werten."""
    valid = series.dropna()
    if len(valid) < 2:
        return pd.Series(float("nan"), index=series.index)
    ranks = valid.rank(ascending=higher_is_better, method="average")  # höchster Rang = bester
    return ((ranks - 1) / (len(valid) - 1)).reindex(series.index)


def add_percentiles(df: pd.DataFrame) -> pd.DataFrame:
    """Hängt je Quote 'pct_<name>' (innerhalb der Branche) und 'pctall_<name>' (alle) an."""
    out = df.copy()
    for name, (_, higher_better, _) in RATIOS.items():
        if name not in out:
            continue
        out[f"pctall_{name}"] = _pct_rank(out[name], higher_better)
        out[f"pct_{name}"] = out.groupby("sector", group_keys=False)[name].apply(
            lambda s, hb=higher_better: _pct_rank(s, hb))
    return out


def overall_score(df: pd.DataFrame, group: str = "sector") -> pd.Series:
    """Durchschnitt der Perzentile über alle verfügbaren Quoten (reine Orientierung)."""
    prefix = "pct_" if group == "sector" else "pctall_"
    cols = [c for c in df.columns if c.startswith(prefix)]
    return df[cols].mean(axis=1, skipna=True)


def format_value(name: str, value: float) -> str:
    if pd.isna(value):
        return "-"
    kind = RATIOS[name][2]
    return f"{value * 100:.1f}%" if kind == "pct" else f"{value:.2f}x"
