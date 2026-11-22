import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.edgar import EdgarClient, EdgarError, parse_filings
from invest_research.facts import parse_facts
from invest_research.ingest import ingest_watchlist
from invest_research.models import Event, Filing, Metric

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "msft_facts_small.json").read_text())


def submissions(extra: list[tuple[str, str, str]] = ()):
    rows = [("0001193125-26-436133", "10-Q", "2026-10-28"),
            ("0001193125-26-323660", "10-K", "2026-07-29"),
            ("0001193125-26-427849", "DEF 14A", "2026-10-21"),
            ("0001193125-10-000001", "10-K", "2010-07-29"), *extra]
    return {
        "cik": "789019", "name": "MICROSOFT CORP",
        "filings": {"recent": {
            "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
            "filingDate": [r[2] for r in rows], "reportDate": ["2026-09-30"] * len(rows),
            "primaryDocument": ["doc.htm"] * len(rows)}},
    }


def make_client(subs: dict) -> EdgarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "Test test@example.com"
        if "/submissions/" in request.url.path:
            return httpx.Response(200, json=subs)
        if "/companyfacts/" in request.url.path:
            return httpx.Response(200, json=FACTS)
        return httpx.Response(404)
    return EdgarClient("Test test@example.com", min_interval=0,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))


def by_key(rows):
    return {(r.name, r.period_end, r.period_type): r for r in rows}


def test_empty_user_agent_is_rejected():
    with pytest.raises(EdgarError, match="SEC_USER_AGENT"):
        EdgarClient("  ")


def test_parse_facts_annual_revenue_and_tag_merge():
    rows = by_key(parse_facts(FACTS, since_year=2024))
    fy = rows[("revenue", date(2026, 6, 30), "FY")]
    assert fy.value == 331_839_000_000
    # Quartalswert (Q1 FY27) vorhanden, 6-/9-Monats-Kumulierungen nicht
    assert ("revenue", date(2026, 9, 30), "Q") in rows
    assert all(r.period_type in {"FY", "Q", "instant"} for r in rows.values())


def test_parse_facts_since_year_filters_old_and_ignores_other_forms():
    rows = parse_facts(FACTS, since_year=2024)
    assert min(r.period_end.year for r in rows) >= 2024  # alter 'Revenues'-Tag (2010) raus
    # NetIncome aus DEF 14A (end 2026-06-30) darf nicht ueber den 10-K-Wert gewinnen
    ni = by_key(rows)[("net_income", date(2026, 6, 30), "FY")]
    assert ni.value > 0


def test_parse_facts_balance_sheet_is_instant_and_shares():
    rows = by_key(parse_facts(FACTS, since_year=2024))
    assert rows[("total_assets", date(2026, 9, 30), "instant")].value == 779_403_000_000
    assert ("shares_outstanding", date(2026, 10, 22), "instant") in rows


def test_parse_filings_filters_forms_and_year():
    out = parse_filings(submissions(), {"10-K", "10-Q"}, since_year=2024)
    assert {f.form_type for f in out} == {"10-K", "10-Q"}
    assert len(out) == 2  # DEF 14A und 10-K von 2010 fallen raus
    assert out[0].url.endswith("/789019/000119312526436133/doc.htm")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software", cik="0000789019")])
        yield s


def test_ingest_is_idempotent_and_alerts_only_on_new_filings(session):
    first = ingest_watchlist(session, make_client(submissions()), since_year=2024)[0]
    assert first.error is None and first.new_filings == 2 and first.metrics_written > 20
    assert session.scalars(select(Event)).all() == []  # Erstladen: keine Alerts

    again = ingest_watchlist(session, make_client(submissions()), since_year=2024)[0]
    assert again.new_filings == 0 and again.metrics_written == 0
    n_metrics = len(session.scalars(select(Metric)).all())

    newer = submissions([("0001193125-26-999999", "8-K", "2026-11-05")])
    third = ingest_watchlist(session, make_client(newer), since_year=2024)[0]
    assert third.new_filings == 1
    events = session.scalars(select(Event)).all()
    assert len(events) == 1 and events[0].kind == "new_filing" and "8-K" in events[0].title
    assert len(session.scalars(select(Metric)).all()) == n_metrics
    assert len(session.scalars(select(Filing)).all()) == 3


def test_ingest_error_is_reported_not_raised(session):
    def handler(request):
        return httpx.Response(404)
    client = EdgarClient("Test test@example.com", min_interval=0, max_retries=0,
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = ingest_watchlist(session, client, since_year=2024)[0]
    assert res.error and "404" in res.error
