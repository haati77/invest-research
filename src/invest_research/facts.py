"""Übersetzt XBRL-'companyfacts' in einheitliche Kennzahlen-Rohwerte.

Mehrere XBRL-Tags liefern dasselbe Konzept (z. B. Umsatz). Tags werden zusammengeführt:
bei gleicher Periode gewinnt der Tag mit höherer Priorität, bei gleichem Tag die
zuletzt eingereichte Meldung (Korrekturen überschreiben).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

ANNUAL_FORMS = {"10-K", "10-K/A"}
QUARTER_FORMS = {"10-Q", "10-Q/A"}
ALLOWED_FORMS = ANNUAL_FORMS | QUARTER_FORMS


@dataclass(frozen=True)
class Concept:
    name: str
    tags: tuple[str, ...]  # Priorität: erster Tag zuerst
    kind: str  # "duration" (Fluss) | "instant" (Bestand)
    unit: str = "USD"
    taxonomy: str = "us-gaap"


CONCEPTS: tuple[Concept, ...] = (
    Concept("revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax",
                        "RevenueFromContractWithCustomerIncludingAssessedTax",
                        "SalesRevenueNet", "Revenues"), "duration"),
    Concept("gross_profit", ("GrossProfit",), "duration"),
    Concept("operating_income", ("OperatingIncomeLoss",), "duration"),
    Concept("net_income", ("NetIncomeLoss", "ProfitLoss"), "duration"),
    Concept("eps_diluted", ("EarningsPerShareDiluted",), "duration", unit="USD/shares"),
    Concept("operating_cash_flow", ("NetCashProvidedByUsedInOperatingActivities",), "duration"),
    Concept("capex", ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                      "PaymentsToAcquireOtherPropertyPlantAndEquipment"), "duration"),
    Concept("total_assets", ("Assets",), "instant"),
    Concept("total_liabilities", ("Liabilities",), "instant"),
    Concept("equity", ("StockholdersEquity",
                       "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), "instant"),
    Concept("long_term_debt", ("LongTermDebt", "LongTermDebtNoncurrent"), "instant"),
    Concept("cash", ("CashAndCashEquivalentsAtCarryingValue",
                     "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), "instant"),
    Concept("shares_outstanding", ("EntityCommonStockSharesOutstanding",), "instant",
            unit="shares", taxonomy="dei"),
)


@dataclass(frozen=True)
class MetricRow:
    name: str
    value: float
    unit: str
    period_end: date
    period_type: str  # FY | Q | H1 | 9M | instant


def _period_type(concept: Concept, entry: dict) -> str | None:
    if concept.kind == "instant":
        return "instant"
    if not entry.get("start"):
        return None
    days = (date.fromisoformat(entry["end"]) - date.fromisoformat(entry["start"])).days
    if 350 <= days <= 380:
        return "FY"
    if 80 <= days <= 100:
        return "Q"
    if 170 <= days <= 200:
        return "H1"  # kumulierte 6 Monate (10-Q); dient nur zur Ableitung von Einzelquartalen
    if 260 <= days <= 290:
        return "9M"  # kumulierte 9 Monate
    return None


def parse_facts(facts: dict, since_year: int) -> list[MetricRow]:
    out: list[MetricRow] = []
    for concept in CONCEPTS:
        tax = facts.get("facts", {}).get(concept.taxonomy, {})
        best: dict[tuple[str, str], tuple[int, str, float]] = {}  # (end, ptype) -> (prio, filed, val)
        for prio, tag in enumerate(concept.tags):
            entries = tax.get(tag, {}).get("units", {}).get(concept.unit, [])
            for e in entries:
                if e.get("form") not in ALLOWED_FORMS or e.get("val") is None:
                    continue
                if int(e["end"][:4]) < since_year:
                    continue
                ptype = _period_type(concept, e)
                if ptype is None:
                    continue
                key = (e["end"], ptype)
                cand = (prio, e["filed"], float(e["val"]))
                cur = best.get(key)
                if cur is None or cand[0] < cur[0] or (cand[0] == cur[0] and cand[1] > cur[1]):
                    best[key] = cand
        for (end, ptype), (_, _, val) in sorted(best.items()):
            out.append(MetricRow(concept.name, val, concept.unit, date.fromisoformat(end), ptype))
    return out
