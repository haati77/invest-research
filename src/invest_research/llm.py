"""LLM-Schnittstelle. Standard: `claude -p` (headless, ueber das Claude-Code-Abo).

Sicherheit: Berichtstexte sind nicht vertrauenswuerdige Eingaben (Prompt Injection moeglich).
Der Aufruf schaltet daher ALLE Tools ab, laeuft in einem leeren Temp-Verzeichnis und speichert
keine Session. Selbst eine eingeschleuste Anweisung kann nichts ausfuehren.

Wechsel auf die API: eine Klasse mit `complete(system, prompt) -> LLMResponse` genuegt.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Protocol


class LLMError(RuntimeError):
    pass


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
                          timeout=timeout, cwd=cwd)


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

    def command(self, system: str) -> list[str]:
        return [self._exe, "-p", "--output-format", "json", "--model", self.model,
                "--system-prompt", system, "--tools", "", "--disable-slash-commands",
                "--no-session-persistence"]

    def complete(self, system: str, prompt: str) -> LLMResponse:
        with tempfile.TemporaryDirectory() as tmp:  # kein Projektkontext (CLAUDE.md etc.)
            try:
                proc = self._runner(self.command(system), prompt, self._timeout, tmp)
            except subprocess.TimeoutExpired as exc:
                raise LLMError(f"claude -p Zeitueberschreitung nach {self._timeout}s") from exc
        if proc.returncode != 0:
            raise LLMError(f"claude -p Fehlercode {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude -p lieferte kein JSON: {proc.stdout[:300]!r}") from exc
        if data.get("is_error"):
            raise LLMError(f"claude -p meldet Fehler: {str(data.get('result'))[:500]}")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=data.get("result") or "", model=self.model,
            input_tokens=(usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0),
            output_tokens=usage.get("output_tokens"), cost_usd=data.get("total_cost_usd"))
