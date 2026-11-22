"""Lesende Abfragen fuer das Dashboard (kein Streamlit-Code, daher testbar)."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Analysis, Company, Event, Filing, Metric, Price, Profile
from .valuation import market_cap, p_fcf_ratio, pe_ratio, price_change, ps_ratio


def watchlist(session: Session) -> list[Company]:
    return list(session.scalars(
        select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker)))


def price_history(session: Session, company_id: int) -> pd.DataFrame:
    rows = session.execute(select(Price.date, Price.close).where(Price.company_id == company_id)
                           .order_by(Price.date)).all()
    return pd.DataFrame(rows, columns=["date", "close"])


def _ttm_values(session: Session, company_id: int) -> dict[str, float]:
    """TTM-Werte des juengsten TTM-Stichtags (Umsatz-Stichtag) plus juengste Aktienzahl."""
    ends = list(session.scalars(select(Metric.period_end).where(
        Metric.company_id == company_id, Metric.name == "revenue", Metric.period_type == "TTM")))
    out: dict[str, float] = {}
    if ends:
        end = max(ends)
        for m in session.scalars(select(Metric).where(
                Metric.company_id == company_id, Metric.period_end == end,
                Metric.period_type == "TTM", Metric.name.in_(["revenue", "eps_diluted", "fcf"]))):
            if m.value is not None:
                out[m.name] = m.value
    shares = session.execute(select(Metric.value).where(
        Metric.company_id == company_id, Metric.name == "shares_outstanding")
        .order_by(Metric.period_end.desc()).limit(1)).scalar()
    if shares:
        out["shares"] = shares
    return out


def overview(session: Session) -> pd.DataFrame:
    """Eine Zeile je Watchlist-Titel: Kurs, Entwicklung, Bewertung, Sparkline. Index = Ticker."""
    rows = []
    for c in watchlist(session):
        ph = price_history(session, c.id)
        hist = list(zip(ph["date"], ph["close"])) if not ph.empty else []
        price = hist[-1][1] if hist else None
        year = [x for x in hist if (hist[-1][0] - x[0]).days <= 365]
        pos52 = None
        if year:
            lo, hi = min(p for _, p in year), max(p for _, p in year)
            pos52 = (price - lo) / (hi - lo) if hi > lo else None
        t = _ttm_values(session, c.id)
        cap = market_cap(price, t.get("shares"))
        rows.append({
            "ticker": c.ticker, "name": c.name, "sector": c.sector, "price": price,
            "price_date": hist[-1][0] if hist else None,
            "chg_1m": price_change(hist, 30), "chg_ytd": price_change(hist, ytd=True),
            "chg_1y": price_change(hist, 365), "pos_52w": pos52, "market_cap": cap,
            "pe": pe_ratio(price, t.get("eps_diluted")), "ps": ps_ratio(cap, t.get("revenue")),
            "p_fcf": p_fcf_ratio(cap, t.get("fcf")),
            "spark": [p for _, p in year[-250:]],
        })
    return pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()


def annual_series(session: Session, company_id: int, names: list[str],
                  ptype: str = "FY") -> pd.DataFrame:
    """Lange Tabelle (period_end, name, value) fuer Diagramme."""
    rows = session.execute(select(Metric.period_end, Metric.name, Metric.value).where(
        Metric.company_id == company_id, Metric.period_type == ptype,
        Metric.name.in_(names), Metric.value.is_not(None)).order_by(Metric.period_end)).all()
    return pd.DataFrame(rows, columns=["period_end", "name", "value"])


def recent_filings(session: Session, company_id: int, limit: int = 15) -> pd.DataFrame:
    rows = session.execute(select(Filing.filed_at, Filing.form_type, Filing.period_end, Filing.url)
                           .where(Filing.company_id == company_id)
                           .order_by(Filing.filed_at.desc()).limit(limit)).all()
    return pd.DataFrame(rows, columns=["filed_at", "form_type", "period_end", "url"])


def events(session: Session, *, only_unseen: bool = False, limit: int = 100) -> pd.DataFrame:
    q = (select(Event.id, Company.ticker, Event.created_at, Event.kind, Event.severity,
                Event.title, Event.detail, Event.seen)
         .join(Company, Company.id == Event.company_id)
         .order_by(Event.created_at.desc()).limit(limit))
    if only_unseen:
        q = q.where(Event.seen.is_(False))
    return pd.DataFrame(session.execute(q).all(), columns=[
        "id", "ticker", "created_at", "kind", "severity", "title", "detail", "seen"])


def mark_events_seen(session: Session, ids: list[int]) -> None:
    for e in session.scalars(select(Event).where(Event.id.in_(ids))):
        e.seen = True
    session.commit()


SECTION_ORDER = {"business": 0, "risks": 1, "risks_diff": 2, "mdna": 3}


def latest_analyses(session: Session, ticker: str) -> list[dict]:
    """Aktuelle Analysen (neueste Prompt-Version je Filing/Abschnitt), neueste Filings zuerst."""
    q = (select(Analysis, Filing.form_type, Filing.filed_at, Filing.url)
         .join(Filing, Filing.id == Analysis.filing_id).join(Company, Company.id == Filing.company_id)
         .where(Company.ticker == ticker.upper())
         .order_by(Filing.filed_at.desc(), Analysis.prompt_version.desc()))
    seen, out = set(), []
    for a, form, filed, url in session.execute(q):
        key = (a.filing_id, a.section_key)
        if key in seen:
            continue
        seen.add(key)
        out.append({"form_type": form, "filed_at": filed, "url": url, "section": a.section_key,
                    "model": a.model, "created_at": a.created_at, "content": a.content})
    out.sort(key=lambda r: (-r["filed_at"].toordinal(), SECTION_ORDER.get(r["section"], 9)))
    return out


def profile_versions(session: Session, ticker: str) -> list[dict]:
    """Alle Profil-Versionen eines Unternehmens, neueste zuerst."""
    q = (select(Profile).join(Company, Company.id == Profile.company_id)
         .where(Company.ticker == ticker.upper()).order_by(Profile.created_at.desc(), Profile.id.desc()))
    return [{"id": p.id, "created_at": p.created_at, "model": p.model, "content": p.content}
            for p in session.scalars(q)]


def data_status(session: Session) -> pd.DataFrame:
    """Datenstand je Watchlist-Titel: Aktualitaet von Kurs, Bericht, Profil; Zahl der Analysen und offenen Alerts."""
    from sqlalchemy import func
    rows = []
    for c in watchlist(session):
        last_price = session.scalar(select(func.max(Price.date)).where(Price.company_id == c.id))
        filing = session.execute(select(Filing.form_type, Filing.filed_at).where(
            Filing.company_id == c.id, Filing.form_type.in_(["10-K", "10-Q"]))
            .order_by(Filing.filed_at.desc()).limit(1)).first()
        last_profile = session.scalar(select(func.max(Profile.created_at)).where(Profile.company_id == c.id))
        n_an = session.scalar(select(func.count()).select_from(Analysis).join(Filing, Filing.id == Analysis.filing_id)
                              .where(Filing.company_id == c.id)) or 0
        n_ev = session.scalar(select(func.count()).select_from(Event).where(
            Event.company_id == c.id, Event.seen.is_(False))) or 0
        rows.append({"ticker": c.ticker, "last_price": last_price,
                     "last_report": f"{filing[0]} {filing[1]}" if filing else None,
                     "last_profile": last_profile, "analyses": n_an, "open_alerts": n_ev})
    return pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()
