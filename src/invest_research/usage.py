"""Nutzungsprotokoll und Limits fuer KI-Aufrufe.

`MeteredLLM` umhuellt einen LLMClient: prueft vor jedem Aufruf das Budget, misst Dauer und
Token und schreibt jeden Aufruf (auch Fehlschlaege) in `llm_calls`. Das Protokoll geht ueber eine
eigene kurze Datenbankverbindung, damit laufende Transaktionen der Auswertungen unberuehrt bleiben.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .llm import LLMBudgetExceeded, LLMClient, LLMResponse
from .models import LLMCall


@dataclass(frozen=True)
class Budget:
    max_calls_per_day: int
    max_cost_per_month: float | None = None


def day_start(now: datetime | None = None) -> datetime:
    """Lokaler Tagesbeginn (so wie der Nutzer 'heute' versteht), als UTC fuer die Abfrage."""
    local = (now or datetime.now().astimezone()).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def month_start(now: datetime | None = None) -> datetime:
    local = (now or datetime.now().astimezone()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _count_ok(session: Session, since: datetime) -> int:
    return session.scalar(select(func.count()).select_from(LLMCall)
                          .where(LLMCall.created_at >= since, LLMCall.ok.is_(True))) or 0


def _cost(session: Session, since: datetime) -> float:
    return float(session.scalar(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                .where(LLMCall.created_at >= since, LLMCall.ok.is_(True))) or 0.0)


class MeteredLLM:
    def __init__(self, inner: LLMClient, engine: Engine, purpose: str, budget: Budget):
        self._inner, self._engine, self._purpose, self._budget = inner, engine, purpose, budget

    @property
    def model(self) -> str:
        return self._inner.model

    def check_budget(self) -> None:
        with Session(self._engine) as s:
            calls = _count_ok(s, day_start())
            if calls >= self._budget.max_calls_per_day:
                raise LLMBudgetExceeded(
                    f"Tageslimit von {self._budget.max_calls_per_day} KI-Aufrufen erreicht "
                    "(LLM_MAX_CALLS_PER_DAY in .env). Morgen geht es weiter, oder das Limit erhoehen.")
            cap = self._budget.max_cost_per_month
            if cap is not None and _cost(s, month_start()) >= cap:
                raise LLMBudgetExceeded(
                    f"Monatslimit von {cap:.2f} USD (Listenpreis) erreicht "
                    "(LLM_MAX_COST_USD_PER_MONTH in .env).")

    def _record(self, ok: bool, started: float, resp: LLMResponse | None = None, error: str | None = None) -> None:
        with Session(self._engine) as s:
            s.add(LLMCall(purpose=self._purpose, model=self.model, ok=ok,
                          input_tokens=resp.input_tokens if resp else None,
                          output_tokens=resp.output_tokens if resp else None,
                          cost_usd=resp.cost_usd if resp else None,
                          duration_s=round(time.monotonic() - started, 2),
                          error=(error or "")[:400] or None))
            s.commit()

    def complete(self, system: str, prompt: str) -> LLMResponse:
        self.check_budget()  # Limit-Verstoss wird NICHT als Aufruf protokolliert
        started = time.monotonic()
        try:
            resp = self._inner.complete(system, prompt)
        except Exception as exc:
            self._record(False, started, error=str(exc))
            raise
        self._record(True, started, resp)
        return resp


def usage_summary(session: Session, budget: Budget) -> dict:
    """Kennzahlen fuer `usage` und die System-Seite."""
    today, month = day_start(), month_start()
    out = {"calls_today": _count_ok(session, today), "max_calls_per_day": budget.max_calls_per_day,
           "cost_month": _cost(session, month), "max_cost_per_month": budget.max_cost_per_month}
    rows = session.execute(select(LLMCall.purpose, func.count(), func.coalesce(func.sum(LLMCall.input_tokens), 0),
                                  func.coalesce(func.sum(LLMCall.output_tokens), 0),
                                  func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                           .where(LLMCall.created_at >= month, LLMCall.ok.is_(True)).group_by(LLMCall.purpose)).all()
    out["by_purpose"] = [{"purpose": r[0], "calls": r[1], "input_tokens": int(r[2]), "output_tokens": int(r[3]),
                          "cost_usd": float(r[4])} for r in rows]
    out["failed_month"] = session.scalar(select(func.count()).select_from(LLMCall)
                                         .where(LLMCall.created_at >= month, LLMCall.ok.is_(False))) or 0
    last = session.scalars(select(LLMCall).where(LLMCall.ok.is_(False)).order_by(LLMCall.created_at.desc()).limit(3))
    out["recent_errors"] = [{"at": c.created_at, "purpose": c.purpose, "error": c.error} for c in last]
    return out
