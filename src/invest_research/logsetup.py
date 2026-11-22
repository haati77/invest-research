"""Protokollierung: ausführliche Logdatei (rotierend) plus knappe Konsolenausgabe."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

NOISY = ("httpx", "httpcore", "urllib3", "watchdog", "PIL", "matplotlib")


def setup_logging(log_path: Path, *, verbose: bool = False) -> None:
    """Datei: ab INFO mit Zeitstempel (1 MB x 3 Dateien). Konsole: nur Warnungen, mit -v auch INFO."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        if getattr(h, "_invest_research", False):
            root.removeHandler(h)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_h = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    for h in (file_h, console):
        h._invest_research = True  # type: ignore[attr-defined]
        root.addHandler(h)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


class Tee:
    """Schreibt jede ausgegebene Zeile zusätzlich als INFO ins Protokoll (für Befehle, die Daten ändern)."""

    def __init__(self, stream, logger: logging.Logger):
        self._stream, self._log, self._buf = stream, logger, ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log.info(line.strip())
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def tail(log_path: Path, lines: int = 80) -> str:
    """Letzte Zeilen der Logdatei (leer, wenn es noch keine gibt)."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""
