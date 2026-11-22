"""Datenbank-Setup und Watchlist-Synchronisierung."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import Settings, WatchlistEntry
from .models import Base, Company


def get_engine(settings: Settings) -> Engine:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(settings.database_url)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine: Engine) -> None:
    """Einfache Migration: ergänzt in bestehenden Datenbanken neue (nullable) Spalten.

    create_all legt nur fehlende Tabellen an, nie fehlende Spalten. Nicht-nullable Spalten
    ohne Default können so nicht nachgerüstet werden und gelten als Programmierfehler.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable and col.server_default is None:
                    raise RuntimeError(f"Spalte {table.name}.{col.name} fehlt und ist nicht nullable: "
                                       "Datenbank neu aufbauen oder Migration schreiben.")
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                                  f"{col.type.compile(engine.dialect)}"))


def sync_watchlist(session: Session, entries: list[WatchlistEntry]) -> None:
    """Legt Unternehmen aus der YAML an; Titel, die nicht mehr drin stehen, werden deaktiviert."""
    wanted = {e.ticker: e for e in entries}
    existing = {c.ticker: c for c in session.scalars(select(Company))}
    for ticker, entry in wanted.items():
        company = existing.get(ticker)
        if company is None:
            session.add(Company(ticker=ticker, sector=entry.sector, cik=entry.cik, on_watchlist=True))
        else:
            company.sector = entry.sector
            if entry.cik:
                company.cik = entry.cik
            company.on_watchlist = True
    for ticker, company in existing.items():
        if ticker not in wanted:
            company.on_watchlist = False
    session.commit()
