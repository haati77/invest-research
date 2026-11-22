"""LLM-Schnittstelle. Standard: `claude -p` (headless, ueber das Claude-Code-Abo).

Sicherheit: Berichtstexte sind nicht vertrauenswuerdige Eingaben (Prompt Injection moeglich).
Der Aufruf schaltet daher ALLE Tools ab, laeuft in einem leeren Temp-Verzeichnis und speichert
keine Session. Selbst eine eingeschleuste Anweisung kann nichts ausfuehren.

Wechsel auf die API: eine Klasse mit `complete(system, prompt) -> LLMResponse` genuegt.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Protocol


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """Der Aufruf selbst ist gescheitert (Programm, Anmeldung, Zeitlimit, Limit erreicht), nicht die
    Antwort des Modells. Wiederholte Faelle beenden den KI-Teil eines Laufs frueh (FailureGuard)."""


class LLMBudgetExceeded(LLMUnavailable):
    """Selbstgesetztes Limit (Aufrufe/Tag oder Kosten/Monat) erreicht: KI-Arbeit endet sofort."""


class FailureGuard:
    """Zaehlt aufeinanderfolgende LLMUnavailable-Fehler; ab `limit` gilt die KI als nicht verfuegbar."""

    def __init__(self, limit: int = 2):
        self.limit, self.fails, self.last = limit, 0, ""

    def ok(self) -> None:
        self.fails = 0

    def failed(self, exc: Exception) -> None:
        if isinstance(exc, LLMBudgetExceeded):
            self.fails, self.last = self.limit, str(exc)  # Limit gilt fuer alle weiteren Aufrufe: sofort stoppen
        elif isinstance(exc, LLMUnavailable):
            self.fails += 1
            self.last = str(exc)
        else:
            self.fails = 0  # ungueltige Antworten sind kein Ausfall

    @property
    def tripped(self) -> bool:
        return self.fails >= self.limit

    def message(self) -> str:
        return f"KI nicht verfuegbar, restliche Aufrufe uebersprungen. Letzter Fehler: {self.last}"


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, prompt: str) -> LLMResponse: ...


Runner = Callable[[list[str], str, int, str], "subprocess.CompletedProcess[str]"]


def _run(cmd: list[str], stdin: str, timeout: int, cwd: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=cwd)


def describe_failure(proc: "subprocess.CompletedProcess[str]", limit: int = 400) -> str:
    """Verstaendlicher Fehlertext: bevorzugt das 'result'-Feld der JSON-Antwort, sonst stderr/stdout."""
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            parts = [str(data.get(k)) for k in ("result", "api_error_status", "subtype")
                     if data.get(k) and not (k == "subtype" and data.get(k) == "success")]  # 'success' verwirrt
            if parts:
                return " | ".join(parts)[:limit]
    except json.JSONDecodeError:
        pass
    return ((proc.stderr or "").strip() or out)[:limit] or "(keine Ausgabe)"


class ClaudeCLI:
    def __init__(self, model: str = "sonnet", *, timeout: int = 300, executable: str | None = None,
                 runner: Runner = _run):
        self.model = model
        self._timeout = timeout
        self._exe = executable or shutil.which("claude")
        self._runner = runner
        if not self._exe:
            raise LLMError("Das 'claude'-Programm wurde nicht gefunden. Claude Code installieren und "
                           "anmelden (im Terminal 'claude' ausfuehren).")

    def command(self, system_file: str) -> list[str]:
        # Der Systemprompt steht in einer Datei: mehrzeilige Argumente mit | " < > werden von der
        # Windows-Eingabeaufforderung (claude.cmd) zerlegt. '--tools ""' schaltet ALLE Tools ab;
        # '--tools=' wirkt NICHT (getestet) - das leere Argument muss separat stehen.
        return [self._exe, "-p", "--output-format", "json", "--model", self.model,
                "--system-prompt-file", system_file, "--tools", "", "--disable-slash-commands",
                "--no-session-persistence"]

    def complete(self, system: str, prompt: str) -> LLMResponse:
        with tempfile.TemporaryDirectory() as sysdir, tempfile.TemporaryDirectory() as tmp:
            sys_file = os.path.join(sysdir, "system.txt")
            with open(sys_file, "w", encoding="utf-8") as fh:
                fh.write(system)
            try:  # cwd = leeres Temp-Verzeichnis: kein Projektkontext (CLAUDE.md etc.)
                proc = self._runner(self.command(sys_file), prompt, self._timeout, tmp)
            except subprocess.TimeoutExpired as exc:
                raise LLMUnavailable(f"claude -p Zeitueberschreitung nach {self._timeout}s") from exc
            except OSError as exc:
                raise LLMUnavailable(f"claude -p konnte nicht gestartet werden ({self._exe}): {exc}") from exc
        if proc.returncode != 0:
            raise LLMUnavailable(f"claude -p Fehlercode {proc.returncode}: {describe_failure(proc)}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"claude -p lieferte kein JSON: {proc.stdout[:300]!r}") from exc
        if data.get("is_error"):
            raise LLMUnavailable(f"claude -p meldet Fehler: {describe_failure(proc)}")
        if data.get("permission_denials"):  # Tools sind aus; ein Versuch deutet auf Prompt Injection hin
            raise LLMError(f"Das Modell wollte Tools nutzen (blockiert): {str(data['permission_denials'])[:200]}")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=data.get("result") or "", model=self.model,
            input_tokens=(usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0),
            output_tokens=usage.get("output_tokens"), cost_usd=data.get("total_cost_usd"))


def diagnose(model: str = "haiku") -> list[tuple[bool, str]]:
    """Prueft die claude-Anbindung Schritt fuer Schritt. Gibt (ok, Text)-Zeilen zurueck."""
    exe = shutil.which("claude")
    if not exe:
        return [(False, "'claude' nicht im PATH gefunden. Claude Code installieren; danach neues Terminal oeffnen.")]
    lines = [(True, f"Programm: {exe}")]
    if exe.lower().endswith((".cmd", ".bat")):
        lines.append((True, "Hinweis: 'claude' laeuft ueber einen .cmd-Wrapper (npm-Installation)."))
    try:
        ver = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        lines.append((ver.returncode == 0, f"Version: {(ver.stdout or ver.stderr).strip()[:100]}"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return lines + [(False, f"'claude --version' fehlgeschlagen: {exc}")]
    try:
        resp = ClaudeCLI(model, timeout=120, executable=exe).complete(
            "You are a connectivity check. Reply with exactly the word OK.", "ping")
        ok = "ok" in resp.text.lower()
        lines.append((ok, f"Testaufruf (Modell {model}): Antwort {resp.text.strip()[:60]!r}, "
                          f"{resp.input_tokens or 0}+{resp.output_tokens or 0} Token"))
    except LLMError as exc:
        lines.append((False, f"Testaufruf fehlgeschlagen: {exc}"))
        if any(w in str(exc).lower() for w in ("authenticate", "oauth", "not logged in", "login", "401")):
            lines.append((False, "Die Anmeldung ist abgelaufen oder fehlt: im Terminal 'claude' starten, dort "
                                 "'/login' eingeben, dann 'check-claude' erneut ausfuehren."))
        else:
            lines.append((False, "Moegliche Ursachen: nicht angemeldet (im Terminal 'claude' starten und /login), "
                                 "Nutzungslimit erreicht, oder Modellname unbekannt (--model)."))
    return lines
