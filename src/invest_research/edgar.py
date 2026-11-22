"""Dünner Client für SEC EDGAR (kostenlos, offiziell).

Regeln der SEC: identifizierender User-Agent Pflicht, max. 10 Requests/Sekunde.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

import httpx

DATA_HOST = "https://data.sec.gov"
WWW_HOST = "https://www.sec.gov"


class EdgarError(RuntimeError):
    pass


def pad_cik(cik: int | str) -> str:
    return f"{int(cik):010d}"


@dataclass(frozen=True)
class FilingInfo:
    accession_number: str
    form_type: str
    filed_at: date
    period_end: date | None
    url: str


class EdgarClient:
    def __init__(self, user_agent: str, *, min_interval: float = 0.15,
                 client: httpx.Client | None = None, max_retries: int = 3):
        if not user_agent.strip():
            raise EdgarError(
                "SEC_USER_AGENT ist leer. Trage in .env 'Name email@example.com' ein; "
                "die SEC blockiert Anfragen ohne identifizierenden User-Agent."
            )
        self._client = client or httpx.Client(timeout=30.0)
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_request = 0.0

    def _get_json(self, url: str) -> dict:
        return self._get(url).json()

    def get_document(self, url: str) -> str:
        """Lädt ein Filing-Dokument (HTML) als Text; httpx entpackt gzip automatisch."""
        return self._get(url).text

    def _get(self, url: str) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = self._client.get(url, headers=self._headers)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                time.sleep(2 ** attempt)
                continue
            raise EdgarError(f"EDGAR {resp.status_code} für {url}")
        raise EdgarError(f"EDGAR nicht erreichbar: {url}")  # pragma: no cover

    def ticker_map(self) -> dict[str, str]:
        """Ticker -> CIK (10-stellig). Fallback, falls kein CIK in der Watchlist steht."""
        data = self._get_json(f"{WWW_HOST}/files/company_tickers.json")
        return {row["ticker"].upper(): pad_cik(row["cik_str"]) for row in data.values()}

    def submissions(self, cik: str) -> dict:
        return self._get_json(f"{DATA_HOST}/submissions/CIK{pad_cik(cik)}.json")

    def company_facts(self, cik: str) -> dict:
        return self._get_json(f"{DATA_HOST}/api/xbrl/companyfacts/CIK{pad_cik(cik)}.json")


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def parse_filings(submissions: dict, forms: set[str], since_year: int) -> list[FilingInfo]:
    """Liest die 'recent'-Filings (ca. letzte 1000) aus der Submissions-Antwort."""
    cik = int(submissions["cik"])
    recent = submissions.get("filings", {}).get("recent", {})
    out: list[FilingInfo] = []
    for i, accn in enumerate(recent.get("accessionNumber", [])):
        form = recent["form"][i]
        filed = _d(recent["filingDate"][i])
        if form not in forms or filed is None or filed.year < since_year:
            continue
        doc = recent["primaryDocument"][i]
        url = f"{WWW_HOST}/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}" if doc else ""
        out.append(FilingInfo(accn, form, filed, _d(recent["reportDate"][i] or None), url))
    return out
