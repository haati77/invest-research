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
python -m invest_research dashboard   # Dashboard im Browser öffnen
python -m invest_research ask "Welche Titel haben sinkende Margen und steigende Schulden?"   # Fragen an die Daten
python -m invest_research usage       # KI-Nutzung und Limits
python -m invest_research watch       # Kennzahlensprung, Kursereignis, Risikotext-Vergleich -> Alerts
python -m invest_research alerts      # Alerts im Terminal (--mark-seen: als gelesen markieren)
python -m invest_research compare     # Peer-Vergleich im Terminal
python -m invest_research analyze     # KI-Analyse neuester Berichte (braucht angemeldetes Claude Code)
python -m invest_research show MSFT   # Analyse im Terminal lesen
python -m invest_research profile     # Unternehmensprofile erstellen (--no-ai ohne KI)
python -m invest_research show-profile MSFT   # Profil als Markdown
pytest                                # Tests
```

Die Watchlist steht in `config/watchlist.yaml`. Die KI-Analyse nutzt `claude -p` (Abo) und ist auf 12 Aufrufe je Lauf begrenzt; erneut ausführen macht weiter. Weitere Befehle: `init`, `ingest`, `compute`, `prices`, `status`.

## Täglich automatisch (Windows)

`scripts\daily_run.bat` führt `update` und `watch` aus und schreibt nach `data\daily.log`.
Einmal in der Eingabeaufforderung einrichten (Pfad anpassen; der Rechner muss zur Uhrzeit laufen):

```powershell
schtasks /Create /SC DAILY /ST 18:30 /TN "InvestWatch" /TR "C:\Data\dev\invest-research\scripts\daily_run.bat"
```

Prüfen: `schtasks /Query /TN InvestWatch`, entfernen: `schtasks /Delete /TN InvestWatch`.
Der Textvergleich per KI braucht ein angemeldetes Claude Code; ohne KI laufen die Zahlen-Alerts trotzdem.

## Wenn KI-Funktionen fehlschlagen

```powershell
python -m invest_research check-claude
```

Der Befehl zeigt, welches `claude` gefunden wird, dessen Version und das Ergebnis eines Testaufrufs mit der
echten Fehlermeldung. Typische Ursachen: nicht angemeldet (im Terminal `claude` starten und `/login`),
Nutzungslimit erreicht oder `claude` nicht im PATH. Zahlen, Kennzahlen, Kurse und Zahlen-Alerts funktionieren
ohne KI; `watch --no-ai` und `profile --no-ai` laufen ohne `claude`.

## Fragen an die Daten

Im Dashboard (Seite „Fragen“) oder im Terminal mit `ask` kannst du Fragen in normaler Sprache stellen, etwa
„Welche drei Titel sind am billigsten gemessen am KGV?“ oder „Welche Risiken nennt Microsoft?“.
Die KI übersetzt die Frage nur in einen Abfrageplan; ausgeführt wird er vom Programm auf der lokalen Datenbank.
Unter „Verstanden als“ siehst du, wie die Frage gelesen wurde. Prognosen, Kaufempfehlungen und Firmen außerhalb
der Watchlist werden abgelehnt.

## Limits, Kosten und Protokoll

- `python -m invest_research usage` zeigt, wie viele KI-Aufrufe heute und in diesem Monat gelaufen sind.
- In `.env` begrenzt `LLM_MAX_CALLS_PER_DAY` (Standard 80) die KI-Aufrufe pro Tag; optional
  `LLM_MAX_COST_USD_PER_MONTH`. Bei Erreichen stoppt nur die KI-Arbeit mit einer klaren Meldung.
- Alle Läufe schreiben in `data\invest_research.log` (rotierend). Mit `-v` erscheinen Infomeldungen auch auf der
  Konsole. Das Dashboard zeigt Datenstand, KI-Nutzung und die letzten Protokollzeilen auf der Seite „System“.
