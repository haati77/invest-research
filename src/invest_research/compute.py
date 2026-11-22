"""Schreibt abgeleitete Kennzahlen (source='computed') in die Datenbank (idempotent)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .engine import compute_derived
from .facts import MetricRow
from .models import Company, Metric


def compute_company(session: Session, company: Company) -> int:
    """Berechnet alle abgeleiteten Werte für ein Unternehmen. Gibt Anzahl geänderter Zeilen zurück."""
    existing = {(m.name, m.period_end, m.period_type): m
                for m in session.scalars(select(Metric).where(Metric.company_id == company.id))}
    base = [MetricRow(m.name, m.value, m.unit or "", m.period_end, m.period_type)
            for m in existing.values() if m.source == "xbrl" and m.value is not None]
    changed = 0
    for r in compute_derived(base):
        m = existing.get((r.name, r.period_end, r.period_type))
        if m is None:
            session.add(Metric(company_id=company.id, name=r.name, value=r.value, unit=r.unit,
                               period_end=r.period_end, period_type=r.period_type, source="computed"))
            changed += 1
        elif m.source == "computed" and m.value != r.value:
            m.value = r.value
            changed += 1
        # XBRL-Rohwerte werden nie von abgeleiteten Werten überschrieben
    session.commit()
    return changed


def compute_watchlist(session: Session) -> dict[str, int]:
    companies = session.scalars(select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker))
    return {c.ticker: compute_company(session, c) for c in companies}
