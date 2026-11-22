"""Veraenderungserkennung: Kennzahlensprung, Kursereignis, Textaenderung -> Events (Alerts).

Alle Schwellen stehen hier an einer Stelle. Jedes Ereignis hat einen dedupe_key, damit ein
erneuter Lauf nichts doppelt meldet. Zahlen in Titeln stammen aus dem Code, nie von der KI.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analysis import Analysis, ask_model, ensure_sections, verify
from .edgar import EdgarClient
from .engine import RATIOS
from .llm import LLMClient
from .models import Company, Event, Filing, Metric, Price
from .textdiff import diff_sections

log = logging.getLogger(__name__)

DIFF_VERSION = "d1"
DIFF_SECTION = "risks_diff"


@dataclass(frozen=True)
class Rule:
    unit: str                 # "pp" = Prozentpunkte-Aenderung einer Quote, "x" = Aenderung eines Faktors
    medium: float
    high: float
    sign_flip: bool = False   # Wechsel ins Negative / zurueck ins Positive melden


# Vergleich: letzte 12 Monate (TTM) gegen dieselben 12 Monate ein Jahr frueher
METRIC_RULES: dict[str, Rule] = {
    "gross_margin": Rule("pp", 3, 6),
    "operating_margin": Rule("pp", 3, 6, sign_flip=True),
    "net_margin": Rule("pp", 3, 6, sign_flip=True),
    "fcf_margin": Rule("pp", 4, 8, sign_flip=True),
    "revenue_growth": Rule("pp", 5, 10, sign_flip=True),
    "roe": Rule("pp", 5, 10),
    "debt_to_equity": Rule("x", 0.3, 0.6),
}
PRICE_MOVE_MEDIUM = 0.07   # Tagesbewegung ab 7 % -> mittel
PRICE_MOVE_HIGH = 0.12     # ab 12 % -> hoch
PRICE_LOOKBACK_DAYS = 5    # so viele letzte Handelstage werden geprueft (verpasste Laeufe)
TEXT_MIN_ADDED_CHARS = 800  # darunter gilt die Risikofaktoren-Aenderung als unerheblich
STALE_DAYS = 90  # Kennzahlenmeldungen zu aelteren Stichtagen sind keine frischen Nachrichten (-> gelesen)


def add_event(session: Session, company: Company, *, kind: str, severity: int, title: str,
              detail: str | None, key: str, seen: bool = False) -> bool:
    """Legt ein Ereignis an, falls der Schluessel neu ist. True = neu angelegt."""
    exists = session.scalar(select(Event.id).where(Event.company_id == company.id, Event.dedupe_key == key))
    if exists:
        return False
    session.add(Event(company_id=company.id, kind=kind, severity=severity, title=title[:200],
                      detail=detail, dedupe_key=key, seen=seen))
    return True


# ------------------------------------------------------------------ Kennzahlensprung
def _fmt(name: str, v: float) -> str:
    return f"{v * 100:.1f}%" if RATIOS[name][2] == "pct" else f"{v:.2f}x"


def classify_change(rule: Rule, old: float, new: float, higher_is_better: bool = True
                    ) -> tuple[int, str, bool] | None:
    """(Wichtigkeit, Art, verbessert) oder None. Art: 'flip_neg' | 'flip_pos' | 'change'.

    Verschlechterungen behalten ihre volle Wichtigkeit, Verbesserungen werden um eine Stufe
    herabgestuft (mindestens 1): Eine Watchlist soll zuerst auf Probleme hinweisen.
    """
    if rule.sign_flip:
        if old >= 0 > new:
            return 3, "flip_neg", False
        if old < 0 <= new:
            return 2, "flip_pos", True
    delta = (new - old) * (100 if rule.unit == "pp" else 1)
    size = abs(delta)
    severity = 3 if size >= rule.high else 2 if size >= rule.medium else 0
    if not severity:
        return None
    improved = (delta > 0) == higher_is_better
    return (max(1, severity - 1) if improved else severity), "change", improved


def detect_metric_changes(session: Session, company: Company) -> int:
    ends = sorted(session.scalars(select(Metric.period_end).where(
        Metric.company_id == company.id, Metric.name == "revenue", Metric.period_type == "TTM")))
    if not ends:
        return 0
    now_end = ends[-1]
    prev_end = next((e for e in reversed(ends) if 350 <= (now_end - e).days <= 380), None)
    if prev_end is None:
        return 0
    vals: dict[tuple[str, date], float] = {}
    for m in session.scalars(select(Metric).where(
            Metric.company_id == company.id, Metric.period_type == "TTM",
            Metric.name.in_(list(METRIC_RULES)), Metric.period_end.in_([now_end, prev_end]))):
        if m.value is not None:
            vals[(m.name, m.period_end)] = m.value
    created = 0
    for name, rule in METRIC_RULES.items():
        old, new = vals.get((name, prev_end)), vals.get((name, now_end))
        if old is None or new is None:
            continue
        cls = classify_change(rule, old, new, RATIOS[name][1])
        if cls is None:
            continue
        severity, art, improved = cls
        label = RATIOS[name][0]
        delta = (new - old) * (100 if rule.unit == "pp" else 1)
        unit = " Pp" if rule.unit == "pp" else "x"
        move = f"{_fmt(name, old)} -> {_fmt(name, new)}"
        suffix = {"flip_neg": " (ins Negative gedreht)", "flip_pos": " (wieder positiv)",
                  "change": f" ({delta:+.1f}{unit}, {'besser' if improved else 'schlechter'})"}[art]
        title = f"{label}: {move}{suffix}"
        detail = (f"Letzte 12 Monate bis {now_end} gegenueber den 12 Monaten bis {prev_end}. "
                  f"Berechnet aus XBRL-Kennzahlen, keine KI-Aussage.")
        stale = (date.today() - now_end).days > STALE_DAYS
        if stale:
            detail += f" Stichtag liegt mehr als {STALE_DAYS} Tage zurueck: automatisch als gelesen markiert."
        created += add_event(session, company, kind="metric_change", severity=severity, title=title,
                             detail=detail, key=f"metric:{name}:TTM:{now_end}", seen=stale)
    return created


# ------------------------------------------------------------------ Kursereignis
def detect_price_moves(session: Session, company: Company) -> int:
    rows = session.execute(select(Price.date, Price.close).where(Price.company_id == company.id)
                           .order_by(Price.date.desc()).limit(PRICE_LOOKBACK_DAYS + 1)).all()
    created = 0
    for (day, close), (_, prev_close) in zip(rows, rows[1:]):
        if not prev_close:
            continue
        move = close / prev_close - 1
        if abs(move) < PRICE_MOVE_MEDIUM:
            continue
        severity = 3 if abs(move) >= PRICE_MOVE_HIGH else 2
        if move > 0:
            severity -= 1  # Kursgewinne sind seltener ein Problem als Kursverluste
        created += add_event(
            session, company, kind="price_move", severity=severity,
            title=f"Kurs {move * 100:+.1f}% am {day} ({prev_close:.2f} -> {close:.2f} $)",
            detail="Tagesbewegung gegenueber dem vorigen Handelstag; Grund nicht ermittelt.",
            key=f"price:{day}")
    return created


def detect_all_numeric(session: Session, only_ticker: str | None = None) -> dict[str, int]:
    """Kennzahlen- und Kursereignisse fuer die Watchlist (ohne Netz, ohne KI)."""
    out = {"metric_change": 0, "price_move": 0}
    for c in session.scalars(select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker)):
        if only_ticker and c.ticker != only_ticker.upper():
            continue
        out["metric_change"] += detect_metric_changes(session, c)
        out["price_move"] += detect_price_moves(session, c)
    session.commit()
    return out


# ------------------------------------------------------------------ Textaenderung (Risikofaktoren)
@dataclass
class TextChangeReport:
    ticker: str
    status: str   # neu | uebersprungen | keine Aenderung | FEHLER: ...
    n_added: int = 0
    n_removed: int = 0
    n_verified: int = 0


def _two_latest_10k(session: Session, company: Company) -> tuple[Filing, Filing] | None:
    rows = list(session.scalars(select(Filing).where(Filing.company_id == company.id, Filing.form_type == "10-K")
                                .order_by(Filing.filed_at.desc()).limit(2)))
    return (rows[0], rows[1]) if len(rows) == 2 else None


def detect_text_changes(session: Session, llm: LLMClient | None, edgar: EdgarClient, *,
                        only_ticker: str | None = None, max_calls: int = 6, force: bool = False
                        ) -> list[TextChangeReport]:
    """Vergleicht die Risikofaktoren des neuesten 10-K mit dem des Vorjahres.

    Der Absatzvergleich ist deterministisch. Mit LLM werden die neuen Absaetze zusammengefasst und
    nach Wesentlichkeit geordnet (Zitate werden gegen den NEUEN Text geprueft). Ohne LLM bleibt es bei
    der Absatzstatistik. Ergebnis je (neues 10-K, Prompt-Version) einmal gespeichert.
    """
    reports, calls = [], 0
    companies = list(session.scalars(select(Company).where(Company.on_watchlist.is_(True))
                                     .order_by(Company.ticker)))
    for company in companies:
        if only_ticker and company.ticker != only_ticker.upper():
            continue
        pair = _two_latest_10k(session, company)
        if not pair:
            reports.append(TextChangeReport(company.ticker, "uebersprungen (weniger als zwei 10-K)"))
            continue
        new, old = pair
        existing = session.scalar(select(Analysis).where(
            Analysis.filing_id == new.id, Analysis.section_key == DIFF_SECTION,
            Analysis.prompt_version == DIFF_VERSION))
        want_ai = llm is not None
        if existing and not force and (existing.model != "none" or not want_ai):
            reports.append(TextChangeReport(company.ticker, "uebersprungen (schon vorhanden)",
                                            existing.content["diff_stats"]["n_added"]))
            continue
        try:
            new_sec = ensure_sections(session, edgar, new).get("risks")
            old_sec = ensure_sections(session, edgar, old).get("risks")
            if not new_sec or not old_sec:
                reports.append(TextChangeReport(company.ticker, "FEHLER: Risikofaktoren nicht erkannt"))
                continue
            diff = diff_sections(old_sec, new_sec)
            stats = {"n_added": len(diff.added), "n_removed": len(diff.removed), "added_chars": diff.added_chars,
                     "n_new_paragraphs": diff.n_new, "n_old_paragraphs": diff.n_old,
                     "old_filing": str(old.filed_at), "new_filing": str(new.filed_at)}
            content = {"summary": "", "items": [], "tone": None, "tone_verified": False, "tone_quote": None,
                       "truncated": False, "n_items": 0, "n_verified": 0, "low_quality": False,
                       "diff_stats": stats, "added_paragraphs": diff.added[:60]}
            model = "none"
            if want_ai and diff.added_chars >= TEXT_MIN_ADDED_CHARS and calls < max_calls:
                calls += 1
                added_text = "\n\n".join(diff.added)
                header = (f"Company: {company.name or company.ticker} ({company.ticker})\n"
                          f"Document: Risk Factors of the 10-K filed {new.filed_at}; the text below contains only "
                          f"paragraphs that are new or substantially reworded compared with the 10-K filed {old.filed_at}")
                result, resp, truncated = ask_model(llm, DIFF_SECTION, added_text, header=header)
                ai = verify(result, added_text, truncated=truncated)
                content.update({k: ai[k] for k in ("summary", "items", "n_items", "n_verified", "low_quality",
                                                   "truncated")})
                model = resp.model
            if existing:
                session.delete(existing)
                session.flush()
            session.add(Analysis(filing_id=new.id, section_key=DIFF_SECTION, prompt_version=DIFF_VERSION,
                                 model=model, content=content))
            created = _text_change_event(session, company, new, old, content)
            session.commit()
            reports.append(TextChangeReport(company.ticker, "neu" if created else "keine Aenderung",
                                            stats["n_added"], stats["n_removed"], content["n_verified"]))
        except Exception as exc:
            session.rollback()
            log.warning("Textvergleich fehlgeschlagen: %s: %s", company.ticker, exc)
            reports.append(TextChangeReport(company.ticker, f"FEHLER: {exc}"))
    return reports


def _text_change_event(session: Session, company: Company, new: Filing, old: Filing, content: dict) -> bool:
    stats = content["diff_stats"]
    if stats["added_chars"] < TEXT_MIN_ADDED_CHARS:
        return False
    verified = [i for i in content["items"] if i["verified"]]
    if verified:
        severity = 3 if any(i["importance"] == 3 for i in verified) else 2
        detail = content["summary"] + "\n\n" + "\n".join(f"- {i['title']}" for i in verified[:6])
    else:
        severity = 2 if stats["added_chars"] >= 5000 else 1
        detail = ("Absatzvergleich ohne KI-Einordnung. Mit 'watch' ohne --no-ai erscheint eine "
                  "Zusammenfassung der wesentlichen Aenderungen.")
    title = (f"Risikofaktoren geaendert: {stats['n_added']} neue/geaenderte Absaetze, "
             f"{stats['n_removed']} entfallen (10-K {new.filed_at} vs. {old.filed_at})")
    key = f"textdiff:risks:{new.accession_number}"
    existing = session.scalar(select(Event).where(Event.company_id == company.id, Event.dedupe_key == key))
    if existing:  # Aufwertung von 'ohne KI' auf 'mit KI': Ereignis aktualisieren statt doppeln
        existing.severity, existing.detail, existing.title = severity, detail, title[:200]
        return False
    return add_event(session, company, kind="text_change", severity=severity, title=title, detail=detail, key=key)
