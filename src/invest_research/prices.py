"""Kursdaten hinter einer austauschbaren Schnittstelle.

Standardquelle ist die öffentliche Yahoo-Finance-Chart-Schnittstelle (kostenlos, aber
inoffiziell: kann sich ändern oder blockieren). Wer wechseln will, implementiert
`PriceProvider.history` und übergibt den neuen Provider an `update_prices`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Company, Price

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceRow:
    date: date
    close: float
    adj_close: float | None
    volume: float | None


class PriceProvider(Protocol):
    def history(self, ticker: str, start: date) -> list[PriceRow]: ...


class PriceError(RuntimeError):
    pass


def parse_yahoo_chart(payload: dict) -> list[PriceRow]:
    chart = payload.get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise PriceError(f"Yahoo: {chart.get('error') or 'keine Daten'}")
    res = chart["result"][0]
    offset = res.get("meta", {}).get("gmtoffset", 0)
    quote = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for i, ts in enumerate(res.get("timestamp") or []):
        close = quote["close"][i]
        if close is None:  # Feiertage/Lücken
            continue
        day = datetime.fromtimestamp(ts + offset, tz=timezone.utc).date()
        a = adj[i] if i < len(adj) else None
        vol = quote["volume"][i] if i < len(quote.get("volume", [])) else None
        rows.append(PriceRow(day, float(close), None if a is None else float(a),
                             None if vol is None else float(vol)))
    return rows


class YahooProvider:
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, client: httpx.Client | None = None, max_retries: int = 2):
        self._client = client or httpx.Client(timeout=30.0)
        self._max_retries = max_retries

    def history(self, ticker: str, start: date) -> list[PriceRow]:
        symbol = ticker.replace(".", "-")  # BRK.B -> BRK-B
        params = {"period1": int(datetime(start.year, start.month, start.day,
                                          tzinfo=timezone.utc).timestamp()),
                  "period2": int(time.time()) + 86400, "interval": "1d"}
        for attempt in range(self._max_retries + 1):
            resp = self._client.get(self.URL.format(symbol=symbol), params=params,
                                    headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                return parse_yahoo_chart(resp.json())
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                time.sleep(2 ** attempt)
                continue
            raise PriceError(f"Yahoo {resp.status_code} für {ticker}")
        raise PriceError(f"Yahoo nicht erreichbar: {ticker}")  # pragma: no cover


def update_prices(session: Session, provider: PriceProvider, *, years: int = 5,
                  pause: float = 0.3) -> dict[str, str]:
    """Lädt fehlende Kurse nach (inkrementell). Gibt je Ticker 'N neu' oder 'FEHLER: ...' zurück."""
    report: dict[str, str] = {}
    companies = list(session.scalars(
        select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker)))
    for i, c in enumerate(companies):
        try:
            last = session.scalar(select(func.max(Price.date)).where(Price.company_id == c.id))
            start = last - timedelta(days=7) if last else date.today() - timedelta(days=365 * years)
            existing = {p.date: p for p in session.scalars(
                select(Price).where(Price.company_id == c.id, Price.date >= start))}
            new = 0
            for r in provider.history(c.ticker, start):
                p = existing.get(r.date)
                if p is None:
                    session.add(Price(company_id=c.id, date=r.date, close=r.close,
                                      adj_close=r.adj_close, volume=r.volume))
                    new += 1
                else:  # bereinigte Kurse ändern sich rückwirkend (Dividenden/Splits)
                    p.close, p.adj_close, p.volume = r.close, r.adj_close, r.volume
            session.commit()
            report[c.ticker] = f"{new} neue Kurse"
        except Exception as exc:  # ein Fehler stoppt nicht die übrigen Titel
            session.rollback()
            log.warning("Kurse fehlgeschlagen: %s: %s", c.ticker, exc)
            report[c.ticker] = f"FEHLER: {exc}"
        if i < len(companies) - 1:
            time.sleep(pause)
    return report
