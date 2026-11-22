import json
import os
import subprocess
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from invest_research.analysis import (NONE_KEY, SectionResult, analyze_watchlist,
                                      ask_model, extract_json, verify)
from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.filing_text import html_to_text, quote_in_text, split_sections
from invest_research.llm import ClaudeCLI, LLMError, LLMResponse
from invest_research.models import Analysis, Company, Filing, FilingSection

SENT = "The company sells widgets to many customers worldwide and reports three segments. "


def body(n=40):
    return SENT * n  # > 2000 Zeichen


def doc(form="10-K"):
    """Synthetisches Dokument mit Inhaltsverzeichnis, Seitenkopfzeilen und echten Ueberschriften."""
    toc = "<p>Item 1. Business</p><p>Item 1A. Risk Factors</p><p>Item 7. Management's Discussion</p>"
    head = "<div>PART I</div><div>Item 1A</div>"  # laufende Kopfzeile, keine Ueberschrift
    if form == "10-K":
        main = (f"<div>ITEM 1. BUSINESS</div><p>{body()}</p>{head}"
                f"<div>ITEM 1A. RISK FACTORS</div><p>{body()}</p><div>12</div>{head}"
                f"<div>ITEM 1B. UNRESOLVED STAFF COMMENTS</div><p>None.</p>"
                f"<div>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</div><p>{body()}</p>"
                f"<div>ITEM 8. FINANCIAL STATEMENTS</div><p>x</p>")
    else:
        main = (f"<div>Item 1. Financial Statements</div><p>{body()}</p>"
                f"<div>Item 2. Management's Discussion and Analysis</div><p>{body()}</p>"
                f"<div>Item 2. Unregistered Sales of Equity Securities</div><p>None.</p>")
    return f"<html><body><ix:header><div>hidden stuff</div></ix:header>{toc}{main}</body></html>"


# ------------------------------------------------------------ Textpipeline
def test_html_to_text_keeps_inline_tags_and_tables():
    html = ("<html><body><ix:header>geheim</ix:header><p>Revenue was <ix:nonFraction>5</ix:nonFraction> "
            "billion</p><table><tr><td>A</td><td>1</td></tr></table></body></html>")
    text = html_to_text(html)
    assert "Revenue was 5 billion" in text and "geheim" not in text
    assert "A | 1" in text


def test_split_sections_10k_picks_real_sections_not_toc_or_page_headers():
    secs = split_sections(html_to_text(doc("10-K")), "10-K")
    assert set(secs) == {"business", "risks", "mdna"}
    assert all(len(v) > 2000 for v in secs.values())
    assert "Item 1A" not in secs["business"].splitlines()[-1]       # Kopfzeile am Ende entfernt
    assert not secs["risks"].rstrip().endswith("12")                # Seitenzahl entfernt


def test_split_sections_10q_takes_longest_item_2():
    secs = split_sections(html_to_text(doc("10-Q")), "10-Q")
    assert set(secs) == {"mdna"} and len(secs["mdna"]) > 2000       # nicht 'Unregistered Sales'


def test_split_sections_headings_in_table_cells_and_part_prefix():
    html = ("<table><tr><td>PART I</td><td>Item 1.</td><td>Financial Statements</td></tr></table>"
            f"<p>{body()}</p>"
            "<table><tr><td>ITEM 2.</td><td>MANAGEMENT'S DISCUSSION AND ANALYSIS</td></tr></table>"
            f"<p>{body()}</p>"
            "<table><tr><td>ITEM 3.</td><td>QUANTITATIVE DISCLOSURES</td></tr></table><p>x</p>")
    secs = split_sections(html_to_text(html), "10-Q")
    assert set(secs) == {"mdna"} and len(secs["mdna"]) > 2000


def test_mdna_is_cut_before_financial_statements_that_precede_item_8():
    html = (f"<div>Item 7. Management's Discussion and Analysis.</div><p>{body(100)}</p>"
            f"<div>Report of Management</div><p>{body()}</p>"
            "<div>Item 8. Financial Statements</div><p>x</p>")
    secs = split_sections(html_to_text(html), "10-K")
    assert secs["mdna"].count("The company sells widgets") == 100  # nur die MD&A, nicht der Rest


def test_split_sections_missing_returns_nothing():
    assert split_sections("Kein Bericht hier.", "10-K") == {}


def test_quote_matching_is_formatting_tolerant_but_not_paraphrase():
    text = "Our “cloud” business — benefits from three economies of scale:\n datacenters deploy resources."
    assert quote_in_text('Our "cloud" business - benefits from three economies of scale', text)
    assert quote_in_text("OUR CLOUD BUSINESS BENEFITS FROM THREE ECONOMIES OF SCALE", text)
    assert not quote_in_text("Our cloud business gains from three economies of scale", text)  # Paraphrase
    assert not quote_in_text("cloud", text)                          # zu kurz, trivial


# ------------------------------------------------------------ Modellantworten
def test_extract_json_tolerates_fences_and_noise():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Hier ist es: {"a": {"b": 2}} Fertig.') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("kein json")


def good_reply(quote="The company sells widgets to many customers worldwide and reports three segments.",
               kind="segment"):
    return json.dumps({"summary": "Zusammenfassung.", "tone": None, "tone_quote": None,
                       "items": [{"kind": kind, "title": "T", "statement": "S", "quote": quote,
                                  "importance": 7}]})


class FakeLLM:
    model = "fake"

    def __init__(self, replies):
        self.replies, self.calls, self.prompts = list(replies), 0, []

    def complete(self, system, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return LLMResponse(text=r, model="fake", input_tokens=10, output_tokens=5, cost_usd=0.001)


def test_ask_model_retries_once_then_succeeds_and_coerces_values():
    llm = FakeLLM(["das ist kein json", good_reply(kind="unbekannt")])
    res, _, truncated = ask_model(llm, "risks", body(), header="H")
    assert llm.calls == 2 and not truncated
    assert res.items[0].kind == "other" and res.items[0].importance == 3  # geklammert statt Absturz
    assert "invalid" in llm.prompts[1]


def test_ask_model_gives_up_after_second_invalid_reply():
    with pytest.raises(LLMError):
        ask_model(FakeLLM(["nein", "immer noch nein"]), "risks", body(), header="H")


def test_verify_flags_hallucinated_quote():
    result = SectionResult.model_validate(json.loads(good_reply(
        quote="This sentence was never in the filing at all, honestly.")))
    content = verify(result, body(), truncated=False)
    assert content["items"][0]["verified"] is False
    assert content["n_verified"] == 0 and content["low_quality"] is True


def test_verify_only_trusts_text_the_model_actually_saw():
    from invest_research.analysis import MAX_SECTION_CHARS
    hidden = "z" * MAX_SECTION_CHARS + " A distinctive sentence placed after the cutoff point exists here."
    result = SectionResult.model_validate(json.loads(good_reply(
        quote="A distinctive sentence placed after the cutoff point exists here.")))
    assert verify(result, hidden, truncated=True)["items"][0]["verified"] is False


# ------------------------------------------------------------ Orchestrierung
class FakeEdgar:
    def __init__(self, html_by_form):
        self.html, self.downloads = html_by_form, 0

    def get_document(self, url):
        self.downloads += 1
        return self.html["10-K" if "k" in url else "10-Q"]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software")])
        c = s.scalar(select(Company))
        c.name = "MICROSOFT CORP"
        s.add_all([Filing(company_id=c.id, accession_number="a-k", form_type="10-K", filed_at=date(2026, 7, 29),
                          url="http://x/k"),
                   Filing(company_id=c.id, accession_number="a-q", form_type="10-Q", filed_at=date(2026, 10, 28),
                          url="http://x/q"),
                   Filing(company_id=c.id, accession_number="a-old", form_type="10-K", filed_at=date(2025, 7, 29),
                          url="http://x/k-old")])
        s.commit()
        yield s


def edgar():
    return FakeEdgar({"10-K": doc("10-K"), "10-Q": doc("10-Q")})


def test_analyze_stores_verified_results_and_is_idempotent(session):
    llm, ed = FakeLLM([good_reply()]), edgar()
    reports, pending = analyze_watchlist(session, llm, ed, max_calls=10)
    assert pending == 0 and llm.calls == 4                     # 3 Abschnitte 10-K + MD&A 10-Q
    assert all(r.status == "ok" and r.n_verified == 1 for r in reports)
    assert ed.downloads == 2                                   # nur neuestes 10-K und neuestes 10-Q

    reports2, _ = analyze_watchlist(session, llm, ed, max_calls=10)
    assert llm.calls == 4 and ed.downloads == 2                # nichts doppelt
    assert all(r.status == "uebersprungen" for r in reports2)
    assert len(session.scalars(select(Analysis)).all()) == 4


def test_max_calls_caps_run_and_next_run_continues(session):
    llm, ed = FakeLLM([good_reply()]), edgar()
    reports, pending = analyze_watchlist(session, llm, ed, max_calls=2)
    assert llm.calls == 2 and pending == 2
    _, pending2 = analyze_watchlist(session, llm, ed, max_calls=10)
    assert llm.calls == 4 and pending2 == 0


def test_errors_are_isolated_and_force_replaces(session):
    llm = FakeLLM([LLMError("Limit erreicht"), good_reply()])
    reports, _ = analyze_watchlist(session, llm, edgar(), max_calls=10)
    assert reports[0].status.startswith("FEHLER") and sum(r.status == "ok" for r in reports) == 3
    analyze_watchlist(session, FakeLLM([good_reply()]), edgar(), max_calls=10)   # fehlenden nachholen
    assert len(session.scalars(select(Analysis)).all()) == 4
    _, _ = analyze_watchlist(session, FakeLLM([good_reply()]), edgar(), max_calls=10, force=True)
    assert len(session.scalars(select(Analysis)).all()) == 4                      # ersetzt, nicht verdoppelt


def test_unparseable_filing_is_marked_and_not_downloaded_again(session):
    ed = FakeEdgar({"10-K": "<html><body>nichts Brauchbares</body></html>", "10-Q": doc("10-Q")})
    analyze_watchlist(session, FakeLLM([good_reply()]), ed, max_calls=10)
    first = ed.downloads
    assert session.scalar(select(FilingSection).where(FilingSection.key == NONE_KEY)) is not None
    analyze_watchlist(session, FakeLLM([good_reply()]), ed, max_calls=10)
    assert ed.downloads == first


# ------------------------------------------------------------ claude -p
def make_runner(stdout, returncode=0, seen=None):
    def runner(cmd, stdin, timeout, cwd):
        if seen is not None:
            seen.update(cmd=cmd, stdin=stdin, cwd=cwd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom")
    return runner


def test_claude_cli_is_locked_down_and_parses_usage():
    seen = {}
    out = json.dumps({"result": "{}", "is_error": False, "total_cost_usd": 0.01,
                      "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100, "output_tokens": 7}})
    cli = ClaudeCLI("haiku", executable="claude", runner=make_runner(out, seen=seen))
    resp = cli.complete("SYS", "PROMPT")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == ""                       # alle Tools aus
    assert "--no-session-persistence" in cmd and cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert seen["stdin"] == "PROMPT" and "PROMPT" not in cmd         # Prompt ueber stdin, nicht argv
    assert seen["cwd"] != os.getcwd()                                # leeres Temp-Verzeichnis
    assert resp.input_tokens == 105 and resp.output_tokens == 7 and resp.cost_usd == 0.01


def test_claude_cli_error_paths():
    with pytest.raises(LLMError, match="Fehlercode"):
        ClaudeCLI("haiku", executable="claude", runner=make_runner("", returncode=1)).complete("s", "p")
    with pytest.raises(LLMError, match="kein JSON"):
        ClaudeCLI("haiku", executable="claude", runner=make_runner("Login erforderlich")).complete("s", "p")
    err = json.dumps({"is_error": True, "result": "Limit erreicht"})
    with pytest.raises(LLMError, match="Limit erreicht"):
        ClaudeCLI("haiku", executable="claude", runner=make_runner(err)).complete("s", "p")


def test_claude_cli_missing_executable(monkeypatch):
    monkeypatch.setattr("invest_research.llm.shutil.which", lambda _: None)
    with pytest.raises(LLMError, match="nicht gefunden"):
        ClaudeCLI("haiku")


# ------------------------------------------------------------ Dashboard
def test_dashboard_shows_only_verified_items(tmp_path, monkeypatch, session):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    analyze_watchlist(session, FakeLLM([good_reply()]), edgar(), max_calls=10)
    # eine nicht verifizierte Aussage dazu: darf nicht erscheinen
    a = session.scalars(select(Analysis)).first()
    a.content = {**a.content, "items": a.content["items"] + [{
        "kind": "risk", "title": "ERFUNDEN", "statement": "x", "quote": "nope nope nope nope nope nope",
        "importance": 3, "verified": False}], "n_items": 2}
    session.commit()
    db = tmp_path / "d.db"
    eng = create_engine(f"sqlite:///{db}")
    init_db(eng)
    with Session(eng) as dst:
        for model in (Company, Filing, Analysis):
            for row in session.scalars(select(model)):
                data = {c.name: getattr(row, c.name) for c in model.__table__.columns}
                dst.add(model(**data))
        dst.commit()
    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    app = os.path.join(os.path.dirname(__file__), "..", "src", "invest_research", "dashboard", "app.py")
    at = AppTest.from_file(app, default_timeout=30).run()
    at.sidebar.radio[0].set_value("Unternehmen").run()
    assert not at.exception
    page = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "KI-Zusammenfassung" in page and "ERFUNDEN" not in page
    assert "ohne verifiziertes Zitat ausgeblendet" in page
