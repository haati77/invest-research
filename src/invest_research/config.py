"""Konfiguration: Umgebungsvariablen (.env) und Watchlist (YAML)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class WatchlistEntry(BaseModel):
    ticker: str
    sector: str | None = None
    cik: str | None = None  # optional; sonst Aufloesung ueber SEC-Tickerliste


@dataclass(frozen=True)
class Settings:
    sec_user_agent: str
    database_path: Path
    watchlist_path: Path

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    db = Path(os.getenv("DATABASE_PATH", "data/research.db"))
    if not db.is_absolute():
        db = PROJECT_ROOT / db
    return Settings(
        sec_user_agent=os.getenv("SEC_USER_AGENT", ""),
        database_path=db,
        watchlist_path=PROJECT_ROOT / "config" / "watchlist.yaml",
    )


def load_watchlist(path: Path) -> list[WatchlistEntry]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = [WatchlistEntry(**c) for c in data.get("companies", [])]
    for e in entries:
        e.ticker = e.ticker.upper().strip()
    return entries
