import json
import os
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from invest_research.changes import (DIFF_SECTION, STALE_DAYS, Rule, METRIC_RULES, classify_change,
                                     detect_all_numeric, detect_metric_changes, detect_price_moves,
                                     detect_text_changes)
from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.llm import LLMError, LLMResponse
from invest_research.models import Analysis, Company, Event, Filing, Metric, Price
from invest_research.profiles import diff_profiles
from invest_research.textdiff import diff_sections, paragraphs

PP = Rule("pp", 3, 6, sign_flip=True)


# ------------------------------------------------------------ Klassifikation
def test_classify_thresholds_direction_and_flip():
    assert classify_change(PP, 0.30, 0.31) is None                       # 1 Pp: unerheblich
    assert classify_change(PP, 0.30, 0.25) == (2, "change", False)       # -5 Pp: Verschlechterung, mittel
    assert classify_change(PP, 0.30, 0.20) == (3, "change", False)       # -10 Pp: hoch
    assert classify_change(PP, 0.20, 0.30) == (2, "change", True)        # +10 Pp: hoch -> herabgestuft
    assert classify_change(PP, 0.20, 0.24) == (1, "change", True)        # +4 Pp: mittel -> niedrig
    assert classify_change(PP, 0.02, -0.01) == (3, "flip_neg", False)    # ins Negative gedreht, auch bei kleinem Delta
    assert classify_change(PP, -0.05, 0.01) == (2, "flip_pos", True)
    de = METRIC_RULES["debt_to_equity"]                                  # niedriger ist besser
    assert classify_change(de, 1.0, 1.5, higher_is_better=False) == (2, "change", False)
    assert classify_change(de, 1.5, 0.8, higher_is_better=False) == (2, "change", True)  # 0.7x -> hoch, herabgestuft


# ------------------------------------------------------------ Datenbank-Fixture
@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software")])
        s.scalar(select(Company)).name = "MICROSOFT CORP"
        s.commit()
        yield s


def comp(session):
    return session.scalar(select(Company))


def put_ttm(session, end, **values):
    c = comp(session)
    session.add(Metric(company_id=c.id, name="revenue", value=1.0, unit="USD", period_end=end,
                       period_type="TTM", source="computed"))
    for k, v in values.items():
        session.add(Metric(company_id=c.id, name=k, value=v, unit="ratio", period_end=end,
                           period_type="TTM", source="computed"))
    session.commit()


RECENT = date.today() - timedelta(days=30)
YEAR_BEFORE = RECENT - timedelta(days=364)


def events(session):
    return session.scalars(select(Event).order_by(Event.id)).all()


def test_metric_change_event_text_severity_and_idempotence(session):
    put_ttm(session, YEAR_BEFORE, net_margin=0.40, gross_margin=0.70, debt_to_equity=0.6)
    put_ttm(session, RECENT, net_margin=0.30, gross_margin=0.705, debt_to_equity=0.65)
    assert detect_metric_changes(session, comp(session)) == 1               # nur die Nettomarge
    ev = events(session)[0]
    assert ev.kind == "metric_change" and ev.severity == 3 and not ev.seen
    assert "Nettomarge: 40.0% -> 30.0%" in ev.title and "-10.0 Pp, schlechter" in ev.title
    assert ev.dedupe_key == f"metric:net_margin:TTM:{RECENT}"
    assert detect_metric_changes(session, comp(session)) == 0               # Wiederholung: nichts doppelt
    assert len(events(session)) == 1


def test_stale_period_is_marked_seen_automatically(session):
    old_now = date.today() - timedelta(days=STALE_DAYS + 40)
    put_ttm(session, old_now - timedelta(days=364), net_margin=0.40)
    put_ttm(session, old_now, net_margin=0.30)
    detect_metric_changes(session, comp(session))
    ev = events(session)[0]
    assert ev.seen is True and "automatisch als gelesen" in ev.detail


def test_metric_change_needs_prior_year_and_values(session):
    put_ttm(session, RECENT, net_margin=0.30)                               # kein Vorjahr
    assert detect_metric_changes(session, comp(session)) == 0
    put_ttm(session, YEAR_BEFORE, gross_margin=0.5)                         # Vorjahr ohne Nettomarge
    assert detect_metric_changes(session, comp(session)) == 0


def put_prices(session, closes):
    c = comp(session)
    start = date.today() - timedelta(days=len(closes))
    for i, v in enumerate(closes):
        session.add(Price(company_id=c.id, date=start + timedelta(days=i), close=v))
    session.commit()


def test_price_move_detection_direction_and_lookback(session):
    put_prices(session, [100, 100, 100, 100, 100, 100, 100, 100, 85, 85])   # -15 % vor 2 Tagen
    assert detect_price_moves(session, comp(session)) == 1
    ev = events(session)[0]
    assert ev.kind == "price_move" and ev.severity == 3 and "-15.0%" in ev.title
    assert detect_price_moves(session, comp(session)) == 0                  # idempotent


def test_price_rise_is_demoted_and_small_moves_ignored(session):
    put_prices(session, [100, 100, 100, 100, 100, 100, 100, 100, 104, 115.4])  # +4 % ignoriert, +11 % mittel->niedrig
    detect_price_moves(session, comp(session))
    evs = events(session)
    assert len(evs) == 1 and evs[0].severity == 1


def test_old_price_moves_outside_lookback_are_ignored(session):
    put_prices(session, [100, 70, 70, 70, 70, 70, 70, 70, 70, 70])           # Absturz liegt > 5 Tage zurueck
    assert detect_price_moves(session, comp(session)) == 0


def test_detect_all_numeric_counts(session):
    put_ttm(session, YEAR_BEFORE, net_margin=0.40)
    put_ttm(session, RECENT, net_margin=0.30)
    put_prices(session, [100] * 8 + [85, 85])
    assert detect_all_numeric(session) == {"metric_change": 1, "price_move": 1}
    assert detect_all_numeric(session) == {"metric_change": 0, "price_move": 0}


# ------------------------------------------------------------ Textvergleich
def para(i, n=25):
    return " ".join(f"term{i}x{j}" for j in range(n))  # eindeutige Woerter je Absatz, ~200 Zeichen


def test_diff_sections_ignores_reordering_and_short_lines_but_finds_changes():
    old = "\n".join(para(i) for i in range(10))
    reordered = "\n".join(para(i) for i in reversed(range(10)))
    assert diff_sections(old, reordered).added == [] and diff_sections(old, reordered).removed == []
    new = "\n".join([para(i) for i in range(1, 10)] + [para(99), "Kurze Ueberschrift"])
    d = diff_sections(old, new)
    assert d.added == [para(99)] and d.removed == [para(0)]
    assert d.n_old == 10 and d.n_new == 10 and len(paragraphs("kurz\n" + para(1))) == 1


def test_diff_sections_partial_rewording_counts_as_changed():
    old = para(1, 40)
    reworded = " ".join(f"term1x{j}" for j in range(10)) + " " + " ".join(f"new{j}word" for j in range(30))
    assert len(diff_sections(old, reworded).added) == 1                      # unter 50 % Wortfolgen uebernommen


# ------------------------------------------------------------ Textvergleich ueber Berichte
def risk_doc(paras):
    return ("<html><body><div>ITEM 1. BUSINESS</div><p>" + "x " * 10 + "</p>"
            "<div>ITEM 1A. RISK FACTORS</div>" + "".join(f"<p>{p}</p>" for p in paras) +
            "<div>ITEM 1B. UNRESOLVED STAFF COMMENTS</div><p>None.</p></body></html>")


OLD_PARAS = [para(i) for i in range(30)]
NEW_PARAS = [p for k, p in enumerate(OLD_PARAS) if k not in (5, 6)] + [para(100 + i) for i in range(5)]  # ~1000 Zeichen neu


class FakeEdgar:
    def __init__(self, docs):
        self.docs, self.downloads = docs, 0

    def get_document(self, url):
        self.downloads += 1
        return self.docs[url]


class FakeLLM:
    model = "fake"

    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def complete(self, system, prompt):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return LLMResponse(text=json.dumps(self.reply), model="fake", cost_usd=0.001)


def llm_reply(quote=None):
    q = quote or " ".join(f"term100x{j}" for j in range(12))
    return {"summary": "Neue Risiken.", "tone": None, "tone_quote": None,
            "items": [{"kind": "risk", "title": "Neues Risiko", "statement": "S", "quote": q, "importance": 3},
                      {"kind": "risk", "title": "Erfunden", "statement": "S", "importance": 2,
                       "quote": "this quote never appears anywhere in the text"}]}


@pytest.fixture
def tsession(session):
    c = comp(session)
    session.add_all([Filing(company_id=c.id, accession_number="a-new", form_type="10-K", filed_at=date(2026, 7, 29),
                            url="http://x/new"),
                     Filing(company_id=c.id, accession_number="a-old", form_type="10-K", filed_at=date(2025, 7, 30),
                            url="http://x/old")])
    session.commit()
    return session


def edgar(new=NEW_PARAS, old=OLD_PARAS):
    return FakeEdgar({"http://x/new": risk_doc(new), "http://x/old": risk_doc(old)})


def test_text_change_with_ai_creates_verified_event(tsession):
    llm = FakeLLM(llm_reply())
    r = detect_text_changes(tsession, llm, edgar())[0]
    assert r.status == "neu" and r.n_added == 5 and r.n_removed == 2 and r.n_verified == 1
    a = tsession.scalar(select(Analysis).where(Analysis.section_key == DIFF_SECTION))
    assert a.model == "fake" and a.content["n_items"] == 2 and a.content["n_verified"] == 1
    ev = events(tsession)[0]
    assert ev.kind == "text_change" and ev.severity == 3 and "Neues Risiko" in ev.detail
    assert "Erfunden" not in ev.detail                                     # nicht verifizierte Aussage taucht nicht auf
    assert "5 neue/geaenderte" in ev.title and "2 entfallen" in ev.title


def test_text_change_is_idempotent_and_skips_downloads(tsession):
    llm, ed = FakeLLM(llm_reply()), edgar()
    detect_text_changes(tsession, llm, ed)
    n = ed.downloads
    r = detect_text_changes(tsession, llm, ed)[0]
    assert r.status.startswith("uebersprungen") and llm.calls == 1 and ed.downloads == n and len(events(tsession)) == 1


def test_no_ai_then_ai_upgrades_same_event(tsession):
    detect_text_changes(tsession, None, edgar())
    ev = events(tsession)[0]
    assert ev.severity == 1 and "ohne KI" in ev.detail
    assert tsession.scalar(select(Analysis).where(Analysis.section_key == DIFF_SECTION)).model == "none"
    detect_text_changes(tsession, None, edgar())                           # erneut ohne KI: uebersprungen
    assert len(events(tsession)) == 1
    llm = FakeLLM(llm_reply())
    assert detect_text_changes(tsession, llm, edgar())[0].status != "uebersprungen"
    assert llm.calls == 1 and len(events(tsession)) == 1                   # kein zweites Ereignis
    assert events(tsession)[0].severity == 3 and "Neues Risiko" in events(tsession)[0].detail
    assert len(tsession.scalars(select(Analysis).where(Analysis.section_key == DIFF_SECTION)).all()) == 1


def test_small_change_makes_no_event(tsession):
    new = OLD_PARAS[:-1] + [para(200, 10)]                                 # nur ~80 Zeichen neu, zu kurz
    assert len(paragraphs(para(200, 10))) == 0
    r = detect_text_changes(tsession, FakeLLM(llm_reply()), edgar(new))[0]
    assert r.status == "keine Aenderung" and events(tsession) == []


def test_text_change_needs_two_10k_and_isolates_errors(tsession):
    llm = FakeLLM(LLMError("Limit erreicht"))
    r = detect_text_changes(tsession, llm, edgar())[0]
    assert r.status.startswith("FEHLER") and "Limit erreicht" in r.status
    assert events(tsession) == [] and tsession.scalars(select(Analysis)).all() == []
    tsession.delete(tsession.scalar(select(Filing).where(Filing.accession_number == "a-old")))
    tsession.commit()
    assert "weniger als zwei" in detect_text_changes(tsession, None, edgar())[0].status


def test_max_calls_limits_ai_but_still_records_paragraph_stats(tsession):
    llm = FakeLLM(llm_reply())
    r = detect_text_changes(tsession, llm, edgar(), max_calls=0)[0]
    assert llm.calls == 0 and r.status == "neu"
    assert tsession.scalar(select(Analysis)).model == "none"               # spaeter mit KI aufwertbar


# ------------------------------------------------------------ Profilvergleich, Migration, Dashboard
def profile_content(margin, statements):
    return {"facts": {"fy": {"values": {"net_margin": margin}}, "ttm": None},
            "sheet": {"F:net_margin:FY": f"Nettomarge (...): {margin * 100:.1f}%"},
            "synthesis": {"strengths": [{"statement": s, "evidence": []} for s in statements],
                          "weaknesses": [], "watch": []}}


def test_diff_profiles():
    d = diff_profiles(profile_content(0.40, ["A", "B"]), profile_content(0.35, ["B", "C"]))
    assert d["facts"][0]["old_text"] == "40.0%" and d["facts"][0]["new_text"] == "35.0%"
    assert d["added"] == ["C"] and d["removed"] == ["A"]
    same = diff_profiles(profile_content(0.4, ["A"]), profile_content(0.4, ["A"]))
    assert same == {"facts": [], "added": [], "removed": []}


def test_migration_adds_dedupe_key_to_old_events_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alt.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, company_id INTEGER, created_at DATETIME, "
                          "kind VARCHAR(40), severity INTEGER, title VARCHAR(200), detail TEXT, seen BOOLEAN)"))
    init_db(engine)
    assert "dedupe_key" in {c["name"] for c in inspect(engine).get_columns("events")}


def test_dashboard_alerts_page_and_mark_seen(tmp_path, monkeypatch, session):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    c = comp(session)
    session.add_all([Event(company_id=c.id, kind="metric_change", severity=3, title="Nettomarge: 40.0% -> 30.0%",
                           detail="Detailtext", dedupe_key="k1"),
                     Event(company_id=c.id, kind="price_move", severity=1, title="Kurs +8.0%", dedupe_key="k2")])
    session.commit()
    db = tmp_path / "a.db"
    eng = create_engine(f"sqlite:///{db}")
    init_db(eng)
    with Session(eng) as dst:
        for model in (Company, Event):
            for row in session.scalars(select(model)):
                dst.add(model(**{col.name: getattr(row, col.name) for col in model.__table__.columns}))
        dst.commit()
    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    app = os.path.join(os.path.dirname(__file__), "..", "src", "invest_research", "dashboard", "app.py")
    at = AppTest.from_file(app, default_timeout=30).run()
    at.sidebar.radio[0].set_value("Alerts").run()
    assert not at.exception
    labels = [e.label for e in at.expander]
    assert labels[0].startswith("[HOCH] MSFT") and any("Kurs +8.0%" in l for l in labels)  # sortiert nach Wichtigkeit
    at.button[0].click().run()                                              # Angezeigte als gelesen markieren
    assert not at.exception and any("Keine passenden Alerts" in s.value for s in at.success)
