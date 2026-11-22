"""Lädt Filings und Kennzahlen von EDGAR in die Datenbank (idempotent)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .edgar import EdgarClient, parse_filings
from .facts import parse_facts
from .models import Company, Event, Filing, Metric

log = logging.getLogger(__name__)

TRACKED_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K"}
# Wichtigkeit neuer Filings für Alerts: Jahres-/Quartalsberichte höher als 8-K
FORM_SEVERITY = {"10-K": 3, "10-K/A": 2, "10-Q": 2, "10-Q/A": 1, "8-K": 1}


@dataclass
class IngestResult:
    ticker: str
    new_filings: int = 0
    metrics_written: int = 0
    error: str | None = None


def ingest_company(session: Session, client: EdgarClient, company: Company,
                   *, since_year: int, ticker_map: dict[str, str] | None = None) -> IngestResult:
    result = IngestResult(company.ticker)
    if not company.cik:
        cik = (ticker_map or {}).get(company.ticker)
        if not cik:
            raise ValueError(f"Kein CIK für {company.ticker}: in watchlist.yaml 'cik' eintragen.")
        company.cik = cik

    subs = client.submissions(company.cik)
    company.name = subs.get("name") or company.name

    first_load = not session.scalar(select(Filing.id).where(Filing.company_id == company.id).limit(1))
    known = {a for a in session.scalars(select(Filing.accession_number).where(Filing.company_id == company.id))}
    for f in parse_filings(subs, TRACKED_FORMS, since_year):
        if f.accession_number in known:
            continue
        session.add(Filing(company_id=company.id, accession_number=f.accession_number,
                           form_type=f.form_type, filed_at=f.filed_at,
                           period_end=f.period_end, url=f.url))
        result.new_filings += 1
        if not first_load:  # beim Erstladen keine Alert-Flut
            session.add(Event(company_id=company.id, kind="new_filing",
                              severity=FORM_SEVERITY.get(f.form_type, 1),
                              title=f"Neues Filing: {f.form_type} ({f.filed_at})", detail=f.url))

    rows = parse_facts(client.company_facts(company.cik), since_year)
    existing = {(m.name, m.period_end, m.period_type): m
                for m in session.scalars(select(Metric).where(Metric.company_id == company.id))}
    for r in rows:
        m = existing.get((r.name, r.period_end, r.period_type))
        if m is None:
            session.add(Metric(company_id=company.id, name=r.name, value=r.value, unit=r.unit,
                               period_end=r.period_end, period_type=r.period_type, source="xbrl"))
            result.metrics_written += 1
        elif m.value != r.value or m.source != "xbrl":
            m.value = r.value
            m.source = "xbrl"  # echter Rohwert verdrängt einen abgeleiteten
            result.metrics_written += 1
    session.commit()
    return result


def ingest_watchlist(session: Session, client: EdgarClient, *, since_year: int,
                     only_ticker: str | None = None) -> list[IngestResult]:
    companies = list(session.scalars(
        select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker)))
    if only_ticker:
        companies = [c for c in companies if c.ticker == only_ticker.upper()]
    ticker_map = client.ticker_map() if any(not c.cik for c in companies) else None
    results = []
    for c in companies:
        try:
            results.append(ingest_company(session, client, c, since_year=since_year, ticker_map=ticker_map))
        except Exception as exc:  # ein Fehler soll die übrigen Titel nicht stoppen
            session.rollback()
            log.exception("Ingest fehlgeschlagen: %s", c.ticker)
            results.append(IngestResult(c.ticker, error=str(exc)))
    return results
