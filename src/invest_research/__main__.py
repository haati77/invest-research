"""CLI:  python -m invest_research <init|ingest|compute|prices|update|analyze|show|profile|show-profile|compare|dashboard|status>"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .analysis import analyze_watchlist
from .compute import compute_watchlist
from .config import load_settings, load_watchlist
from .db import get_engine, init_db, sync_watchlist
from .edgar import EdgarClient
from .ingest import ingest_watchlist
from .llm import ClaudeCLI, LLMError
from .profiles import build_profiles, render_markdown
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


def print_analysis(session: Session, ticker: str) -> None:
    from .queries import latest_analyses
    rows = latest_analyses(session, ticker)
    if not rows:
        print(f"Keine Analyse fuer {ticker.upper()}. Erst: python -m invest_research analyze --ticker {ticker.upper()}")
        return
    for r in rows:
        c = r["content"]
        print(f"\n=== {ticker.upper()} {r['form_type']} ({r['filed_at']}) - {r['section']} "
              f"[{c['n_verified']}/{c['n_items']} Zitate verifiziert] ===")
        print(f"Zusammenfassung (nicht zitatgeprueft): {c['summary']}")
        if c.get("tone"):
            print(f"Ton: {c['tone']}" + ("" if c["tone_verified"] else " (Zitat nicht verifiziert)"))
        for it in sorted(c["items"], key=lambda i: -i["importance"]):
            mark = "OK " if it["verified"] else "?? "
            print(f"  {mark}[{it['kind']}] {it['title']}: {it['statement']}")
            if it["verified"]:
                print(f"      Zitat: \"{it['quote']}\"")


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
    p_an = sub.add_parser("analyze", help="KI-Analyse neuester 10-K/10-Q (per 'claude -p', zitatgeprueft)")
    p_an.add_argument("--ticker", help="nur diesen Ticker")
    p_an.add_argument("--max-calls", type=int, default=12, help="max. KI-Aufrufe je Lauf (Standard 12)")
    p_an.add_argument("--model", default="haiku", help="Modell fuer claude -p (Standard haiku; sonnet = gruendlicher, mehr Abo-Verbrauch)")
    p_an.add_argument("--force", action="store_true", help="vorhandene Analysen neu erstellen")
    p_show = sub.add_parser("show", help="KI-Analyse eines Unternehmens anzeigen")
    p_show.add_argument("ticker")
    p_pr = sub.add_parser("profile", help="Unternehmensprofile erstellen (neue Version nur bei geaenderten Eingaben)")
    p_pr.add_argument("--ticker", help="nur diesen Ticker")
    p_pr.add_argument("--force", action="store_true", help="auch bei unveraenderten Eingaben neue Version")
    p_pr.add_argument("--no-ai", action="store_true", help="ohne KI-Synthese (kein claude noetig)")
    p_pr.add_argument("--model", default="haiku", help="Modell fuer die Synthese (Standard haiku)")
    p_sp = sub.add_parser("show-profile", help="Neuestes Profil als Markdown ausgeben")
    p_sp.add_argument("ticker")
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
        elif args.cmd == "analyze":
            try:
                llm = ClaudeCLI(model=args.model)
            except LLMError as exc:
                raise SystemExit(str(exc))
            reports, pending = analyze_watchlist(session, llm, EdgarClient(settings.sec_user_agent),
                                                 max_calls=args.max_calls, only_ticker=args.ticker, force=args.force)
            for r in reports:
                detail = f"{r.n_verified}/{r.n_items} Zitate verifiziert" if r.status in ("ok", "uebersprungen") else ""
                print(f"{r.ticker:6} {r.form_type:5} {r.section:9} {r.status} {detail}")
            if pending:
                print(f"\n{pending} Abschnitte noch offen: 'analyze' erneut ausfuehren (Fortschritt ist gespeichert).")
        elif args.cmd == "show":
            print_analysis(session, args.ticker)
        elif args.cmd == "profile":
            llm = None
            if not args.no_ai:
                try:
                    llm = ClaudeCLI(model=args.model)
                except LLMError as exc:
                    raise SystemExit(f"{exc}\n(Oder mit --no-ai ohne KI-Synthese.)")
            for r in build_profiles(session, llm, only_ticker=args.ticker, force=args.force):
                extra = ""
                if r.status == "neu":
                    extra = " (mit KI-Synthese" + (f", {r.dropped} Aussagen mangels Belegen verworfen" if r.dropped else "") + ")" \
                        if r.ai else " (ohne KI-Synthese)"
                print(f"{r.ticker:6} {r.status}{extra}")
        elif args.cmd == "show-profile":
            from .queries import profile_versions
            versions = profile_versions(session, args.ticker)
            if not versions:
                raise SystemExit(f"Kein Profil fuer {args.ticker.upper()}. Erst: python -m invest_research profile --ticker {args.ticker.upper()}")
            print(render_markdown(versions[0]["content"], versions[0]["created_at"]))
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
