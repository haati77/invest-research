import json
import os
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from invest_research.analysis import PROMPT_VERSION
from invest_research.compute import compute_company
from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.facts import parse_facts
from invest_research.llm import LLMError, LLMResponse
from invest_research.models import Analysis, Company, Filing, Metric, Price, Profile
from invest_research.profiles import (Point, build_profiles, check_point, render_markdown, synthesize)

FACTS = json.loads(open(os.path.join(os.path.dirname(__file__), "fixtures", "msft_facts_small.json")).read())
SHEET = {"F:revenue_growth:FY": "Umsatzwachstum (letztes Geschäftsjahr, Stichtag 2026-06-30): 17.8%",
         "F:net_margin:FY": "Nettomarge (letztes Geschäftsjahr, Stichtag 2026-06-30): 40.3%",
         "V:pe": "KGV (Kurs 2026-11-20, Gewinn letzte 12 Monate): 25.3x"}


# ------------------------------------------------------------ Beleg-Prüfung
def test_check_point_accepts_exact_and_german_formatted_numbers():
    assert check_point(Point(statement="Das Wachstum lag bei 17,8 % im Geschäftsjahr 2026.",
                             evidence=["F:revenue_growth:FY"]), SHEET) is None
    assert check_point(Point(statement="Drei von 10 Kennzahlen", evidence=["V:pe"]), SHEET) is None  # kleine Ganzzahlen frei


@pytest.mark.parametrize("statement,evidence,reason_part", [
    ("Wachstum von 25 %.", ["F:revenue_growth:FY"], "Zahl nicht in den Belegen: 25"),
    ("Starkes Wachstum.", [], "keine Belege"),
    ("Starkes Wachstum.", ["F:gibt_es_nicht"], "unbekannte Beleg-ID"),
    ("Die Marge von 40.3% und KGV 25.3x", ["F:net_margin:FY"], "Zahl nicht in den Belegen: 25.3"),  # Zahl aus anderem Beleg
    ("Eigene Rechnung: 17.8 plus 40.3 sind 58.1", ["F:revenue_growth:FY", "F:net_margin:FY"], "58.1"),
])
def test_check_point_rejects(statement, evidence, reason_part):
    assert reason_part in check_point(Point(statement=statement, evidence=evidence), SHEET)


class FakeLLM:
    model = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, system, prompt):
        self.calls += 1
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return LLMResponse(text=r if isinstance(r, str) else json.dumps(r), model="fake", cost_usd=0.001)


def company():
    return Company(ticker="MSFT", name="MICROSOFT CORP", sector="Software")


def test_synthesize_drops_unsupported_points_with_reasons_and_retries_bad_json():
    good = {"statement": "Wachstum 17.8%.", "evidence": ["F:revenue_growth:FY"]}
    bad = {"statement": "Wachstum 99%.", "evidence": ["F:revenue_growth:FY"]}
    reply = {"headline": "Kopf", "strengths": [good, bad], "weaknesses": [], "watch": [],
             "open_questions": ["Frage?"]}
    llm = FakeLLM(["kein json", reply])
    out = synthesize(llm, company(), SHEET)
    assert llm.calls == 2
    assert [p["statement"] for p in out["strengths"]] == ["Wachstum 17.8%."]
    assert out["dropped"] == 1 and "99" in out["dropped_points"][0]["reason"]
    with pytest.raises(LLMError):
        synthesize(FakeLLM(["nein", "nein"]), company(), SHEET)


# ------------------------------------------------------------ Ende-zu-Ende
def analysis_content(verified_title="Echte Aussage"):
    items = [{"kind": "segment", "title": verified_title, "statement": "Drei Segmente.", "quote": "q" * 40,
              "importance": 3, "verified": True},
             {"kind": "risk", "title": "ERFUNDEN", "statement": "x", "quote": "z" * 40, "importance": 3,
              "verified": False}]
    return {"summary": "Zusammenfassung.", "items": items, "tone": None, "tone_verified": False,
            "tone_quote": None, "truncated": False, "n_items": 2, "n_verified": 1, "low_quality": False}


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software"),
                           WatchlistEntry(ticker="ORCL", sector="Software")])
        c = s.scalar(select(Company).where(Company.ticker == "MSFT"))
        c.name = "MICROSOFT CORP"
        for r in parse_facts(FACTS, since_year=2024):
            s.add(Metric(company_id=c.id, name=r.name, value=r.value, unit=r.unit, period_end=r.period_end,
                         period_type=r.period_type, source="xbrl"))
        s.commit()
        compute_company(s, c)
        s.add(Price(company_id=c.id, date=date(2026, 11, 20), close=480.0))
        s.add(Filing(company_id=c.id, accession_number="a-k", form_type="10-K", filed_at=date(2026, 7, 29),
                     url="http://x/k"))
        s.commit()
        yield s


def add_analysis(session, filing_acc="a-k", section="business", title="Echte Aussage"):
    f = session.scalar(select(Filing).where(Filing.accession_number == filing_acc))
    session.add(Analysis(filing_id=f.id, section_key=section, prompt_version=PROMPT_VERSION, model="haiku",
                         content=analysis_content(title)))
    session.commit()


def versions(session):
    return session.scalars(select(Profile).order_by(Profile.id)).all()


def synth_reply(session):
    sheet = versions(session)[-1].content["sheet"]
    rg = sheet["F:revenue_growth:FY"].rsplit(": ", 1)[1]
    return {"headline": "Kurzbild", "open_questions": ["Wie nachhaltig?"], "watch": [],
            "strengths": [{"statement": f"Wachstum {rg}.", "evidence": ["F:revenue_growth:FY"]}],
            "weaknesses": [{"statement": "Erfundene Zahl 99.9%.", "evidence": ["F:revenue_growth:FY"]}]}


def test_profile_without_ai_has_facts_gaps_and_is_idempotent(session):
    res = build_profiles(session, None, only_ticker="MSFT")
    assert res[0].status == "neu" and not res[0].ai
    p = versions(session)[0]
    c = p.content
    assert p.model is None and c["synthesis"] is None
    assert c["facts"]["fy"]["values"]["revenue"] == 331_839_000_000
    assert "F:revenue_growth:FY" in c["sheet"] and "V:pe" in c["sheet"]
    assert any("Bruttomarge" in g or "KI-Berichtsanalyse" in g for g in c["gaps"])
    assert build_profiles(session, None, only_ticker="MSFT")[0].status == "unverändert"
    assert len(versions(session)) == 1


def test_profile_with_ai_new_version_then_stable_then_force(session):
    add_analysis(session)
    build_profiles(session, None, only_ticker="MSFT")                       # Profil ohne KI
    llm = FakeLLM([synth_reply(session)])
    r = build_profiles(session, llm, only_ticker="MSFT")[0]
    assert r.status == "neu" and r.ai and r.dropped == 1 and llm.calls == 1  # Version mit KI (Eingaben gleich!)
    latest = versions(session)[-1]
    assert latest.model == "fake" and len(versions(session)) == 2
    assert [p["statement"][:7] for p in latest.content["synthesis"]["strengths"]] == ["Wachstu"]
    assert latest.content["synthesis"]["dropped_points"][0]["reason"].startswith("Zahl nicht")
    assert build_profiles(session, llm, only_ticker="MSFT")[0].status == "unverändert" and llm.calls == 1
    assert build_profiles(session, llm, only_ticker="MSFT", force=True)[0].status == "neu"
    assert len(versions(session)) == 3


def test_only_verified_analysis_items_enter_the_evidence(session):
    add_analysis(session)
    build_profiles(session, None, only_ticker="MSFT")
    c = versions(session)[0].content
    titles = [i["title"] for i in c["business"]["items"]]
    assert titles == ["Echte Aussage"]
    assert not any("ERFUNDEN" in v for v in c["sheet"].values())


def test_new_analysis_triggers_new_version(session):
    add_analysis(session)
    build_profiles(session, None, only_ticker="MSFT")
    add_analysis(session, section="risks")
    assert build_profiles(session, None, only_ticker="MSFT")[0].status == "neu"
    assert len(versions(session)) == 2


def test_no_analysis_means_no_llm_call(session):
    llm = FakeLLM([{}])
    r = build_profiles(session, llm, only_ticker="MSFT")[0]
    assert r.status == "neu" and not r.ai and llm.calls == 0


def test_llm_error_stores_nothing_and_isolated(session):
    add_analysis(session)
    r = build_profiles(session, FakeLLM([LLMError("Limit erreicht")]), only_ticker="MSFT")[0]
    assert r.status.startswith("FEHLER") and "Limit erreicht" in r.status and versions(session) == []


def test_company_without_data_reports_error_not_crash(session):
    res = {r.ticker: r.status for r in build_profiles(session, None)}
    assert res["MSFT"] == "neu" and res["ORCL"].startswith("FEHLER: keine Daten")


def test_markdown_contains_core_parts(session):
    add_analysis(session)
    build_profiles(session, None, only_ticker="MSFT")
    build_profiles(session, FakeLLM([synth_reply(session)]), only_ticker="MSFT")
    md = render_markdown(versions(session)[-1].content)
    for part in ("# MICROSOFT CORP (MSFT)", "Kurzbild", "## Kennzahlen", "17.8%", "## Stärken",
                 "## Offene Fragen", "## Datenlücken", "keine Anlageberatung"):
        assert part in md, part
    assert "99.9" not in md  # verworfene Aussage taucht nicht auf


# ------------------------------------------------------------ Migration und Dashboard
def test_migration_adds_missing_profile_columns_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alt.db'}")
    with engine.begin() as conn:  # Zustand nach Schritt 1: Profiltabelle ohne neue Spalten
        conn.execute(text("CREATE TABLE profiles (id INTEGER PRIMARY KEY, company_id INTEGER, created_at DATETIME, "
                          "content JSON, based_on_filing_id INTEGER)"))
    init_db(engine)
    init_db(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("profiles")}
    assert {"input_hash", "model", "prompt_version"} <= cols


def test_dashboard_profile_page(tmp_path, monkeypatch, session):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    add_analysis(session)
    build_profiles(session, None, only_ticker="MSFT")
    build_profiles(session, FakeLLM([synth_reply(session)]), only_ticker="MSFT")
    db = tmp_path / "p.db"
    eng = create_engine(f"sqlite:///{db}")
    init_db(eng)
    with Session(eng) as dst:
        for model in (Company, Filing, Profile):
            for row in session.scalars(select(model)):
                dst.add(model(**{c.name: getattr(row, c.name) for c in model.__table__.columns}))
        dst.commit()
    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    app = os.path.join(os.path.dirname(__file__), "..", "src", "invest_research", "dashboard", "app.py")
    at = AppTest.from_file(app, default_timeout=30).run()
    at.sidebar.radio[0].set_value("Profil").run()
    assert not at.exception
    page = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "Kurzbild" in page and "Verworfene KI-Aussagen" in " ".join(e.label for e in at.expander)
