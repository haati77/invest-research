import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from invest_research.compute import compute_company
from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.engine import (compute_derived, derive_q4, derive_quarters_from_ytd,
                                    derive_total_liabilities, derive_ttm)
from invest_research.facts import MetricRow, parse_facts
from invest_research.models import Company, Metric
from invest_research.peers import add_percentiles, overall_score, snapshot

FACTS = json.loads((Path(__file__).parent / "fixtures" / "msft_facts_small.json").read_text())


def R(name, value, end, ptype, unit="USD"):
    return MetricRow(name, float(value), unit, end, ptype)


def by_key(rows):
    return {(r.name, r.period_type, r.period_end): r.value for r in rows}


# Geschäftsjahr endet 31.12.2024; Quartale 31.03., 30.06., 30.09.
FY24, Q1, Q2, Q3 = date(2024, 12, 31), date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30)


def test_derive_q4_is_fy_minus_three_quarters():
    base = [R("revenue", 400, FY24, "FY"), R("revenue", 90, Q1, "Q"),
            R("revenue", 100, Q2, "Q"), R("revenue", 100, Q3, "Q")]
    assert by_key(derive_q4(base))[("revenue", "Q", FY24)] == 110


def test_cash_flow_quarters_from_cumulative_values():
    # 10-Q melden Cashflow kumuliert: Q1=10 (3M), H1=25, 9M=45, FY=70
    base = [R("operating_cash_flow", 10, Q1, "Q"), R("operating_cash_flow", 25, Q2, "H1"),
            R("operating_cash_flow", 45, Q3, "9M"), R("operating_cash_flow", 70, FY24, "FY")]
    v = by_key(compute_derived(base))
    assert v[("operating_cash_flow", "Q", Q2)] == 15
    assert v[("operating_cash_flow", "Q", Q3)] == 20
    assert v[("operating_cash_flow", "Q", FY24)] == 25
    assert v[("operating_cash_flow", "TTM", FY24)] == 70  # Q1..Q4 lückenlos
    assert derive_quarters_from_ytd([R("operating_cash_flow", 25, Q2, "H1")]) == []  # ohne Q1 nichts


def test_derive_q4_needs_exactly_three_quarters():
    base = [R("revenue", 400, FY24, "FY"), R("revenue", 90, Q1, "Q"), R("revenue", 100, Q2, "Q")]
    assert derive_q4(base) == []


def test_ttm_requires_four_consecutive_quarters():
    q = [R("revenue", v, e, "Q") for v, e in
         [(90, Q1), (100, Q2), (100, Q3), (110, FY24)]]
    assert by_key(derive_ttm(q))[("revenue", "TTM", FY24)] == 400
    gap = [q[0], q[1], q[3]]  # Q3 fehlt -> keine TTM
    assert derive_ttm(gap) == []


def test_total_liabilities_derived_only_when_missing():
    base = [R("total_assets", 1000, FY24, "instant"), R("equity", 400, FY24, "instant")]
    assert by_key(derive_total_liabilities(base))[("total_liabilities", "instant", FY24)] == 600
    base.append(R("total_liabilities", 650, FY24, "instant"))  # Rohwert hat Vorrang
    assert derive_total_liabilities(base) == []


def test_ratios_and_growth():
    prev, cur = date(2023, 12, 31), FY24
    base = [R("revenue", 800, prev, "FY"), R("revenue", 1000, cur, "FY"),
            R("gross_profit", 600, cur, "FY"), R("operating_income", 250, cur, "FY"),
            R("net_income", 100, prev, "FY"), R("net_income", 200, cur, "FY"),
            R("operating_cash_flow", 300, cur, "FY"), R("capex", 100, cur, "FY"),
            R("total_assets", 2000, cur, "instant"), R("equity", 800, cur, "instant"),
            R("total_liabilities", 1200, cur, "instant"),
            R("long_term_debt", 500, cur, "instant"), R("cash", 150, cur, "instant")]
    v = by_key(compute_derived(base))
    assert v[("gross_margin", "FY", cur)] == pytest.approx(0.6)
    assert v[("operating_margin", "FY", cur)] == pytest.approx(0.25)
    assert v[("net_margin", "FY", cur)] == pytest.approx(0.2)
    assert v[("fcf", "FY", cur)] == 200
    assert v[("fcf_margin", "FY", cur)] == pytest.approx(0.2)
    assert v[("roe", "FY", cur)] == pytest.approx(0.25)
    assert v[("roa", "FY", cur)] == pytest.approx(0.1)
    assert v[("debt_to_equity", "FY", cur)] == pytest.approx(1.5)
    assert v[("net_debt", "FY", cur)] == 350
    assert v[("revenue_growth", "FY", cur)] == pytest.approx(0.25)
    assert v[("net_income_growth", "FY", cur)] == pytest.approx(1.0)


def test_ratios_guard_against_misleading_values():
    prev, cur = date(2023, 12, 31), FY24
    base = [R("revenue", 800, prev, "FY"), R("revenue", 1000, cur, "FY"),
            R("net_income", -50, prev, "FY"), R("net_income", 200, cur, "FY"),
            R("equity", -100, cur, "instant"), R("total_liabilities", 900, cur, "instant")]
    v = by_key(compute_derived(base))
    assert ("net_income_growth", "FY", cur) not in v  # Basis negativ -> kein Wachstum
    assert ("roe", "FY", cur) not in v                # negatives Eigenkapital
    assert ("debt_to_equity", "FY", cur) not in v
    assert ("gross_margin", "FY", cur) not in v       # kein Bruttogewinn -> nichts erfunden


def test_real_microsoft_data_q4_and_ttm_plausible():
    rows = parse_facts(FACTS, since_year=2024)
    v = by_key(rows + compute_derived(rows))
    fy26 = v[("revenue", "FY", date(2026, 6, 30))]
    q4 = v[("revenue", "Q", date(2026, 6, 30))]
    assert 70e9 < q4 < 100e9 and q4 < fy26
    assert ("total_liabilities", "instant", date(2026, 9, 30)) in v  # Fixture ohne Liabilities-Tag
    ttm = v[("revenue", "TTM", date(2026, 9, 30))]
    assert 300e9 < ttm < 400e9
    assert 0.6 < v[("gross_margin", "FY", date(2026, 6, 30))] < 0.8


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software")])
        yield s


def test_compute_company_is_idempotent_and_keeps_raw_values(session):
    c = session.scalar(select(Company))
    for r in parse_facts(FACTS, since_year=2024):
        session.add(Metric(company_id=c.id, name=r.name, value=r.value, unit=r.unit,
                           period_end=r.period_end, period_type=r.period_type, source="xbrl"))
    session.commit()
    raw_before = {(m.name, m.period_end, m.period_type): m.value
                  for m in session.scalars(select(Metric).where(Metric.source == "xbrl"))}
    assert compute_company(session, c) > 0
    assert compute_company(session, c) == 0
    raw_after = {(m.name, m.period_end, m.period_type): m.value
                 for m in session.scalars(select(Metric).where(Metric.source == "xbrl"))}
    assert raw_before == raw_after


def test_percentiles_within_sector_and_direction():
    df = pd.DataFrame({
        "sector": ["A", "A", "A", "B"],
        "net_margin": [0.30, 0.20, 0.10, 0.50],
        "debt_to_equity": [0.5, 1.0, 2.0, 3.0],  # niedriger ist besser
    }, index=["X", "Y", "Z", "W"])
    out = add_percentiles(df)
    assert out.loc["X", "pct_net_margin"] == 1.0 and out.loc["Z", "pct_net_margin"] == 0.0
    assert out.loc["X", "pct_debt_to_equity"] == 1.0 and out.loc["Z", "pct_debt_to_equity"] == 0.0
    assert pd.isna(out.loc["W", "pct_net_margin"])      # Einzelner in der Branche -> kein Rang
    assert out.loc["W", "pctall_net_margin"] == 1.0     # aber im Gesamtvergleich bester
    assert overall_score(out).loc["X"] == 1.0


def test_snapshot_picks_latest_period(session):
    c = session.scalar(select(Company))
    for end, rev in ((date(2025, 6, 30), 100.0), (date(2026, 6, 30), 120.0)):
        session.add(Metric(company_id=c.id, name="revenue", value=rev, unit="USD",
                           period_end=end, period_type="FY", source="xbrl"))
    session.add(Metric(company_id=c.id, name="gross_margin", value=0.7, unit="ratio",
                       period_end=date(2026, 6, 30), period_type="FY", source="computed"))
    session.add(Metric(company_id=c.id, name="gross_margin", value=0.1, unit="ratio",
                       period_end=date(2025, 6, 30), period_type="FY", source="computed"))
    session.commit()
    df = snapshot(session, "FY")
    assert df.loc["MSFT", "period_end"] == date(2026, 6, 30)
    assert df.loc["MSFT", "gross_margin"] == 0.7
    with pytest.raises(ValueError):
        snapshot(session, "XYZ")
