@echo off
REM Taeglicher Lauf: Daten aktualisieren, Veraenderungen erkennen. Ausgabe in data\daily.log
REM Einrichtung in der Windows-Aufgabenplanung: siehe README (Abschnitt "Taeglich automatisch").
cd /d "%~dp0.."
if not exist data mkdir data
call .venv\Scripts\activate.bat
echo === %date% %time% === >> data\daily.log
python -m invest_research update >> data\daily.log 2>&1
python -m invest_research watch >> data\daily.log 2>&1
