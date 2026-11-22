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
6. Unternehmensprofile -- erledigt
7. Veraenderungserkennung + Alerts -- erledigt
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
- Nur das neueste 10-K und neueste 10-Q je Firma werden analysiert; der Vergleich mit dem Vorjahres-10-K (Risikofaktoren) steckt in Schritt 7.
- Unter Windows: `claude` muss im PATH und angemeldet sein. Bei KI-Fehlern ZUERST `python -m invest_research check-claude` (zeigt Pfad, Version, Testaufruf und die echte Fehlermeldung).
- Windows-Bericht (Schritt 7): alle `claude -p`-Aufrufe scheiterten mit Exit-Code 1. Wahrscheinliche Ursache (nicht bestaetigt, hier nicht reproduzierbar): der mehrzeilige Systemprompt mit `| " < >` ging als Argument durch `claude.cmd`/cmd.exe. Fix: Systemprompt per `--system-prompt-file`; Argumente enthalten nie Zeilenumbrueche oder Shell-Sonderzeichen (Test). `--tools ""` muss als SEPARATES leeres Argument stehen: `--tools=` schaltet Tools NICHT ab (getestet).
- Robustheit: `LLMUnavailable` (Start/Anmeldung/Limit/Zeitlimit) unterscheidet Ausfall von schlechter Antwort. `FailureGuard`: nach 2 Ausfaellen in Folge endet der KI-Teil von `analyze`, `watch` (Rest nur mit Absatzstatistik, spaeter mit KI aufwertbar) und `profile` (Rest uebersprungen). Fehlertexte zeigen das `result`-Feld der JSON-Antwort statt des ganzen JSON. `permission_denials` in der Antwort (Tool-Versuch, Injection-Verdacht) fuehrt zu einem Fehler.

## Unternehmensprofile (Schritt 6)
- CLI: `profile [--ticker T] [--force] [--no-ai] [--model haiku]`, `show-profile TICKER` (Markdown). Dashboard: Seite "Profil" (Versionsauswahl, Belege, Markdown-Download).
- `profiles.py`: Profil = Fakten (Code: Kennzahlen FY/TTM, Bewertung, Branchen-Score) + verifizierte Aussagen aus Schritt 5 + optionale KI-Synthese (Staerken/Schwaechen/Beobachten/offene Fragen) + ehrliche Datenluecken. Tabelle `profiles` (versioniert, `input_hash`, `model`).
- Beleg-Pflicht: Die KI bekommt ein Beleg-Blatt (`F:`=Kennzahl, `V:`=Bewertung, `P:`=Peer, `A:`=verifizierte Berichtsaussage) mit vom Code formatierten Zahlen. Jede Aussage muss Beleg-IDs nennen; unbekannte IDs oder Zahlen, die nicht in den zitierten Belegen stehen (kleine Ganzzahlen <= 10 frei), fuehren zum Verwerfen. Verworfenes wird mit Grund in `synthesis.dropped_points` gespeichert und im Dashboard gezeigt.
- GRENZE: Zahlen und Belege werden geprueft, Kausalaussagen ("getrieben von ...") und Wertungen ("hoch") nicht. Offene Fragen sind Fragen, keine Fakten.
- Neue Version nur bei geaenderten Eingaben (Hash ueber Kennzahlen FY/TTM und Analysen; Kurs/Bewertung zaehlen nicht) oder `--force`. Ein Profil ohne KI und eines mit KI sind verschiedene Versionen. Ohne Berichtsanalyse wird nie das LLM aufgerufen.
- Migration: `db.init_db` ergaenzt fehlende nullable Spalten in bestehenden Datenbanken (`_add_missing_columns`). Neue Modellspalten muessen nullable sein oder einen server_default haben.
- TODO Schritt 8: Oberflaeche nutzt Ersatzschreibungen (ae/oe/ue) statt Umlaute; auf echte Umlaute umstellen (Tests nutzen die Seitennamen als Schluessel).

## Veraenderungserkennung und Alerts (Schritt 7)
- CLI: `watch [--ticker T] [--no-text] [--no-ai] [--max-calls 6]`, `alerts [--all] [--min-severity N] [--mark-seen]`. `update` erzeugt zusaetzlich die Zahlen-Alerts. Dashboard: Seite "Alerts" (Filter, nach Wichtigkeit sortiert, gelesen markieren); Profil-Seite zeigt Aenderungen gegenueber der Vorversion (`profiles.diff_profiles`).
- `changes.py`: alle Schwellen an einer Stelle (`METRIC_RULES`, `PRICE_MOVE_*`, `STALE_DAYS`, `TEXT_MIN_ADDED_CHARS`). Ereignisarten: `new_filing` (aus ingest), `metric_change`, `price_move`, `text_change`. Jedes Event hat `dedupe_key` -> Wiederholung meldet nichts doppelt.
- Kennzahlensprung: TTM gegen TTM ein Jahr frueher. Verschlechterungen behalten die volle Wichtigkeit, Verbesserungen werden um eine Stufe herabgestuft (sonst 22 von 37 Meldungen "hoch"); Vorzeichenwechsel (ins Negative) immer hoch. Titel enthalten die Richtung ("besser"/"schlechter"). Meldungen zu Stichtagen aelter als 90 Tage werden automatisch als gelesen angelegt (Erstlauf-Flut).
- Kursereignis: Tagesbewegung >= 7 % (mittel) bzw. >= 12 % (hoch) in den letzten 5 Handelstagen; Kursgewinne eine Stufe niedriger. Grund wird nicht ermittelt.
- Textvergleich Risikofaktoren (`textdiff.py` + `detect_text_changes`): neuestes 10-K gegen das davor. Deterministisch: Absatz gilt als neu/geaendert, wenn < 50 % seiner 5-Wort-Folgen im alten Abschnitt vorkommen (robust gegen Umstellungen). Mit LLM werden nur die neuen Absaetze zusammengefasst; Zitate werden gegen diesen Text geprueft. Ergebnis als `analyses`-Zeile (`section_key='risks_diff'`, `model='none'` ohne KI -> spaeter mit KI aufwertbar, Event wird aktualisiert statt doppelt angelegt). Aenderungen < 800 Zeichen erzeugen kein Event.
- Nicht abgedeckt: Textvergleich fuer MD&A/Geschaeftsbericht und 10-Q (zu viel Rauschen durch wechselnde Zahlen), Benachrichtigung per E-Mail/Telegram, Alerts auf Basis von Kursniveaus (52W-Hoch/Tief).
- Windows: `scripts/daily_run.bat` fuer die Aufgabenplanung (README); Zahlen-Alerts und Textvergleich auf Windows bestaetigt (identische Zaehlung wie Linux); KI-Aufrufe siehe Windows-Bericht in Schritt 5.
- Fund beim Testen: pandas liefert fehlende Textfelder als NaN (float) -> nie `x or ""` bei DataFrame-Zellen, sondern `isinstance(x, str)`.
