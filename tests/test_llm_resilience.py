import json
import subprocess
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from invest_research.analysis import analyze_watchlist
from invest_research.changes import detect_text_changes
from invest_research.config import WatchlistEntry
from invest_research.db import init_db, sync_watchlist
from invest_research.llm import (ClaudeCLI, FailureGuard, LLMError, LLMResponse, LLMUnavailable,
                                 describe_failure, diagnose)
from invest_research.models import Analysis, Company, Filing
from invest_research.models import Metric
from invest_research.profiles import build_profiles

SENT = "The company sells widgets to many customers worldwide and reports three segments. "


def para(i, n=25):
    return " ".join(f"term{i}x{j}" for j in range(n))   # eindeutige Woerter je Absatz


OLD_PARAS = [para(i) for i in range(30)]
NEW_PARAS = [p for k, p in enumerate(OLD_PARAS) if k not in (5, 6)] + [para(100 + i) for i in range(5)]


def full_doc(paras):
    return ("<html><body><div>ITEM 1. BUSINESS</div><p>" + SENT * 40 + "</p>"
            "<div>ITEM 1A. RISK FACTORS</div>" + "".join(f"<p>{p}</p>" for p in paras) +
            "<div>ITEM 1B. UNRESOLVED STAFF COMMENTS</div><p>None.</p>"
            "<div>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</div><p>" + SENT * 40 + "</p>"
            "<div>ITEM 8. FINANCIAL STATEMENTS</div><p>x</p></body></html>")


class FakeEdgar:
    def __init__(self, docs):
        self.docs = docs

    def get_document(self, url):
        return self.docs[url]

# So sah der Fehler auf dem Windows-Rechner aus: Exit-Code 1, JSON auf stdout, null Token
WINDOWS_STYLE_ERROR = json.dumps({"is_error": True, "result": "Not logged in - please run /login",
                                  "stop_reason": "stop_sequence", "usage": {"input_tokens": 0, "output_tokens": 0},
                                  "total_cost_usd": 0, "duration_api_ms": 0})


def proc(stdout="", stderr="", code=1):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def runner_returning(p):
    return lambda cmd, stdin, timeout, cwd: p


# ------------------------------------------------------------ Fehlertexte
def test_describe_failure_prefers_result_text_from_json():
    msg = describe_failure(proc(WINDOWS_STYLE_ERROR))
    assert msg.startswith("Not logged in") and "duration_api_ms" not in msg      # nicht das ganze JSON
    assert describe_failure(proc("", "kaputt")) == "kaputt"
    assert describe_failure(proc("kein json")) == "kein json"
    assert describe_failure(proc("")) == "(keine Ausgabe)"


def test_nonzero_exit_with_json_error_names_the_real_cause():
    cli = ClaudeCLI("haiku", executable="claude", runner=runner_returning(proc(WINDOWS_STYLE_ERROR)))
    with pytest.raises(LLMUnavailable, match="Not logged in"):
        cli.complete("s", "p")


def test_start_failure_and_timeout_are_unavailable():
    def boom(cmd, stdin, timeout, cwd):
        raise FileNotFoundError("claude.cmd")
    with pytest.raises(LLMUnavailable, match="nicht gestartet"):
        ClaudeCLI("haiku", executable="claude", runner=boom).complete("s", "p")

    def slow(cmd, stdin, timeout, cwd):
        raise subprocess.TimeoutExpired(cmd, timeout)
    with pytest.raises(LLMUnavailable, match="Zeitueberschreitung"):
        ClaudeCLI("haiku", executable="claude", runner=slow).complete("s", "p")


def test_tool_attempt_is_blocked_and_reported_as_error_not_outage():
    out = json.dumps({"result": "x", "is_error": False,
                      "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "ls"}}]})
    with pytest.raises(LLMError, match="Tools nutzen") as exc:
        ClaudeCLI("haiku", executable="claude", runner=runner_returning(proc(out, code=0))).complete("s", "p")
    assert not isinstance(exc.value, LLMUnavailable)     # Injection-Verdacht ist kein Ausfall


def test_failure_guard_counts_only_consecutive_outages():
    g = FailureGuard(2)
    g.failed(LLMUnavailable("a"))
    g.failed(LLMError("Modellantwort ungueltig"))        # kein Ausfall: setzt zurueck
    assert not g.tripped
    g.failed(LLMUnavailable("b"))
    g.ok()
    g.failed(LLMUnavailable("c"))
    assert not g.tripped
    g.failed(LLMUnavailable("d"))
    assert g.tripped and "d" in g.message()


def test_diagnose_reports_missing_program(monkeypatch):
    monkeypatch.setattr("invest_research.llm.shutil.which", lambda _: None)
    res = diagnose()
    assert res[0][0] is False and "PATH" in res[0][1]


# ------------------------------------------------------------ Schutzschalter in den Ablaeufen
class DownLLM:
    model = "down"

    def __init__(self):
        self.calls = 0

    def complete(self, system, prompt):
        self.calls += 1
        raise LLMUnavailable("claude -p Fehlercode 1: Not logged in")


@pytest.fixture
def five(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as s:
        tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        sync_watchlist(s, [WatchlistEntry(ticker=t, sector="X") for t in tickers])
        for c in s.scalars(select(Company)):
            s.add_all([Filing(company_id=c.id, accession_number=f"{c.ticker}-k", form_type="10-K",
                              filed_at=date(2026, 7, 29), url=f"http://x/{c.ticker}/new"),
                       Filing(company_id=c.id, accession_number=f"{c.ticker}-o", form_type="10-K",
                              filed_at=date(2025, 7, 30), url=f"http://x/{c.ticker}/old")])
        s.commit()
        yield s, tickers


def test_analyze_stops_after_two_outages(five):
    s, tickers = five
    docs = {f"http://x/{t}/{v}": full_doc(OLD_PARAS) for t in tickers for v in ("new", "old")}
    llm = DownLLM()
    reports, _ = analyze_watchlist(s, llm, FakeEdgar(docs), max_calls=50)
    assert llm.calls == 2                                 # nicht 15 Aufrufe gegen einen Ausfall
    assert reports[-1].ticker == "*" and "KI nicht verfuegbar" in reports[-1].status
    assert s.scalars(select(Analysis)).all() == []


def test_text_change_degrades_to_paragraph_stats_after_two_outages(five):
    s, tickers = five
    docs = {}
    for t in tickers:
        docs[f"http://x/{t}/new"], docs[f"http://x/{t}/old"] = full_doc(NEW_PARAS), full_doc(OLD_PARAS)
    llm = DownLLM()
    reports = detect_text_changes(s, llm, FakeEdgar(docs), max_calls=50)
    assert llm.calls == 2
    assert any(r.ticker == "*" and "KI nicht verfuegbar" in r.status for r in reports)
    stored = {a.model for a in s.scalars(select(Analysis))}
    assert stored == {"none"}                             # Rest nur mit Statistik, spaeter mit KI aufwertbar
    assert len(s.scalars(select(Analysis)).all()) == 3    # AAA/BBB fehlgeschlagen (nichts gespeichert), CCC-EEE ohne KI


def test_profiles_skip_remaining_after_two_outages(five):
    s, tickers = five
    for c in s.scalars(select(Company)):                  # Analysen vorhanden -> KI waere noetig
        f = s.scalar(select(Filing).where(Filing.company_id == c.id, Filing.form_type == "10-K"))
        s.add(Analysis(filing_id=f.id, section_key="business", prompt_version="v2", model="haiku",
                       content={"summary": "x", "items": [], "n_items": 0, "n_verified": 0}))
    s.commit()
    for c in s.scalars(select(Company)):                  # minimale Kennzahlen, damit ein Profil moeglich ist
        s.add(Metric(company_id=c.id, name="revenue", value=1.0, unit="USD", period_end=date(2026, 6, 30),
                     period_type="FY", source="xbrl"))
    s.commit()
    llm = DownLLM()
    res = build_profiles(s, llm)
    assert llm.calls == 2
    assert all(r.status.startswith("FEHLER") for r in res)
    assert "KI nicht verfuegbar" in res[-1].status
