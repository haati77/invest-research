import json
import os
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from invest_research.ask import (CATALOG, Cond, Plan, PlanError, ask, build_prompt, fmt, plan_question,
                                 question_table, run_plan, validate_plan)
from invest_research.compute import compute_company
from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.facts import parse_facts
from invest_research.llm import LLMError, LLMResponse
from invest_research.models import Analysis, Company, Filing, Metric, Price

FACTS = json.loads(open(os.path.join(os.path.dirname(__file__), "fixtures", "msft_facts_small.json")).read())


def frame():
    """Handgebaute Tabelle: A hat alles, B fehlt die Marge (NaN), C ist in einer anderen Branche."""
    df = pd.DataFrame(index=["AAA", "BBB", "CCC"], columns=list(CATALOG), dtype=object).astype(object)
    df = df.fillna(float("nan"))
    df["name"] = ["Alpha", "Beta", "Gamma"]
    df["sector"] = ["Software", "Software", "Konsum"]
    df["ttm_net_margin"] = [0.30, float("nan"), 0.10]
    df["chg_debt_to_equity"] = [0.5, 0.2, -0.3]
    df["pe"] = [20.0, 10.0, 30.0]
    df["chg_1m"] = [-0.10, 0.02, -0.30]
    for c in ("ttm_net_margin", "chg_debt_to_equity", "pe", "chg_1m"):
        df[c] = df[c].astype(float)
    return df


def plan(**kw):
    base = {"kind": "table", "columns": [], "filters": [], "sort_by": None, "descending": True, "limit": 10,
            "tickers": [], "text_section": None, "note": None}
    return Plan.model_validate({**base, **kw})


TICKERS = {"AAA", "BBB", "CCC"}


# ------------------------------------------------------------ Plan-Pruefung
def test_validate_plan_rejects_unknown_columns_tickers_and_type_mismatch():
    with pytest.raises(PlanError, match="Unbekannte Spalte"):
        validate_plan(plan(columns=["geheim; DROP TABLE"]), TICKERS)
    with pytest.raises(PlanError, match="Unbekannte Spalte"):
        validate_plan(plan(sort_by="gibt_es_nicht"), TICKERS)
    with pytest.raises(PlanError, match="Unbekannte Ticker"):
        validate_plan(plan(tickers=["TSLA"]), TICKERS)
    with pytest.raises(PlanError, match="numerisch"):
        validate_plan(plan(filters=[Cond(column="pe", op=">", value="hoch")]), TICKERS)
    with pytest.raises(PlanError, match="Text"):
        validate_plan(plan(filters=[Cond(column="sector", op=">", value="Software")]), TICKERS)
    with pytest.raises(PlanError, match="genau einen Ticker"):
        validate_plan(plan(kind="text", tickers=["AAA", "BBB"], text_section="risks"), TICKERS)
    assert validate_plan(plan(tickers=["aaa"]), TICKERS).tickers == ["AAA"]      # Gross-/Kleinschreibung egal
    assert validate_plan(plan(kind="unsupported", note="nein"), TICKERS).kind == "unsupported"


def test_limit_is_clamped():
    assert plan(limit=9999).limit == 50 and plan(limit=0).limit == 1 and plan(limit="abc").limit == 10


# ------------------------------------------------------------ Ausfuehrung
def test_filters_combine_with_and_and_missing_data_is_reported_not_guessed():
    r = run_plan(frame(), plan(filters=[Cond(column="ttm_net_margin", op=">", value=0.05),
                                        Cond(column="chg_debt_to_equity", op=">", value=0)]))
    assert list(r.raw.index) == ["AAA"]                                   # BBB: keine Marge -> nicht geraten
    assert any("BBB" in n and "Nettomarge" in n for n in r.notes)
    assert "UND" in r.interpreted and "ueber 5.0%" in r.interpreted and "ueber +0.0 Pp" not in r.interpreted


def test_text_filter_sort_limit_and_overflow_note():
    r = run_plan(frame(), plan(filters=[Cond(column="sector", op="==", value="Software")], sort_by="pe",
                               descending=False, limit=1))
    assert list(r.raw.index) == ["BBB"]                                   # kleinstes KGV in Software
    assert any("Treffer" in n and "der erste wird" in n for n in r.notes)
    assert "hoechstens 1 Zeile" in r.interpreted and "aufsteigend" in r.interpreted


def test_no_match_gives_empty_table_with_note():
    r = run_plan(frame(), plan(filters=[Cond(column="pe", op=">", value=1000)]))
    assert r.raw.empty and any("Kein Unternehmen" in n for n in r.notes)


def test_ticker_restriction_and_column_display_include_filter_columns():
    r = run_plan(frame(), plan(tickers=["CCC", "AAA"], columns=["pe"], filters=[Cond(column="chg_1m", op="<", value=0)]))
    assert set(r.raw.index) == {"AAA", "CCC"}
    assert list(r.table.columns) == ["Name", "KGV", "Kurs 1 Monat"]       # Filterspalte wird mit angezeigt
    assert r.table.loc["CCC", "Kurs 1 Monat"] == "-30.0%" and r.table.index.name == "Ticker"


def test_unsupported_returns_reason():
    r = run_plan(frame(), plan(kind="unsupported", note="Prognosen sind nicht moeglich."))
    assert r.table is None and r.notes == ["Prognosen sind nicht moeglich."]


def test_formatting_kinds():
    assert fmt("pct", 0.123) == "12.3%" and fmt("pp", -0.031) == "-3.1 Pp" and fmt("x", 1.5) == "1.50x"
    assert fmt("usd", 3.6e12) == "3.60 Bio. $" and fmt("usd", 2.5e9) == "2.5 Mrd. $" and fmt("price", 9.5) == "9.50 $"
    assert fmt("score", 0.682) == "68 von 100" and fmt("pct", float("nan")) == "-" and fmt("pct", None) == "-"


# ------------------------------------------------------------ KI-Teil mit Attrappe
class FakeLLM:
    model = "fake"

    def __init__(self, replies):
        self.replies, self.calls, self.prompts = list(replies), 0, []

    def complete(self, system, prompt):
        self.calls += 1
        self.prompts.append((system, prompt))
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return LLMResponse(text=r if isinstance(r, str) else json.dumps(r), model="fake")


def test_prompt_contains_catalog_and_names_but_no_data_values():
    df = frame()
    df["price"] = 123456.789
    prompt = build_prompt(df, "Welche Titel sind billig?", today=date(2026, 11, 22))
    assert "ttm_net_margin" in prompt and "AAA = Alpha" in prompt and "Software" in prompt and "2026-11-22" in prompt
    assert "123456" not in prompt and "0.3" not in prompt.split("Columns")[0]   # keine Datenwerte im Prompt


def test_plan_question_retries_once_with_error_hint_and_gives_up():
    good = {"kind": "table", "columns": ["pe"], "filters": [], "sort_by": "pe", "descending": False, "limit": 3}
    llm = FakeLLM([{"kind": "table", "columns": ["erfunden"]}, good])
    p = plan_question(llm, frame(), "billig?")
    assert llm.calls == 2 and p.sort_by == "pe" and "Unbekannte Spalte" in llm.prompts[1][1]
    with pytest.raises(LLMError, match="keinen gueltigen Abfrageplan"):
        plan_question(FakeLLM(["kein json"]), frame(), "x")


# ------------------------------------------------------------ Integration mit echten Daten (MSFT-Fixture)
@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software")])
        c = s.scalar(select(Company))
        c.name = "MICROSOFT CORP"
        for r in parse_facts(FACTS, since_year=2024):
            s.add(Metric(company_id=c.id, name=r.name, value=r.value, unit=r.unit, period_end=r.period_end,
                         period_type=r.period_type, source="xbrl"))
        s.commit()
        compute_company(s, c)
        for i in range(60):
            s.add(Price(company_id=c.id, date=date(2026, 11, 20) - timedelta(days=59 - i), close=480.0 + i))
        s.add(Filing(company_id=c.id, accession_number="a-k", form_type="10-K", filed_at=date(2026, 7, 29), url="http://x"))
        s.commit()
        yield s


def test_question_table_has_every_catalog_column_and_real_values(session):
    df = question_table(session)
    assert set(CATALOG) <= set(df.columns)
    row = df.loc["MSFT"]
    assert row["price"] == 539.0 and row["fy_revenue"] == 331_839_000_000 and 0.35 < row["ttm_net_margin"] < 0.45
    assert pd.notna(row["chg_net_margin"])                               # Jahresvergleich aus den TTM-Werten


def test_ask_end_to_end_numbers_come_from_database_not_from_ai(session):
    reply = {"kind": "table", "columns": ["ttm_net_margin", "market_cap"],
             "filters": [{"column": "ttm_net_margin", "op": ">", "value": 0.2}], "sort_by": "ttm_net_margin"}
    llm = FakeLLM([reply])
    r = ask(session, llm, "Welche Titel haben mehr als 20 Prozent Nettomarge?")
    assert list(r.raw.index) == ["MSFT"] and llm.calls == 1
    expected = session.scalar(select(Metric.value).where(Metric.name == "net_margin", Metric.period_type == "TTM")
                              .order_by(Metric.period_end.desc()).limit(1))
    assert r.table.loc["MSFT", "Nettomarge (letzte 12 Monate)"] == f"{expected * 100:.1f}%"
    assert "Nettomarge (letzte 12 Monate) ueber 20.0%" in r.interpreted


def test_text_question_returns_only_verified_items(session):
    f = session.scalar(select(Filing))
    items = [{"kind": "risk", "title": "Echt", "statement": "S", "quote": "q" * 40, "importance": 3, "verified": True},
             {"kind": "risk", "title": "ERFUNDEN", "statement": "S", "quote": "z" * 40, "importance": 3, "verified": False}]
    session.add(Analysis(filing_id=f.id, section_key="risks", prompt_version="v2", model="haiku",
                         content={"summary": "Zusammenfassung", "items": items, "n_items": 2, "n_verified": 1}))
    session.commit()
    r = ask(session, FakeLLM([{"kind": "text", "tickers": ["msft"], "text_section": "risks"}]), "Welche Risiken nennt MSFT?")
    assert [i["title"] for i in r.text["items"]] == ["Echt"] and "10-K vom 2026-07-29" in r.interpreted
    r2 = ask(session, FakeLLM([{"kind": "text", "tickers": ["MSFT"], "text_section": "mdna"}]), "Management?")
    assert r2.text is None and "analyze" in r2.notes[0]


def test_ask_without_data_or_question(session):
    with pytest.raises(ValueError):
        ask(session, FakeLLM([{}]), "   ")
    empty = create_engine("sqlite:///:memory:")
    init_db(empty)
    with Session(empty) as s, pytest.raises(LLMError, match="Noch keine Daten"):
        ask(s, FakeLLM([{}]), "Frage")


def test_dashboard_ask_page(tmp_path, monkeypatch, session):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    db = tmp_path / "q.db"
    eng = create_engine(f"sqlite:///{db}")
    init_db(eng)
    with Session(eng) as dst:
        for model in (Company, Filing, Metric, Price):
            for row in session.scalars(select(model)):
                dst.add(model(**{c.name: getattr(row, c.name) for c in model.__table__.columns}))
        dst.commit()
    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    reply = {"kind": "table", "columns": ["pe"], "filters": [], "sort_by": "pe", "descending": False, "limit": 5}
    monkeypatch.setattr("invest_research.usage.ClaudeCLI", lambda model="haiku": FakeLLM([reply]))
    app = os.path.join(os.path.dirname(__file__), "..", "src", "invest_research", "dashboard", "app.py")
    at = AppTest.from_file(app, default_timeout=60).run()
    at.sidebar.radio[0].set_value("Fragen").run()
    assert not at.exception
    at.text_input[0].input("Welche Titel sind am billigsten?")
    at.button[-1].click().run()                                            # 'Fragen' (Formular-Button)
    assert not at.exception
    assert any("Verstanden als" in i.value for i in at.info)
    assert len(at.dataframe) == 1 and "KGV" in at.dataframe[0].value.columns
