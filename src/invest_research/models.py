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


class Profile(Base):
    """Versioniertes KI-Unternehmensprofil."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    content: Mapped[dict] = mapped_column(JSON)  # strukturiertes Profil inkl. Zitate
    based_on_filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))

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

    company: Mapped[Company] = relationship(back_populates="events")
