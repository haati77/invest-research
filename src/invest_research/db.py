"""Datenbank-Setup und Watchlist-Synchronisierung."""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import Settings, WatchlistEntry
from .models import Base, Company


def get_engine(settings: Settings) -> Engine:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(settings.database_url)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def sync_watchlist(session: Session, entries: list[WatchlistEntry]) -> None:
    """Legt Unternehmen aus der YAML an; Titel, die nicht mehr drin stehen, werden deaktiviert."""
    wanted = {e.ticker: e for e in entries}
    existing = {c.ticker: c for c in session.scalars(select(Company))}
    for ticker, entry in wanted.items():
        company = existing.get(ticker)
        if company is None:
            session.add(Company(ticker=ticker, sector=entry.sector, on_watchlist=True))
        else:
            company.sector = entry.sector
            company.on_watchlist = True
    for ticker, company in existing.items():
        if ticker not in wanted:
            company.on_watchlist = False
    session.commit()
