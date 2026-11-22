import os
from datetime import date, timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from invest_research import queries
from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.models import Company, Event, Metric, Price
from invest_research.prices import PriceError, PriceRow, YahooProvider, parse_yahoo_chart, update_prices
from invest_research.valuation import (market_cap, p_fcf_ratio, pe_ratio, price_change, ps_ratio)

CHART = {"chart": {"result": [{
    "meta": {"gmtoffset": -18000},
    "timestamp": [1794839400, 1794925800, 1795012200],
    "indicators": {"quote": [{"close": [480.0, None, 484.5], "volume": [10, None, 30]}],
                   "adjclose": [{"adjclose": [479.0, None, 484.5]}]}}], "error": None}}


def test_parse_yahoo_skips_gaps_and_keeps_adjclose():
    rows = parse_yahoo_chart(CHART)
    assert [r.close for r in rows] == [480.0, 484.5]
    assert rows[0].adj_close == 479.0 and rows[1].volume == 30


def test_parse_yahoo_error_raises():
    with pytest.raises(PriceError):
        parse_yahoo_chart({"chart": {"result": None, "error": {"code": "Not Found"}}})


def test_yahoo_provider_http_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(PriceError, match="404"):
        YahooProvider(client, max_retries=0).history("MSFT", date(2026, 1, 1))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software"),
                           WatchlistEntry(ticker="BAD", sector="Software")])
        yield s


class FakeProvider:
    def __init__(self):
        self.calls = []

    def history(self, ticker, start):
        self.calls.append((ticker, start))
        if ticker == "BAD":
            raise PriceError("kaputt")
        return [PriceRow(date(2026, 11, 19), 100.0, 99.0, 1.0), PriceRow(date(2026, 11, 20), 110.0, 109.0, 2.0)]


def test_update_prices_incremental_and_error_isolated(session):
    prov = FakeProvider()
    rep = update_prices(session, prov, pause=0)
    assert rep["MSFT"] == "2 neue Kurse" and rep["BAD"].startswith("FEHLER")
    rep2 = update_prices(session, prov, pause=0)
    assert rep2["MSFT"] == "0 neue Kurse"  # idempotent
    assert prov.calls[-1] == ("MSFT", date(2026, 11, 20) - timedelta(days=7))  # inkrementeller Start
    assert len(session.scalars(select(Price)).all()) == 2


def test_valuation_guards():
    assert market_cap(100, 1e9) == 1e11 and market_cap(None, 1e9) is None and market_cap(100, 0) is None
    assert pe_ratio(100, 5) == 20 and pe_ratio(100, -2) is None and pe_ratio(100, 0) is None
    assert ps_ratio(1e11, 5e10) == 2 and ps_ratio(1e11, -1) is None
    assert p_fcf_ratio(1e11, 1e10) == 10 and p_fcf_ratio(1e11, -5) is None


def test_price_change_windows():
    hist = [(date(2025, 12, 30), 80.0), (date(2026, 10, 20), 90.0), (date(2026, 11, 20), 100.0)]
    assert price_change(hist, 30) == pytest.approx(100 / 90 - 1)
    assert price_change(hist, ytd=True) == pytest.approx(100 / 80 - 1)
    assert price_change(hist, 365 * 3) is None  # Historie reicht nicht -> nichts erfunden
    assert price_change([]) is None


def seed(session):
    c = session.scalar(select(Company).where(Company.ticker == "MSFT"))
    for i in range(400):
        session.add(Price(company_id=c.id, date=date(2026, 11, 20) - timedelta(days=399 - i),
                          close=100.0 + i * 0.1))
    ttm = date(2026, 9, 30)
    for name, val, ptype in (("revenue", 300e9, "TTM"), ("eps_diluted", 20.0, "TTM"), ("fcf", 80e9, "TTM"),
                             ("shares_outstanding", 7.4e9, "instant")):
        session.add(Metric(company_id=c.id, name=name, value=val, unit="x", period_end=ttm,
                           period_type=ptype, source="xbrl"))
    session.commit()
    return c


def test_overview_computes_valuation(session):
    seed(session)
    ov = queries.overview(session)
    row = ov.loc["MSFT"]
    assert row["price"] == pytest.approx(139.9)
    assert row["pe"] == pytest.approx(139.9 / 20)
    assert row["market_cap"] == pytest.approx(139.9 * 7.4e9)
    assert 0 <= row["pos_52w"] <= 1 and len(row["spark"]) > 100
    bad = ov.loc["BAD"]  # keine Daten -> alles leer, kein Absturz
    assert pd.isna(bad["price"]) and pd.isna(bad["pe"])


def test_events_and_mark_seen(session):
    c = session.scalar(select(Company).where(Company.ticker == "MSFT"))
    session.add(Event(company_id=c.id, kind="new_filing", severity=2, title="Neues Filing: 10-Q"))
    session.commit()
    ev = queries.events(session, only_unseen=True)
    assert len(ev) == 1
    queries.mark_events_seen(session, [int(ev["id"].iloc[0])])
    assert queries.events(session, only_unseen=True).empty


def test_dashboard_smoke_all_pages(tmp_path, monkeypatch):
    """Startet die App headless, durchläuft alle Seiten mit Daten und einer leeren DB."""
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.cache_resource.clear()  # App cached die DB-Verbindung prozessweit
    app = os.path.join(os.path.dirname(__file__), "..", "src", "invest_research", "dashboard", "app.py")
    db = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    engine = create_engine(f"sqlite:///{db}")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software")])
        seed(s)
    for page in ("Übersicht", "Peer-Vergleich", "Unternehmen", "Alerts"):
        at = AppTest.from_file(app, default_timeout=30).run()
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, f"{page}: {at.exception}"
    # leere DB: Hinweis statt Absturz
    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "leer.db"))
    at = AppTest.from_file(app, default_timeout=30).run()
    assert not at.exception and any("update" in i.value for i in at.info)
