# Investment-Forschungsroboter

Analysiert US-Unternehmen (SEC EDGAR), vergleicht Kennzahlen und zeigt alles in einem lokalen Dashboard.
Recherche-Werkzeug, **keine Anlageberatung**.

## Einrichtung (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env   # SEC_USER_AGENT eintragen: "Name email@example.com"
```

## Nutzung

```powershell
python -m invest_research update      # Filings + Kennzahlen + Kurse laden und berechnen
python -m invest_research dashboard   # Dashboard im Browser oeffnen
python -m invest_research watch       # Kennzahlensprung, Kursereignis, Risikotext-Vergleich -> Alerts
python -m invest_research alerts      # Alerts im Terminal (--mark-seen: als gelesen markieren)
python -m invest_research compare     # Peer-Vergleich im Terminal
python -m invest_research analyze     # KI-Analyse neuester Berichte (braucht angemeldetes Claude Code)
python -m invest_research show MSFT   # Analyse im Terminal lesen
python -m invest_research profile     # Unternehmensprofile erstellen (--no-ai ohne KI)
python -m invest_research show-profile MSFT   # Profil als Markdown
pytest                                # Tests
```

Die Watchlist steht in `config/watchlist.yaml`. Die KI-Analyse nutzt `claude -p` (Abo) und ist auf 12 Aufrufe je Lauf begrenzt; erneut ausfuehren macht weiter. Weitere Befehle: `init`, `ingest`, `compute`, `prices`, `status`.

## Taeglich automatisch (Windows)

`scripts\daily_run.bat` fuehrt `update` und `watch` aus und schreibt nach `data\daily.log`.
Einmal in der Eingabeaufforderung einrichten (Pfad anpassen; der Rechner muss zur Uhrzeit laufen):

```powershell
schtasks /Create /SC DAILY /ST 18:30 /TN "InvestWatch" /TR "C:\Data\dev\invest-research\scripts\daily_run.bat"
```

Pruefen: `schtasks /Query /TN InvestWatch`, entfernen: `schtasks /Delete /TN InvestWatch`.
Der Textvergleich per KI braucht ein angemeldetes Claude Code; ohne KI laufen die Zahlen-Alerts trotzdem.
