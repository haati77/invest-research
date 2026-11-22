"""Textvergleich zweier Berichtsabschnitte (z. B. Risikofaktoren dieses Jahr gegen letztes Jahr).

Rein deterministisch, ohne KI. Ein Absatz gilt als neu/geaendert, wenn weniger als die Haelfte
seiner Wortfolgen (5-Gramme) irgendwo im alten Abschnitt vorkommen. Das ist robust gegen
umgestellte oder zusammengelegte Absaetze und reagiert auf echte inhaltliche Aenderungen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

WORD_RE = re.compile(r"[a-z0-9]+")
SHINGLE = 5
MIN_PARAGRAPH_CHARS = 120  # Ueberschriften und Fragmente zaehlen nicht als Absatz


def _words(text: str) -> list[str]:
    return WORD_RE.findall(text.lower())


def shingles(text: str) -> set[tuple[str, ...]]:
    w = _words(text)
    return {tuple(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}


def paragraphs(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if len(line.strip()) >= MIN_PARAGRAPH_CHARS]


@dataclass
class DiffResult:
    added: list[str]      # neu oder wesentlich umformuliert
    removed: list[str]    # im alten Text, im neuen nicht mehr (sinngemaess)
    n_old: int
    n_new: int

    @property
    def added_chars(self) -> int:
        return sum(len(p) for p in self.added)


def _unmatched(paras: list[str], other_text: str, threshold: float) -> list[str]:
    reference = shingles(other_text)
    out = []
    for p in paras:
        s = shingles(p)
        if not s:
            continue
        if len(s & reference) / len(s) < threshold:
            out.append(p)
    return out


def diff_sections(old_text: str, new_text: str, *, threshold: float = 0.5) -> DiffResult:
    old_p, new_p = paragraphs(old_text), paragraphs(new_text)
    return DiffResult(added=_unmatched(new_p, old_text, threshold),
                      removed=_unmatched(old_p, new_text, threshold), n_old=len(old_p), n_new=len(new_p))
