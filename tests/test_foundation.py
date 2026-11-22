from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invest_research.config import PROJECT_ROOT, WatchlistEntry, load_watchlist
from invest_research.db import init_db, sync_watchlist
from invest_research.models import Company, Metric


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        yield s


def test_default_watchlist_loads():
    entries = load_watchlist(PROJECT_ROOT / "config" / "watchlist.yaml")
    assert len(entries) == 10
    assert all(e.ticker == e.ticker.upper() for e in entries)


def test_sync_watchlist_adds_and_deactivates(session):
    sync_watchlist(session, [WatchlistEntry(ticker="MSFT", sector="Software"),
                             WatchlistEntry(ticker="KO", sector="Konsum")])
    assert session.scalar(select(Company).where(Company.ticker == "MSFT")).on_watchlist

    sync_watchlist(session, [WatchlistEntry(ticker="MSFT", sector="Software")])
    ko = session.scalar(select(Company).where(Company.ticker == "KO"))
    assert ko is not None and ko.on_watchlist is False


def test_metric_unique_per_period(session):
    c = Company(ticker="MSFT")
    session.add(c)
    session.commit()
    kwargs = dict(company_id=c.id, name="Revenues", value=1.0, period_end=date(2025, 6, 30), period_type="FY")
    session.add(Metric(**kwargs))
    session.commit()
    session.add(Metric(**kwargs))
    with pytest.raises(IntegrityError):
        session.commit()
