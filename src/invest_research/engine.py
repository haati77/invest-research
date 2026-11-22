"""Kennzahlen-Engine: leitet aus XBRL-Rohwerten abgeleitete Werte und Quoten ab.

Reine Funktionen ohne Datenbank und ohne KI. Fehlende Eingaben ergeben None bzw. gar keine
Zeile; es wird nie etwas geschätzt oder erfunden.

Abgeleitet werden:
  * Einzelquartale aus kumulierten Werten (Cashflow wird in 10-Qs nur kumuliert gemeldet):
    Q2 = H1 - Q1, Q3 = 9M - H1
  * Q4 (Jahreswert minus drei Quartale), da Q4 nie einzeln gemeldet wird
  * total_liabilities = Bilanzsumme - Eigenkapital, falls nicht getaggt
  * TTM (Summe der letzten vier Quartale) für Fluss-Kennzahlen
  * Quoten und Wachstum für Geschäftsjahre (FY) und TTM
"""
from __future__ import annotations

from datetime import date

from .facts import MetricRow

FLOW_NAMES = ("revenue", "gross_profit", "operating_income", "net_income",
              "eps_diluted", "operating_cash_flow", "capex")

# Anzeige-Metadaten der Quoten: Name -> (Label, höher ist besser, Format)
RATIOS: dict[str, tuple[str, bool, str]] = {
    "revenue_growth": ("Umsatzwachstum", True, "pct"),
    "operating_income_growth": ("EBIT-Wachstum", True, "pct"),
    "net_income_growth": ("Gewinnwachstum", True, "pct"),
    "eps_growth": ("EPS-Wachstum", True, "pct"),
    "gross_margin": ("Bruttomarge", True, "pct"),
    "operating_margin": ("Operative Marge", True, "pct"),
    "net_margin": ("Nettomarge", True, "pct"),
    "fcf_margin": ("FCF-Marge", True, "pct"),
    "roe": ("Eigenkapitalrendite", True, "pct"),
    "roa": ("Gesamtkapitalrendite", True, "pct"),
    "debt_to_equity": ("Verschuldung (Verb./EK)", False, "x"),
}

Index = dict[tuple[str, str, date], float]


def _index(rows: list[MetricRow]) -> Index:
    return {(r.name, r.period_type, r.period_end): r.value for r in rows}


def _row(name: str, value: float, unit: str, end: date, ptype: str) -> MetricRow:
    return MetricRow(name, value, unit, end, ptype)


def _units(rows: list[MetricRow]) -> dict[str, str]:
    return {r.name: r.unit for r in rows}


def derive_quarters_from_ytd(base: list[MetricRow]) -> list[MetricRow]:
    """Q2 = H1 - Q1 und Q3 = 9M - H1, falls das Einzelquartal nicht selbst gemeldet wurde."""
    idx = _index(base)
    units = _units(base)
    out: list[MetricRow] = []

    def quarter_before(name: str, ptype: str, end: date, value: float | None = None) -> float | None:
        for (n, p, e), v in idx.items():
            if n == name and p == ptype and 75 <= (end - e).days <= 110:
                return v
        return None

    for (name, ptype, end), val in list(idx.items()):
        if name not in FLOW_NAMES or (name, "Q", end) in idx:
            continue
        if ptype == "H1":
            q1 = quarter_before(name, "Q", end)
            if q1 is not None:
                out.append(_row(name, val - q1, units[name], end, "Q"))
        elif ptype == "9M":
            h1 = quarter_before(name, "H1", end)
            if h1 is not None:
                out.append(_row(name, val - h1, units[name], end, "Q"))
    return out


def derive_q4(base: list[MetricRow]) -> list[MetricRow]:
    """Q4 = FY - (Q1 + Q2 + Q3), nur wenn genau diese drei Quartale vorliegen."""
    idx = _index(base)
    units = _units(base)
    out: list[MetricRow] = []
    for name in FLOW_NAMES:
        for (n, ptype, fy_end), fy_val in list(idx.items()):
            if n != name or ptype != "FY" or (name, "Q", fy_end) in idx:
                continue
            quarters = [v for (n2, p2, e), v in idx.items()
                        if n2 == name and p2 == "Q" and 0 < (fy_end - e).days < 350]
            if len(quarters) == 3:
                out.append(_row(name, fy_val - sum(quarters), units[name], fy_end, "Q"))
    return out


def derive_total_liabilities(base: list[MetricRow]) -> list[MetricRow]:
    idx = _index(base)
    out = []
    for (name, ptype, end), assets in idx.items():
        if name == "total_assets" and ("total_liabilities", ptype, end) not in idx:
            equity = idx.get(("equity", ptype, end))
            if equity is not None:
                out.append(_row("total_liabilities", assets - equity, "USD", end, ptype))
    return out


def derive_ttm(rows: list[MetricRow]) -> list[MetricRow]:
    """TTM = Summe von vier lückenlos aufeinanderfolgenden Quartalen (je ~90 Tage Abstand)."""
    units = _units(rows)
    out: list[MetricRow] = []
    for name in FLOW_NAMES:
        quarters = sorted((r.period_end, r.value) for r in rows
                          if r.name == name and r.period_type == "Q")
        for i in range(3, len(quarters)):
            window = quarters[i - 3:i + 1]
            gaps = [(window[k + 1][0] - window[k][0]).days for k in range(3)]
            if all(75 <= g <= 110 for g in gaps):
                out.append(_row(name, sum(v for _, v in window), units[name], window[-1][0], "TTM"))
    return out


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _growth(cur: float | None, prev: float | None) -> float | None:
    """Wachstum nur bei positiver Basis; bei Verlusten wäre die Prozentzahl irreführend."""
    if cur is None or prev is None or prev <= 0:
        return None
    return cur / prev - 1


def _prior(idx: Index, name: str, ptype: str, end: date) -> float | None:
    for (n, p, e), v in idx.items():
        if n == name and p == ptype and 350 <= (end - e).days <= 380:
            return v
    return None


def derive_ratios(rows: list[MetricRow]) -> list[MetricRow]:
    idx = _index(rows)
    out: list[MetricRow] = []

    def put(name: str, value: float | None, unit: str, end: date, ptype: str) -> None:
        if value is not None:
            out.append(_row(name, value, unit, end, ptype))

    periods = sorted({(p, e) for (n, p, e) in idx if n == "revenue" and p in ("FY", "TTM")},
                     key=lambda x: x[1])
    for ptype, end in periods:
        g = lambda name: idx.get((name, ptype, end))  # noqa: E731 - Flusswert dieser Periode
        bal = lambda name: idx.get((name, "instant", end))  # noqa: E731 - Bilanzwert zum Stichtag
        rev, ni = g("revenue"), g("net_income")
        pos_rev = rev if rev and rev > 0 else None
        equity = bal("equity")
        pos_equity = equity if equity is not None and equity > 0 else None
        fcf = None
        if g("operating_cash_flow") is not None and g("capex") is not None:
            fcf = g("operating_cash_flow") - abs(g("capex"))
            put("fcf", fcf, "USD", end, ptype)
        cash, debt = bal("cash"), bal("long_term_debt")
        if cash is not None and debt is not None:
            put("net_debt", debt - cash, "USD", end, ptype)

        put("gross_margin", _div(g("gross_profit"), pos_rev), "ratio", end, ptype)
        put("operating_margin", _div(g("operating_income"), pos_rev), "ratio", end, ptype)
        put("net_margin", _div(ni, pos_rev), "ratio", end, ptype)
        put("fcf_margin", _div(fcf, pos_rev), "ratio", end, ptype)
        put("roe", _div(ni, pos_equity), "ratio", end, ptype)
        put("roa", _div(ni, bal("total_assets")), "ratio", end, ptype)
        put("debt_to_equity", _div(bal("total_liabilities"), pos_equity), "ratio", end, ptype)

        for src, dst in (("revenue", "revenue_growth"), ("operating_income", "operating_income_growth"),
                         ("net_income", "net_income_growth"), ("eps_diluted", "eps_growth")):
            put(dst, _growth(g(src), _prior(idx, src, ptype, end)), "ratio", end, ptype)
    return out


def compute_derived(base: list[MetricRow]) -> list[MetricRow]:
    """Alle abgeleiteten Zeilen zu den XBRL-Rohwerten (ohne diese selbst zu wiederholen)."""
    derived = derive_quarters_from_ytd(base)
    derived += derive_q4(base + derived) + derive_total_liabilities(base)
    derived += derive_ttm(base + derived)
    derived += derive_ratios(base + derived)
    return derived
