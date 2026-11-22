# Investment-Forschungsroboter

Analysiert US-Unternehmen (SEC EDGAR), vergleicht Kennzahlen, erstellt KI-Profile, ueberwacht eine Watchlist. Dashboard: Streamlit. Lokal, SQLite, nur kostenlose Datenquellen.

## Prinzipien
- Zahlen kommen aus Code/XBRL, die KI interpretiert nur. Das LLM rechnet nie.
- Jede KI-Aussage braucht ein Zitat (Filing + Abschnitt); Zitate werden gegen den Originaltext geprueft.
- KI-Ausgaben sind strukturiert (Pydantic-Schemas), kein Freitext.
- KI-Aufrufe laufen ueber `claude -p` (headless, Abo) hinter einer eigenen Schnittstelle, damit spaeter auf die API gewechselt werden kann.
- Datenquellen hinter Schnittstellen kapseln (z. B. Kurse via yfinance, austauschbar).
- Kein Anlageberatungs-Charakter; Disclaimer in jedem Report.

## Konventionen
- Python >= 3.11, Code unter `src/invest_research/`, Tests unter `tests/` (`pytest`).
- Secrets nur in `.env` (nie committen). SEC verlangt `SEC_USER_AGENT`.
- Watchlist: `config/watchlist.yaml`.

## Fahrplan
1. Fundament (Struktur, Config, Datenmodell, Tests) -- erledigt
2. EDGAR-Anbindung (Ticker->CIK, Filings, XBRL companyfacts) -- erledigt
3. Kennzahlen-Engine + Peer-Vergleich (ohne KI) -- erledigt
4. Erstes Dashboard (Streamlit) -- erledigt
5. KI-Berichtsanalyse -- erledigt
6. Unternehmensprofile
7. Veraenderungserkennung + Alerts
8. Politur (Limits, Logging, Fragen an die Daten)

## Datenstand (Schritt 2)
- CLI: `python -m invest_research init|ingest [--ticker X] [--years N]|status`
- `edgar.py` (Client, max. ~7 Req/s, Retry), `facts.py` (XBRL-Konzepte mit Tag-Prioritaet), `ingest.py` (idempotent; Alerts `new_filing` erst ab dem zweiten Lauf).
- Periodentypen: `FY`, `Q` (~90 Tage), `H1`/`9M` (kumuliert aus 10-Qs, nur Zwischenwerte), `instant` (Bilanz). Q4 wird nie einzeln gemeldet und in der Engine abgeleitet.
- www.sec.gov blockt teils Cloud-IPs/generische User-Agents; deshalb sind CIKs in `watchlist.yaml` gepinnt.

## Bekannte Datenluecken (in Schritt 3 beruecksichtigen)
- `total_liabilities`: AMD, KO, ORCL taggen es nicht -> ableiten als total_assets - equity.
- `gross_profit`: ORCL, PG melden keinen Bruttogewinn -> Kennzahl dort `None`, nicht erfinden.
- `capex`: PEP nutzt einen Sondertag; `long_term_debt` bei mehreren Firmen luckenhaft.

## Kennzahlen-Engine (Schritt 3)
- CLI: `python -m invest_research compute` (idempotent), `compare [--basis FY|TTM] [--sector X]`.
- `engine.py`: reine Funktionen (Quartale aus kumulierten Werten: Q2=H1-Q1, Q3=9M-H1; Q4 = FY - 3 Quartale; TTM; Quoten; Wachstum). Cashflow wird in 10-Qs nur kumuliert gemeldet, daher dieser Umweg. Fehlende Eingaben -> keine Zeile, nie geschaetzt.
  Guards: Wachstum nur bei positiver Basis, ROE/Verschuldung nur bei positivem Eigenkapital.
- `compute.py` schreibt `source='computed'`; XBRL-Rohwerte (`source='xbrl'`) werden nie ueberschrieben.
- `peers.py`: Snapshot je Firma (letztes FY bzw. TTM), Perzentil in der Branche (`pct_*`) und gesamt (`pctall_*`).
  Der Score ist nur Orientierung: Firmen mit Datenluecken werden aus weniger Quoten gemittelt (z. B. PEP ohne FCF).
- Nicht vergleichbar ohne Vorsicht: Geschaeftsjahre enden unterschiedlich (Spalte Stichtag beachten).

## Kurse, Bewertung, Dashboard (Schritt 4)
- CLI: `prices`, `update` (ingest + compute + prices), `dashboard`. README enthaelt die Windows-Befehle.
- `prices.py`: Provider-Schnittstelle, Standard = Yahoo-Chart-API (inoffiziell; Ausfall moeglich, daher austauschbar). Inkrementell, idempotent.
- `valuation.py`: KGV, KUV, Kurs/FCF aus Kurs und TTM-Werten; Basis <= 0 -> None. EV bewusst weggelassen (Nettoverschuldung unvollstaendig: nur langfristige Schulden).
- `queries.py`: lesende Abfragen fuers Dashboard (kein Streamlit-Code). `dashboard/app.py`: Seiten Uebersicht, Peer-Vergleich, Unternehmen, Alerts.
- Diagramme: Farben aus der validierten dataviz-Palette (Slots 1-3), Hell/Dunkel ueber `st.context.theme`. Hell-Modus Aqua hat Kontrast-WARN -> Legende, Direktbeschriftung und Tabellenansicht sind Pflicht.
- Aktienanzahl stammt vom 10-Q/10-K-Deckblatt (dei). Bei Firmen mit mehreren Aktienklassen waere die Marktkapitalisierung unvollstaendig (nicht auf der Start-Watchlist).
- Dashboard-Test: `tests/test_prices_valuation_dashboard.py` (Streamlit AppTest); visuell mit Playwright geprueft (hell/dunkel).

## KI-Berichtsanalyse (Schritt 5)
- CLI: `analyze [--ticker T] [--max-calls N] [--model haiku|sonnet] [--force]`, `show TICKER`. Dashboard: Unternehmen -> "KI-Analyse der Berichte".
- Pipeline: `filing_text.py` (HTML -> Text -> Abschnitte business/risks/mdna bei 10-K, mdna bei 10-Q), `llm.py` (`claude -p`), `analysis.py` (Prompts, Schemas, Zitatpruefung, Orchestrierung). Tabellen `filing_sections`, `analyses`.
- Abschnittserkennung: nur Ueberschriften `Item N. Titel` (Titel in derselben Zeile bzw. Tabellenzelle `ITEM 2. | TITEL`), bei Mehrfachtreffern gewinnt der laengste Abschnitt (Inhaltsverzeichnis/Seitenkopfzeilen werden so ignoriert). MD&A endet vor `Report of Management`/`Consolidated Statements of ...` (PepsiCo setzt Finanzberichte vor Item 8). An den neuesten 10-K/10-Q aller 10 Watchlist-Firmen geprueft; bei neuen Firmen Abschnittsgroessen pruefen.
- SICHERHEIT: Berichtstext ist nicht vertrauenswuerdig (Prompt Injection). `claude -p` laeuft immer mit `--tools ""`, leerem Temp-Verzeichnis, ohne Session-Speicherung; Prompt per stdin. Nie Tools aktivieren.
- Qualitaetssicherung: jedes Zitat wird woertlich (formatierungstolerant) gegen den Text geprueft, den das Modell sah; nur verifizierte Aussagen erscheinen im Dashboard. GRENZE: Das Zitat belegt nicht automatisch jedes Detail der Aussage (Prompt v2 verlangt das, ist aber nicht maschinell pruefbar), und die Zusammenfassung ist nie zitatgeprueft.
- Kosten/Limits: Standardmodell haiku (guter Kompromiss: ~0,005 USD Listenpreis je Abschnitt, ~15-20 s). `--max-calls` (Standard 12) begrenzt jeden Lauf, Fortschritt ist gespeichert, Wiederholung macht weiter. Ergebnisse je (Filing, Abschnitt, PROMPT_VERSION) einmal; bei Prompt-Aenderung PROMPT_VERSION erhoehen.
- Nur das neueste 10-K und neueste 10-Q je Firma werden analysiert (Schritt 7 vergleicht spaeter mit frueheren Berichten).
- Unter Windows: `claude` muss im PATH und angemeldet sein. Falls `.cmd`-Wrapper Probleme mit leerem `--tools ""` machen, in `ClaudeCLI.command` anpassen (nicht auf Linux getestet).
