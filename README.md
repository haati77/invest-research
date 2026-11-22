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
python -m invest_research compare     # Peer-Vergleich im Terminal
pytest                                # Tests
```

Die Watchlist steht in `config/watchlist.yaml`. Weitere Befehle: `init`, `ingest`, `compute`, `prices`, `status`.
