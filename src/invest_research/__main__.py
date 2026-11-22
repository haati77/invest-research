"""CLI:  python -m invest_research <init|ingest|compute|prices|update|compare|dashboard|status>"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .compute import compute_watchlist
from .config import load_settings, load_watchlist
from .db import get_engine, init_db, sync_watchlist
from .edgar import EdgarClient
from .ingest import ingest_watchlist
from .prices import YahooProvider, update_prices
from .engine import RATIOS
from .models import Company, Filing, Metric
from .peers import add_percentiles, format_value, overall_score, snapshot


def print_comparison(session: Session, basis: str, sector: str | None) -> None:
    df = snapshot(session, basis)
    if df.empty:
        print("Keine Daten. Erst 'ingest' und 'compute' ausfuehren.")
        return
    df = add_percentiles(df)
    df["score"] = overall_score(df)
    if sector:
        df = df[df["sector"].str.lower() == sector.lower()]
    label = "letztes Geschaeftsjahr" if basis == "FY" else "letzte 12 Monate (TTM)"
    print(f"Peer-Vergleich ({label}); Score = mittleres Branchen-Perzentil, 0-100, nur Orientierung\n")
    cols = ["revenue_growth", "gross_margin", "operating_margin", "net_margin", "fcf_margin", "roe", "debt_to_equity"]
    short = {"revenue_growth": "Umsatz-Wachst.", "gross_margin": "Bruttomarge", "operating_margin": "Op. Marge",
             "net_margin": "Nettomarge", "fcf_margin": "FCF-Marge", "roe": "EK-Rendite", "debt_to_equity": "Verb./EK"}
    for sec, grp in df.sort_values(["sector", "score"], ascending=[True, False]).groupby("sector", sort=True):
        print(f"== {sec} ==")
        header = f"{'Ticker':7}{'Stichtag':11}" + "".join(f"{short[c]:>15}" for c in cols) + f"{'Score':>7}"
        print(header)
        for ticker, r in grp.iterrows():
            vals = "".join(f"{format_value(c, r[c]):>15}" for c in cols)
            score = "-" if r["score"] != r["score"] else f"{r['score'] * 100:.0f}"
            print(f"{ticker:7}{str(r['period_end']):11}{vals}{score:>7}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(prog="invest_research")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Datenbank anlegen und Watchlist uebernehmen")
    p_ing = sub.add_parser("ingest", help="Filings und Kennzahlen von EDGAR laden")
    p_ing.add_argument("--ticker", help="nur diesen Ticker laden")
    p_ing.add_argument("--years", type=int, default=6, help="Anzahl Jahre Historie (Standard 6)")
    sub.add_parser("prices", help="Kursdaten laden (inkrementell)")
    p_upd = sub.add_parser("update", help="Alles aktualisieren: ingest + compute + prices")
    p_upd.add_argument("--years", type=int, default=6)
    sub.add_parser("dashboard", help="Dashboard im Browser starten")
    sub.add_parser("compute", help="Abgeleitete Kennzahlen berechnen (Q4, TTM, Quoten)")
    p_cmp = sub.add_parser("compare", help="Peer-Vergleich anzeigen")
    p_cmp.add_argument("--basis", choices=["FY", "TTM"], default="FY")
    p_cmp.add_argument("--sector", help="nur diese Branche")
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
        elif args.cmd == "prices":
            for ticker, msg in update_prices(session, YahooProvider()).items():
                print(f"{ticker:6} {msg}")
        elif args.cmd == "update":
            client = EdgarClient(settings.sec_user_agent)
            for r in ingest_watchlist(session, client, since_year=date.today().year - args.years):
                print(f"{r.ticker:6} " + (f"FEHLER: {r.error}" if r.error else
                                          f"{r.new_filings} neue Filings, {r.metrics_written} Kennzahlen"))
            compute_watchlist(session)
            print("Kennzahlen berechnet.")
            for ticker, msg in update_prices(session, YahooProvider()).items():
                print(f"{ticker:6} {msg}")
        elif args.cmd == "dashboard":
            import subprocess
            import sys
            from pathlib import Path
            app = Path(__file__).parent / "dashboard" / "app.py"
            subprocess.run([sys.executable, "-m", "streamlit", "run", str(app)], check=False)
        elif args.cmd == "compute":
            for ticker, n in compute_watchlist(session).items():
                print(f"{ticker:6} {n} abgeleitete Kennzahlen geschrieben/aktualisiert")
        elif args.cmd == "compare":
            print_comparison(session, args.basis, args.sector)
        elif args.cmd == "status":
            for c in session.scalars(select(Company).where(Company.on_watchlist.is_(True)).order_by(Company.ticker)):
                nf = session.scalar(select(func.count()).select_from(Filing).where(Filing.company_id == c.id))
                nm = session.scalar(select(func.count()).select_from(Metric).where(Metric.company_id == c.id))
                print(f"{c.ticker:6} {c.name or '-':32} Filings={nf:4}  Kennzahlen={nm:5}")


if __name__ == "__main__":
    main()
