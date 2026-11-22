import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from invest_research.config import load_settings
from invest_research.db import init_db
from invest_research.llm import (FailureGuard, LLMBudgetExceeded, LLMError, LLMResponse, LLMUnavailable)
from invest_research.logsetup import Tee, setup_logging, tail
from invest_research.models import LLMCall
from invest_research.usage import Budget, MeteredLLM, day_start, month_start, usage_summary


class Inner:
    model = "fake"

    def __init__(self, fail=None):
        self.fail, self.calls = fail, 0

    def complete(self, system, prompt):
        self.calls += 1
        if self.fail:
            raise self.fail
        return LLMResponse(text="x", model="fake", input_tokens=100, output_tokens=10, cost_usd=0.02)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'u.db'}")  # Datei: das Protokoll nutzt eine eigene Verbindung
    init_db(eng)
    return eng


def calls(engine):
    with Session(engine) as s:
        return s.scalars(select(LLMCall).order_by(LLMCall.id)).all()


def test_successful_call_is_recorded_with_tokens_and_cost(engine):
    llm = MeteredLLM(Inner(), engine, "analyze", Budget(10))
    assert llm.model == "fake" and llm.complete("s", "p").text == "x"
    c = calls(engine)[0]
    assert (c.purpose, c.ok, c.input_tokens, c.output_tokens, c.cost_usd) == ("analyze", True, 100, 10, 0.02)
    assert c.duration_s is not None and c.error is None


def test_failed_call_is_recorded_and_reraised(engine):
    llm = MeteredLLM(Inner(LLMUnavailable("Not logged in")), engine, "watch", Budget(10))
    with pytest.raises(LLMUnavailable):
        llm.complete("s", "p")
    c = calls(engine)[0]
    assert c.ok is False and "Not logged in" in c.error and c.input_tokens is None


def test_daily_limit_blocks_before_calling_and_is_not_recorded(engine):
    inner = Inner()
    llm = MeteredLLM(inner, engine, "ask", Budget(2))
    llm.complete("s", "p")
    llm.complete("s", "p")
    with pytest.raises(LLMBudgetExceeded, match="Tageslimit von 2"):
        llm.complete("s", "p")
    assert inner.calls == 2 and len(calls(engine)) == 2


def test_failed_calls_do_not_consume_the_daily_budget(engine):
    broken = MeteredLLM(Inner(LLMUnavailable("x")), engine, "ask", Budget(1))
    for _ in range(3):
        with pytest.raises(LLMUnavailable):
            broken.complete("s", "p")
    assert MeteredLLM(Inner(), engine, "ask", Budget(1)).complete("s", "p").text == "x"   # Budget noch frei


def test_yesterday_does_not_count_today(engine):
    with Session(engine) as s:
        s.add(LLMCall(purpose="ask", model="fake", ok=True, created_at=day_start() - timedelta(hours=2)))
        s.commit()
    assert MeteredLLM(Inner(), engine, "ask", Budget(1)).complete("s", "p").text == "x"


def test_monthly_cost_cap(engine):
    with Session(engine) as s:
        s.add(LLMCall(purpose="ask", model="fake", ok=True, cost_usd=0.60, created_at=month_start() + timedelta(minutes=1)))
        s.commit()
    llm = MeteredLLM(Inner(), engine, "ask", Budget(100, max_cost_per_month=0.50))
    with pytest.raises(LLMBudgetExceeded, match="Monatslimit"):
        llm.complete("s", "p")
    assert MeteredLLM(Inner(), engine, "ask", Budget(100, max_cost_per_month=1.0)).complete("s", "p").text == "x"


def test_budget_exceeded_stops_failure_guard_immediately():
    g = FailureGuard(2)
    g.failed(LLMBudgetExceeded("Tageslimit"))
    assert g.tripped and "Tageslimit" in g.message()
    h = FailureGuard(2)
    h.failed(LLMUnavailable("x"))
    assert not h.tripped                                   # normale Ausfälle brauchen zwei in Folge
    assert issubclass(LLMBudgetExceeded, LLMUnavailable) and issubclass(LLMUnavailable, LLMError)


def test_usage_summary(engine):
    llm = MeteredLLM(Inner(), engine, "analyze", Budget(10))
    llm.complete("s", "p")
    MeteredLLM(Inner(), engine, "ask", Budget(10)).complete("s", "p")
    with pytest.raises(LLMUnavailable):
        MeteredLLM(Inner(LLMUnavailable("kaputt")), engine, "watch", Budget(10)).complete("s", "p")
    with Session(engine) as s:
        u = usage_summary(s, Budget(10))
    assert u["calls_today"] == 2 and u["failed_month"] == 1 and u["recent_errors"][0]["error"] == "kaputt"
    assert {r["purpose"] for r in u["by_purpose"]} == {"analyze", "ask"}
    assert u["cost_month"] == pytest.approx(0.04)


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x" / "r.db"))
    monkeypatch.setenv("LLM_MAX_CALLS_PER_DAY", "7")
    monkeypatch.setenv("LLM_MAX_COST_USD_PER_MONTH", "2.5")
    s = load_settings()
    assert s.llm_max_calls_per_day == 7 and s.llm_max_cost_per_month == 2.5
    assert s.log_path == tmp_path / "x" / "invest_research.log"
    monkeypatch.setenv("LLM_MAX_COST_USD_PER_MONTH", "")
    monkeypatch.delenv("LLM_MAX_CALLS_PER_DAY")
    s = load_settings()
    assert s.llm_max_cost_per_month is None and s.llm_max_calls_per_day == 80


def test_logging_writes_file_and_console_only_warnings(tmp_path, capsys):
    path = tmp_path / "logs" / "app.log"
    setup_logging(path)
    log = logging.getLogger("invest_research.test")
    log.info("nur in der Datei")
    log.warning("auch auf der Konsole")
    text = path.read_text(encoding="utf-8")
    assert "nur in der Datei" in text and "auch auf der Konsole" in text
    err = capsys.readouterr().err
    assert "auch auf der Konsole" in err and "nur in der Datei" not in err
    setup_logging(path, verbose=True)                       # -v: Info auch auf der Konsole, keine doppelten Handler
    log.info("jetzt sichtbar")
    assert "jetzt sichtbar" in capsys.readouterr().err
    assert sum(getattr(h, "_invest_research", False) for h in logging.getLogger().handlers) == 2
    assert tail(path, 2).count("\n") <= 2 and tail(tmp_path / "gibt_es_nicht.log") == ""


def test_tee_logs_each_printed_line_and_passes_output_through(capsys):
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    lg = logging.getLogger("tee_test")
    lg.setLevel(logging.INFO)
    lg.addHandler(H())
    import sys
    old = sys.stdout
    sys.stdout = Tee(old, lg)
    try:
        print("Zeile eins")
        print("")
        print("Zeile zwei")
    finally:
        sys.stdout = old
    assert records == ["Zeile eins", "Zeile zwei"]
    assert "Zeile eins" in capsys.readouterr().out


def test_dashboard_system_page(tmp_path, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from invest_research.config import WatchlistEntry
    from invest_research.db import sync_watchlist
    db = tmp_path / "s.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    eng = create_engine(f"sqlite:///{db}")
    init_db(eng)
    with Session(eng) as s:
        sync_watchlist(s, [WatchlistEntry(ticker="MSFT", sector="Software")])
    MeteredLLM(Inner(), eng, "ask", Budget(10)).complete("s", "p")
    st.cache_resource.clear()
    app = os.path.join(os.path.dirname(__file__), "..", "src", "invest_research", "dashboard", "app.py")
    at = AppTest.from_file(app, default_timeout=30).run()
    at.sidebar.radio[0].set_value("System").run()
    assert not at.exception
    assert any("Heute 1 von 80" in p.text for p in at.get("progress"))
