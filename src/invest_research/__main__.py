"""CLI:  python -m invest_research <init|ingest|status>"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import load_settings, load_watchlist
from .db import get_engine, init_db, sync_watchlist
from .edgar import EdgarClient
from .ingest import ingest_watchlist
from .models import Company, Filing, Metric


def main() -> None:
    parser = argparse.ArgumentParser(prog="invest_research")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Datenbank anlegen und Watchlist uebernehmen")
    p_ing = sub.add_parser("ingest", help="Filings und Kennzahlen von EDGAR laden")
    p_ing.add_argument("--ticker", help="nur diesen Ticker laden")
    p_ing.add_argument("--years", type=int, default=6, help="Anzahl Jahre Historie (Standard 6)")
    sub.add_parser("status", help="Datenbestand anzeigen")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    engine = get_engine(settings)
    init_db(engine)

    with Session(engine) as session:
        sync_watchlist(session, load_watchlist(settings.watchlist_path))
        if args.cmd == "init":
            print(f"Datenbank bereit: {settings.database_path}")
        elif args.cmd == "ingest":
            client = EdgarClient(settings.sec_user_agent)
            since = date.today().year - args.years
            for r in ingest_watchlist(session, client, since_year=since, only_ticker=args.ticker):
                status = f"FEHLER: {r.error}" if r.error else f"{r.new_filings} neue Filings, {r.metrics_written} Kennzahlen"
                print(f"{r.ticker:6} {status}")
        elif args.cmd == "status":
            for c in session.scalars(select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker)):
                nf = session.scalar(select(func.count()).select_from(Filing).where(Filing.company_id == c.id))
                nm = session.scalar(select(func.count()).select_from(Metric).where(Metric.company_id == c.id))
                print(f"{c.ticker:6} {c.name or '-':32} Filings={nf:4}  Kennzahlen={nm:5}")


if __name__ == "__main__":
    main()
