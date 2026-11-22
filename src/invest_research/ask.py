"""Fragen an die Daten in natuerlicher Sprache.

Die KI beantwortet nichts selbst und schreibt kein SQL. Sie uebersetzt die Frage in einen
strukturierten Abfrageplan ueber eine feste Spaltenliste (Whitelist). Der Code prueft den Plan,
fuehrt ihn auf der Datenbank aus und zeigt "Verstanden als ..." aus dem Plan selbst (nicht aus
KI-Text), damit jede Antwort nachvollziehbar ist. Zahlen stammen ausschliesslich aus der Datenbank.

Fragen zu Berichtsinhalten ("Welche Risiken nennt MSFT?") liefern die bereits zitatgeprueften
Aussagen aus Schritt 5, keine neue KI-Formulierung.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import queries
from .analysis import extract_json
from .engine import RATIOS
from .llm import LLMClient, LLMError
from .models import Metric
from .peers import add_percentiles, overall_score, snapshot

log = logging.getLogger(__name__)

TEXT_SECTIONS = {"business": "Geschaeftsmodell", "risks": "Risiken", "risks_diff": "Neue Risiken gegenueber dem Vorjahr",
                 "mdna": "Management-Analyse (MD&A)"}
MAX_ROWS = 50


class PlanError(ValueError):
    """Der Plan der KI ist unzulaessig (unbekannte Spalte, falscher Typ ...)."""


@dataclass(frozen=True)
class Col:
    label: str
    kind: str   # text | pct | pp | x | usd | price
    help: str = ""


def build_catalog() -> dict[str, Col]:
    cat: dict[str, Col] = {
        "name": Col("Name", "text"), "sector": Col("Branche", "text", "exakte Branchenbezeichnung"),
        "price": Col("Kurs", "price", "letzter Schlusskurs in USD"),
        "chg_1m": Col("Kurs 1 Monat", "pct", "Kursaenderung 30 Tage"),
        "chg_ytd": Col("Kurs seit Jahresbeginn", "pct"), "chg_1y": Col("Kurs 1 Jahr", "pct"),
        "pos_52w": Col("Lage im 52-Wochen-Bereich", "pct", "0 = Jahrestief, 1 = Jahreshoch"),
        "market_cap": Col("Marktkapitalisierung", "usd"), "pe": Col("KGV", "x", "Kurs/Gewinn, 12 Monate"),
        "ps": Col("KUV", "x", "Kurs/Umsatz, 12 Monate"), "p_fcf": Col("Kurs/FCF", "x", "Kurs/freier Cashflow"),
        "score": Col("Branchen-Score", "score", "mittleres Perzentil in der Branche, 0..1, nur Orientierung"),
    }
    for basis, suffix in (("ttm", "letzte 12 Monate"), ("fy", "letztes Geschaeftsjahr")):
        for name, (label, _, kind) in RATIOS.items():
            cat[f"{basis}_{name}"] = Col(f"{label} ({suffix})", "pct" if kind == "pct" else "x")
        cat[f"{basis}_revenue"] = Col(f"Umsatz ({suffix})", "usd")
        cat[f"{basis}_net_income"] = Col(f"Nettogewinn ({suffix})", "usd")
        cat[f"{basis}_fcf"] = Col(f"Freier Cashflow ({suffix})", "usd")
    for name, (label, _, kind) in RATIOS.items():
        cat[f"chg_{name}"] = Col(f"Veraenderung {label} ggue. Vorjahr", "pp" if kind == "pct" else "x",
                                 "letzte 12 Monate minus dieselben 12 Monate ein Jahr frueher; positiv = gestiegen")
    return cat


CATALOG = build_catalog()


def _yoy_changes(session: Session, company_id: int) -> dict[str, float]:
    ends = sorted(session.scalars(select(Metric.period_end).where(
        Metric.company_id == company_id, Metric.name == "revenue", Metric.period_type == "TTM")))
    if not ends:
        return {}
    now = ends[-1]
    prev = next((e for e in reversed(ends) if 350 <= (now - e).days <= 380), None)
    if prev is None:
        return {}
    vals: dict[tuple[str, date], float] = {}
    for m in session.scalars(select(Metric).where(
            Metric.company_id == company_id, Metric.period_type == "TTM",
            Metric.name.in_(list(RATIOS)), Metric.period_end.in_([now, prev]))):
        if m.value is not None:
            vals[(m.name, m.period_end)] = m.value
    return {f"chg_{n}": vals[(n, now)] - vals[(n, prev)] for n in RATIOS if (n, now) in vals and (n, prev) in vals}


def question_table(session: Session) -> pd.DataFrame:
    """Eine Zeile je Watchlist-Titel mit allen abfragbaren Spalten (Index = Ticker)."""
    ov = queries.overview(session)
    if ov.empty:
        return pd.DataFrame()
    df = ov[["name", "sector", "price", "chg_1m", "chg_ytd", "chg_1y", "pos_52w", "market_cap", "pe", "ps",
             "p_fcf"]].copy()
    fy = add_percentiles(snapshot(session, "FY"))
    if not fy.empty:
        fy["score"] = overall_score(fy)
        df["score"] = fy["score"]
    for basis, snap in (("fy", fy), ("ttm", snapshot(session, "TTM"))):
        for col in list(RATIOS) + ["revenue", "net_income", "fcf"]:
            df[f"{basis}_{col}"] = snap[col] if col in snap else float("nan")
    ids = {c.ticker: c.id for c in queries.watchlist(session)}
    yoy = pd.DataFrame({t: _yoy_changes(session, cid) for t, cid in ids.items()}).T
    for name in RATIOS:
        df[f"chg_{name}"] = yoy[f"chg_{name}"] if f"chg_{name}" in yoy else float("nan")
    for col in CATALOG:
        if col not in df:
            df[col] = float("nan")
    return df


# ------------------------------------------------------------------ Plan
Op = Literal[">", ">=", "<", "<=", "==", "!="]


class Cond(BaseModel):
    column: str
    op: Op
    value: float | str


class Plan(BaseModel):
    kind: Literal["table", "text", "unsupported"] = "table"
    columns: list[str] = Field(default_factory=list)
    filters: list[Cond] = Field(default_factory=list)
    sort_by: str | None = None
    descending: bool = True
    limit: int = 10
    tickers: list[str] = Field(default_factory=list)
    text_section: Literal["business", "risks", "risks_diff", "mdna"] | None = None
    note: str | None = None

    @field_validator("limit", mode="before")
    @classmethod
    def _limit(cls, v):
        try:
            return max(1, min(MAX_ROWS, int(v)))
        except (TypeError, ValueError):
            return 10


def validate_plan(plan: Plan, tickers: set[str]) -> Plan:
    if plan.kind == "unsupported":
        return plan
    plan.tickers = [t.upper() for t in plan.tickers]
    unknown = [t for t in plan.tickers if t not in tickers]
    if unknown:
        raise PlanError(f"Unbekannte Ticker (nicht in der Watchlist): {', '.join(unknown)}")
    if plan.kind == "text":
        if len(plan.tickers) != 1 or not plan.text_section:
            raise PlanError("Textfragen brauchen genau einen Ticker und einen Abschnitt")
        return plan
    for col in plan.columns + [c.column for c in plan.filters] + ([plan.sort_by] if plan.sort_by else []):
        if col not in CATALOG:
            raise PlanError(f"Unbekannte Spalte: {col}")
    for c in plan.filters:
        is_text = CATALOG[c.column].kind == "text"
        if is_text and (c.op not in ("==", "!=") or not isinstance(c.value, str)):
            raise PlanError(f"Spalte {c.column} ist Text: nur == oder != mit Textwert")
        if not is_text and isinstance(c.value, str):
            raise PlanError(f"Spalte {c.column} ist numerisch: Zahlenwert erwartet")
    return plan


# ------------------------------------------------------------------ Formatierung und Ausfuehrung
def fmt(kind: str, v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "score":
        return f"{v * 100:.0f} von 100"
    if kind == "pp":
        return f"{v * 100:+.1f} Pp"
    if kind == "x":
        return f"{v:.2f}x"
    if kind == "price":
        return f"{v:.2f} $"
    if kind == "usd":
        return f"{v / 1e12:.2f} Bio. $" if abs(v) >= 1e12 else f"{v / 1e9:.1f} Mrd. $"
    return str(v)


OP_TEXT = {">": "ueber", ">=": "mindestens", "<": "unter", "<=": "hoechstens", "==": "gleich", "!=": "ungleich"}


@dataclass
class Result:
    plan: Plan
    interpreted: str
    table: pd.DataFrame | None = None          # formatierte Anzeige (Strings)
    raw: pd.DataFrame | None = None            # Rohwerte
    text: dict | None = None                   # Berichtsaussagen bei kind == 'text'
    notes: list[str] = field(default_factory=list)


def _cond_text(c: Cond) -> str:
    col = CATALOG[c.column]
    value = c.value if isinstance(c.value, str) else fmt(col.kind, c.value)
    return f"{col.label} {OP_TEXT[c.op]} {value}"


def run_plan(df: pd.DataFrame, plan: Plan, session: Session | None = None) -> Result:
    if plan.kind == "unsupported":
        return Result(plan, "Die Frage kann mit den vorhandenen Daten nicht beantwortet werden.",
                      notes=[plan.note or "Keine Begruendung angegeben."])
    if plan.kind == "text":
        ticker, section = plan.tickers[0], plan.text_section
        rows = [r for r in (queries.latest_analyses(session, ticker) if session else []) if r["section"] == section]
        label = TEXT_SECTIONS[section]
        if not rows:
            return Result(plan, f"{label} laut Bericht: {ticker}",
                          notes=[f"Keine Analyse fuer {ticker}/{label}. Erst: python -m invest_research analyze "
                                 f"--ticker {ticker}" + (" bzw. watch" if section == "risks_diff" else "")])
        r = rows[0]
        items = sorted((i for i in r["content"]["items"] if i["verified"]), key=lambda i: -i["importance"])
        return Result(plan, f"{label} laut {r['form_type']} vom {r['filed_at']}: {ticker}",
                      text={"form_type": r["form_type"], "filed_at": str(r["filed_at"]), "url": r["url"],
                            "summary": r["content"]["summary"], "items": items},
                      notes=["Zusammenfassung ist KI-Text (nicht zitatgeprueft); die Aussagen darunter haben ein "
                             "geprueftes Originalzitat."])

    notes: list[str] = []
    work = df.copy()
    if plan.tickers:
        work = work.loc[[t for t in plan.tickers if t in work.index]]
    conditions = []
    for c in plan.filters:
        col = CATALOG[c.column]
        series = work[c.column]
        if col.kind == "text":
            mask = (series == c.value) if c.op == "==" else (series != c.value)
            mask = mask.fillna(False) if c.op == "==" else mask
        else:
            missing = work.index[series.isna()].tolist()
            if missing:
                notes.append(f"Ohne Daten fuer '{col.label}' ausgeschlossen: {', '.join(missing)}")
            v = float(c.value)
            mask = {">": series > v, ">=": series >= v, "<": series < v, "<=": series <= v,
                    "==": series == v, "!=": series != v}[c.op] & series.notna()
        work = work[mask]
        conditions.append(_cond_text(c))
    shown = ["name"] + plan.columns + [c.column for c in plan.filters] + ([plan.sort_by] if plan.sort_by else [])
    shown = list(dict.fromkeys(shown))
    if plan.sort_by:
        work = work.sort_values(plan.sort_by, ascending=not plan.descending, na_position="last")
    total = len(work)
    work = work.head(plan.limit)
    if total > plan.limit:
        notes.append(f"{total} Treffer, " + ("der erste wird" if plan.limit == 1 else f"die ersten {plan.limit} werden")
                     + " angezeigt.")
    if work.empty:
        notes.append("Kein Unternehmen erfuellt die Bedingungen.")
    raw = work[shown]
    table = raw.copy().astype(object)
    for col in shown:
        table[col] = [fmt(CATALOG[col].kind, v) for v in raw[col]]
    table.index.name = "Ticker"
    table.columns = [CATALOG[c].label for c in shown]
    parts = ["Zeige " + (", ".join(CATALOG[c].label for c in plan.columns) if plan.columns else "Name")]
    if plan.tickers:
        parts.append("nur " + ", ".join(plan.tickers))
    if conditions:
        parts.append("Bedingungen (alle muessen zutreffen): " + " UND ".join(conditions))
    if plan.sort_by:
        parts.append(f"sortiert nach {CATALOG[plan.sort_by].label}, "
                     + ("absteigend" if plan.descending else "aufsteigend"))
    parts.append(f"hoechstens {plan.limit} " + ("Zeile" if plan.limit == 1 else "Zeilen"))
    return Result(plan, "; ".join(parts), table=table, raw=raw, notes=notes)


# ------------------------------------------------------------------ KI: Frage -> Plan
SYSTEM = """You translate ONE German question about a stock watchlist into a JSON query plan. You never answer \
the question yourself and never invent data; code executes your plan on a database. Reply with exactly one JSON \
object and nothing else (no markdown fences).

Schema:
{
  "kind": "table" | "text" | "unsupported",
  "columns": [column keys to show],          // besides the company name
  "filters": [ {"column": key, "op": ">"|">="|"<"|"<="|"=="|"!=", "value": number or string} ],
  "sort_by": column key or null, "descending": true|false, "limit": 1-50 (default 10),
  "tickers": [tickers] ,                      // restrict to these companies (empty = all)
  "text_section": "business"|"risks"|"risks_diff"|"mdna"|null,   // only for kind "text"
  "note": string|null                         // only for kind "unsupported": short German reason
}

Rules:
- Use ONLY column keys, tickers and sector names from the lists below.
- Percentages and ratios are FRACTIONS: 30 % -> 0.30. Changes in percentage points are fractions too: "3 Prozentpunkte" -> 0.03. \
Columns of type x are plain multiples (1.5 = 1.5x). Money is absolute USD: "5 Mrd." -> 5000000000.
- "Veraenderung ... ggue. Vorjahr" columns (chg_*): "sinkende Marge" -> filter chg_<margin> < 0; "steigende Schulden" -> chg_debt_to_equity > 0.
- Without a stated period, use the trailing-twelve-months columns (ttm_*). Use fy_* only if the question says Geschaeftsjahr.
- "Teuer"/"guenstig" refer to pe, ps or p_fcf. Prefer sorting over inventing thresholds when the question names none.
- kind "text": questions about what a report says (risks, business model, management view, new risks versus last year). Exactly one ticker.
- kind "unsupported": forecasts, buy/sell advice, companies or data not in the lists, anything else.

Examples:
Q: Welche Software-Titel haben eine Nettomarge ueber 30 % und sinkende Schulden?
A: {"kind":"table","columns":["ttm_net_margin","chg_debt_to_equity"],"filters":[{"column":"sector","op":"==","value":"Software"},{"column":"ttm_net_margin","op":">","value":0.30},{"column":"chg_debt_to_equity","op":"<","value":0}],"sort_by":"ttm_net_margin","descending":true,"limit":10,"tickers":[],"text_section":null,"note":null}
Q: Welche drei Titel sind am billigsten gemessen am KGV?
A: {"kind":"table","columns":["pe","price"],"filters":[],"sort_by":"pe","descending":false,"limit":3,"tickers":[],"text_section":null,"note":null}
Q: Welche Risiken nennt Microsoft?
A: {"kind":"text","columns":[],"filters":[],"sort_by":null,"descending":true,"limit":10,"tickers":["MSFT"],"text_section":"risks","note":null}
Q: Wird NVDA naechstes Jahr steigen?
A: {"kind":"unsupported","columns":[],"filters":[],"sort_by":null,"descending":true,"limit":10,"tickers":[],"text_section":null,"note":"Prognosen und Kursprognosen sind nicht moeglich."}"""


def build_prompt(df: pd.DataFrame, question: str, today: date | None = None) -> str:
    cols = "\n".join(f"{k} | {c.label} | {c.kind}" + (f" | {c.help}" if c.help else "") for k, c in CATALOG.items())
    names = "\n".join(f"{t} = {n}" for t, n in zip(df.index, df["name"].fillna("")))
    sectors = ", ".join(sorted(df["sector"].dropna().unique()))
    return (f"Date: {(today or date.today()).isoformat()}\nSectors: {sectors}\n\nTickers:\n{names}\n\n"
            f"Columns (key | label | type | meaning):\n{cols}\n\nQuestion: {question.strip()}")


def plan_question(llm: LLMClient, df: pd.DataFrame, question: str) -> Plan:
    """Fragt die KI nach einem Plan; bei ungueltigem Plan genau ein Wiederholungsversuch mit Fehlerhinweis."""
    prompt, error = build_prompt(df, question), ""
    tickers = set(df.index)
    for attempt in range(2):
        resp = llm.complete(SYSTEM, prompt + (f"\n\nYour previous plan was invalid ({error}). "
                                              "Reply again with only the corrected JSON object." if error else ""))
        try:
            return validate_plan(Plan.model_validate(extract_json(resp.text)), tickers)
        except (ValueError, ValidationError) as exc:  # PlanError ist ein ValueError
            error = str(exc)[:200]
            log.warning("Ungueltiger Abfrageplan (%s), Versuch %d", error, attempt + 1)
    raise LLMError(f"Die KI lieferte keinen gueltigen Abfrageplan: {error}")


def ask(session: Session, llm: LLMClient, question: str) -> Result:
    if not question.strip():
        raise ValueError("Leere Frage")
    df = question_table(session)
    if df.empty:
        raise LLMError("Noch keine Daten. Erst: python -m invest_research update")
    plan = plan_question(llm, df, question)
    log.info("Frage: %r -> %s", question, json.dumps(plan.model_dump(), ensure_ascii=False))
    return run_plan(df, plan, session)
