"""CLI:  python -m invest_research <init|ingest|compute|prices|update|watch|alerts|analyze|show|profile|show-profile|usage|check-claude|compare|dashboard|status>"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .analysis import analyze_watchlist
from .changes import detect_all_numeric, detect_text_changes
from .compute import compute_watchlist
from .config import load_settings, load_watchlist
from .db import get_engine, init_db, sync_watchlist
from .edgar import EdgarClient
from .ingest import ingest_watchlist
from .llm import ClaudeCLI, LLMError, diagnose
from .logsetup import Tee, setup_logging
from .usage import Budget, MeteredLLM, usage_summary
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


def print_alerts(session: Session, *, only_unseen: bool, min_severity: int, mark_seen: bool) -> None:
    from .queries import events, mark_events_seen
    ev = events(session, only_unseen=only_unseen)
    ev = ev[ev["severity"] >= min_severity]
    if ev.empty:
        print("\nKeine Alerts." if only_unseen else "\nKeine Alerts vorhanden.")
        return
    label = {3: "HOCH", 2: "MITTEL", 1: "niedrig"}
    print(f"\n{len(ev)} Alert(s):")
    for _, r in ev.sort_values(["severity", "created_at"], ascending=[False, False]).iterrows():
        print(f"  [{label[int(r['severity'])]:7}] {r['ticker']:5} {r['title']}")
    if mark_seen:
        mark_events_seen(session, [int(i) for i in ev["id"]])
        print("Als gelesen markiert.")


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


DATA_COMMANDS = {"init", "ingest", "prices", "update", "compute", "analyze", "watch", "profile"}
log = logging.getLogger("invest_research")


def make_llm(model: str, purpose: str, engine, settings) -> MeteredLLM:
    """claude -p mit Nutzungsprotokoll und Tageslimit. Wirft LLMError, wenn claude fehlt."""
    budget = Budget(settings.llm_max_calls_per_day, settings.llm_max_cost_per_month)
    return MeteredLLM(ClaudeCLI(model=model), engine, purpose, budget)


def print_usage(session: Session, settings) -> None:
    u = usage_summary(session, Budget(settings.llm_max_calls_per_day, settings.llm_max_cost_per_month))
    print(f"Heute: {u['calls_today']} von {u['max_calls_per_day']} erlaubten KI-Aufrufen")
    cap = f" von {u['max_cost_per_month']:.2f} USD" if u["max_cost_per_month"] else " (kein Limit gesetzt)"
    print(f"Dieser Monat: {u['cost_month']:.3f} USD Listenpreis{cap}; bei Abo wird nichts berechnet, "
          "es zaehlt dein Nutzungskontingent.")
    for r in u["by_purpose"]:
        print(f"  {r['purpose']:8} {r['calls']:4} Aufrufe  {r['input_tokens']:>9,} Token rein  "
              f"{r['output_tokens']:>8,} raus  {r['cost_usd']:.3f} USD")
    if u["failed_month"]:
        print(f"Fehlgeschlagene Aufrufe in diesem Monat: {u['failed_month']}")
        for e in u["recent_errors"]:
            print(f"  {e['at']:%Y-%m-%d %H:%M} {e['purpose']:8} {e['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="invest_research")
    parser.add_argument("-v", "--verbose", action="store_true", help="auch Info-Meldungen auf der Konsole (Logdatei hat sie immer)")
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
    p_w = sub.add_parser("watch", help="Veraenderungen erkennen: Kennzahlensprung, Kursereignis, Risikotext-Vergleich")
    p_w.add_argument("--ticker", help="nur diesen Ticker")
    p_w.add_argument("--no-text", action="store_true", help="Textvergleich der Risikofaktoren auslassen (kein Download)")
    p_w.add_argument("--no-ai", action="store_true", help="Textvergleich ohne KI-Einordnung (kein claude noetig)")
    p_w.add_argument("--max-calls", type=int, default=6, help="max. KI-Aufrufe je Lauf (Standard 6)")
    p_w.add_argument("--model", default="haiku")
    p_al = sub.add_parser("alerts", help="Alerts im Terminal anzeigen")
    p_al.add_argument("--all", action="store_true", help="auch bereits gelesene")
    p_al.add_argument("--min-severity", type=int, default=1, choices=[1, 2, 3])
    p_al.add_argument("--mark-seen", action="store_true", help="angezeigte Alerts als gelesen markieren")
    p_cc = sub.add_parser("check-claude", help="Anbindung an 'claude -p' pruefen (bei KI-Fehlern zuerst ausfuehren)")
    p_cc.add_argument("--model", default="haiku")
    sub.add_parser("dashboard", help="Dashboard im Browser starten")
    sub.add_parser("compute", help="Abgeleitete Kennzahlen berechnen (Q4, TTM, Quoten)")
    p_cmp = sub.add_parser("compare", help="Peer-Vergleich anzeigen")
    p_cmp.add_argument("--basis", choices=["FY", "TTM"], default="FY")
    p_cmp.add_argument("--sector", help="nur diese Branche")
    sub.add_parser("usage", help="KI-Nutzung und Limits anzeigen")
    sub.add_parser("status", help="Datenbestand anzeigen")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.log_path, verbose=args.verbose)
    log.info("Start: %s", " ".join(sys.argv[1:]) or args.cmd)
    if args.cmd in DATA_COMMANDS:  # Ausgabezeilen datenaendernder Befehle landen auch in der Logdatei
        sys.stdout = Tee(sys.stdout, log)
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
            n = detect_all_numeric(session)
            print(f"Alerts: {n['metric_change']} Kennzahlensprung, {n['price_move']} Kursereignis neu "
                  "(Anzeigen: alerts; Risikotext-Vergleich: watch)")
        elif args.cmd == "analyze":
            try:
                llm = make_llm(args.model, "analyze", engine, settings)
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
                    llm = make_llm(args.model, "profile", engine, settings)
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
        elif args.cmd == "watch":
            n = detect_all_numeric(session, args.ticker)
            print(f"Kennzahlensprung: {n['metric_change']} neu | Kursereignis: {n['price_move']} neu")
            if not args.no_text:
                llm = None
                if not args.no_ai:
                    try:
                        llm = make_llm(args.model, "watch", engine, settings)
                    except LLMError as exc:
                        raise SystemExit(f"{exc}\n(Oder mit --no-ai ohne KI-Einordnung.)")
                for r in detect_text_changes(session, llm, EdgarClient(settings.sec_user_agent),
                                             only_ticker=args.ticker, max_calls=args.max_calls):
                    extra = f" ({r.n_added} neue/geaenderte, {r.n_removed} entfallene Absaetze)" if r.n_added or r.n_removed else ""
                    print(f"Risikotext {r.ticker:6} {r.status}{extra}")
            print_alerts(session, only_unseen=True, min_severity=1, mark_seen=False)
        elif args.cmd == "alerts":
            print_alerts(session, only_unseen=not args.all, min_severity=args.min_severity, mark_seen=args.mark_seen)
        elif args.cmd == "usage":
            print_usage(session, settings)
        elif args.cmd == "check-claude":
            results = diagnose(args.model)
            for ok, line in results:
                print(("OK  " if ok else "FEHLER  ") + line)
            if not all(ok for ok, _ in results):
                raise SystemExit(1)
            print("\nclaude -p funktioniert.")
        elif args.cmd == "dashboard":
            import subprocess
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
    try:
        main()
    except BrokenPipeError:  # z. B. Ausgabe an 'head' / 'Select-Object -First' abgeschnitten
        pass
