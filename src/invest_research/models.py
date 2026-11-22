"""Datenmodell (SQLAlchemy 2.0).

Zahlen (Metric) stammen immer aus Code/XBRL, Texte (Profile, Event) aus der KI
mit Quellenverweisen.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    cik: Mapped[str | None] = mapped_column(String(10))  # 10-stellig, mit Nullen
    name: Mapped[str | None] = mapped_column(String(200))
    sector: Mapped[str | None] = mapped_column(String(100))
    on_watchlist: Mapped[bool] = mapped_column(default=True)

    filings: Mapped[list[Filing]] = relationship(back_populates="company", cascade="all, delete-orphan")
    metrics: Mapped[list[Metric]] = relationship(back_populates="company", cascade="all, delete-orphan")
    profiles: Mapped[list[Profile]] = relationship(back_populates="company", cascade="all, delete-orphan")
    events: Mapped[list[Event]] = relationship(back_populates="company", cascade="all, delete-orphan")


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (UniqueConstraint("company_id", "accession_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    accession_number: Mapped[str] = mapped_column(String(25))
    form_type: Mapped[str] = mapped_column(String(10))  # 10-K, 10-Q, 8-K ...
    filed_at: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str | None] = mapped_column(String(500))
    local_path: Mapped[str | None] = mapped_column(String(500))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped[Company] = relationship(back_populates="filings")


class FilingSection(Base):
    """Extrahierter Textabschnitt eines Filings (business | risks | mdna)."""

    __tablename__ = "filing_sections"
    __table_args__ = (UniqueConstraint("filing_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    key: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)


class Analysis(Base):
    """KI-Auswertung eines Abschnitts (zitatgeprueft). Pro Filing/Abschnitt/Prompt-Version einmal."""

    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("filing_id", "section_key", "prompt_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    section_key: Mapped[str] = mapped_column(String(20))
    prompt_version: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    content: Mapped[dict] = mapped_column(JSON)
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    cost_usd: Mapped[float | None] = mapped_column(Float)


class Metric(Base):
    """Eine Kennzahl pro Unternehmen und Periode (Rohwert aus XBRL oder berechnet)."""

    __tablename__ = "metrics"
    __table_args__ = (UniqueConstraint("company_id", "name", "period_end", "period_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(80), index=True)  # z. B. "Revenues", "gross_margin"
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(20))  # USD, ratio, pct ...
    period_end: Mapped[date] = mapped_column(Date)
    period_type: Mapped[str] = mapped_column(String(10))  # FY, Q, TTM, instant
    source: Mapped[str] = mapped_column(String(40), default="xbrl")  # xbrl | computed

    company: Mapped[Company] = relationship(back_populates="metrics")


class Price(Base):
    """Tagesschlusskurs (close = unbereinigt, adj_close = um Splits/Dividenden bereinigt)."""

    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("company_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    close: Mapped[float] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)


class LLMCall(Base):
    """Protokoll jedes KI-Aufrufs (Nutzungsanzeige und Limits)."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    purpose: Mapped[str] = mapped_column(String(20))  # analyze | watch | profile | ask | check
    model: Mapped[str] = mapped_column(String(60))
    ok: Mapped[bool] = mapped_column(default=True)
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    cost_usd: Mapped[float | None] = mapped_column(Float)  # Listenpreis laut claude, bei Abo nicht berechnet
    duration_s: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(400))


class Profile(Base):
    """Versioniertes KI-Unternehmensprofil."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    content: Mapped[dict] = mapped_column(JSON)  # strukturiertes Profil inkl. Zitate
    based_on_filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))
    input_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # Eingaben (Kennzahlen, Analysen)
    model: Mapped[str | None] = mapped_column(String(60))  # None = Profil ohne KI-Synthese
    prompt_version: Mapped[str | None] = mapped_column(String(20))

    company: Mapped[Company] = relationship(back_populates="profiles")


class Event(Base):
    """Erkannte Veraenderung bzw. Alert (neues Filing, Kennzahlensprung ...)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    kind: Mapped[str] = mapped_column(String(40))  # new_filing | metric_change | text_change
    severity: Mapped[int] = mapped_column(default=1)  # 1 niedrig .. 3 hoch
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)
    seen: Mapped[bool] = mapped_column(default=False)
    # Eindeutiger Schluessel je Ereignis (z. B. 'metric:net_margin:TTM:2026-09-30'): wiederholte Laeufe
    # erzeugen dasselbe Ereignis nicht doppelt. None bei aelteren Ereignissen.
    dedupe_key: Mapped[str | None] = mapped_column(String(160), index=True)

    company: Mapped[Company] = relationship(back_populates="events")
