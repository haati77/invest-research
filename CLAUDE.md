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
2. EDGAR-Anbindung (Ticker->CIK, Filings, XBRL companyfacts)
3. Kennzahlen-Engine + Peer-Vergleich (ohne KI)
4. Erstes Dashboard (Streamlit)
5. KI-Berichtsanalyse
6. Unternehmensprofile
7. Veraenderungserkennung + Alerts
8. Politur (Limits, Logging, Fragen an die Daten)
