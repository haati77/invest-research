"""Filing-HTML -> Text -> Abschnitte, plus wörtliche Zitatprüfung.

Reale 10-K/10-Q-Dokumente (Inline-XBRL) enthalten ein Inhaltsverzeichnis am Anfang und
auf jeder Seite eine Kopfzeile wie 'PART I / Item 1A'. Als echte Überschrift gilt daher nur
'Item <Nr>. <Titel>' mit Titel in derselben Zeile; bei mehreren Treffern je Item gewinnt der
längste Abschnitt (das Inhaltsverzeichnis ergibt nur Mini-Abschnitte).
"""
from __future__ import annotations

import re
import unicodedata
import warnings

from bs4 import BeautifulSoup, NavigableString, XMLParsedAsHTMLWarning

# Gewünschte Abschnitte je Formular: Schlüssel -> Item-Nummer
WANTED = {
    "10-K": {"business": "1", "risks": "1a", "mdna": "7"},
    "10-Q": {"mdna": "2"},
}
MIN_SECTION_CHARS = 2000
BLOCK_TAGS = ["p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"]

# Nummer und Titel können in getrennten Tabellenzellen stehen ('ITEM 2. | MANAGEMENT...'),
# optional mit vorangestelltem 'PART I |'.
HEAD_RE = re.compile(
    r"(?im)^[ \t]*(?:part[ \t]+[iv]+[ \t]*\|[ \t]*)?item[ \t]+(\d{1,2}[abc]?)[ \t]*[\.\:\-–—]"
    r"[ \t]*(?:\|[ \t]*)?([A-Za-z][^\n]{2,150})$")

# Manche Berichte (z. B. PepsiCo) setzen die Finanzberichte VOR die 'Item 8'-Überschrift.
# Die MD&A endet dann an der ersten dieser Zeilen (nach einer Mindestlänge).
MDNA_END_RE = re.compile(
    r"(?im)^(report of management|management.s report on internal control|"
    r"report of independent registered|consolidated (statements?|balance sheets?) of)\b")
MDNA_MIN_BEFORE_END = 5000


def html_to_text(html: str) -> str:
    """Sichtbarer Text mit Zeilenumbrüchen an Blockgrenzen; Inline-XBRL-Tags zerreißen Sätze nicht."""
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "ix:header"]):
        tag.decompose()
    for cell in soup.find_all(["td", "th"]):
        cell.append(NavigableString(" | "))
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for tag in soup.find_all(BLOCK_TAGS):
        tag.append(NavigableString("\n"))
    text = soup.get_text("")
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\xa0​]+", " ", raw).strip()
        line = re.sub(r"(\s*\|\s*)+$", "", line).strip()
        line = re.sub(r"^(\|\s*)+", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


_PAGE_NOISE = re.compile(r"^(\d{1,3}|PART [IV]+|Item \d{1,2}[A-C]?(, ?\d{1,2}[A-C]?)*)$", re.I)


def _strip_trailing_noise(body: str) -> str:
    lines = body.split("\n")
    while lines and _PAGE_NOISE.match(lines[-1].strip()):
        lines.pop()  # Seitenzahl und Kopfzeile der Folgeseite am Abschnittsende
    return "\n".join(lines).strip()


def split_sections(text: str, form_type: str) -> dict[str, str]:
    """Liefert {schlüssel: text} für die gewünschten Abschnitte; nicht Gefundenes fehlt einfach."""
    wanted = WANTED.get(form_type.split("/")[0], {})
    heads = [(m.start(), m.end(), m.group(1).lower()) for m in HEAD_RE.finditer(text)]
    best: dict[str, str] = {}
    for i, (_, end, num) in enumerate(heads):
        nxt = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        body = _strip_trailing_noise(text[end:nxt].strip())
        if len(body) > len(best.get(num, "")):
            best[num] = body
    out = {key: best[num] for key, num in wanted.items() if len(best.get(num, "")) >= MIN_SECTION_CHARS}
    if "mdna" in out:
        out["mdna"] = _trim_mdna(out["mdna"])
    return out


def _trim_mdna(text: str) -> str:
    for m in MDNA_END_RE.finditer(text):
        if m.start() >= MDNA_MIN_BEFORE_END:
            return text[:m.start()].strip()
    return text


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return re.sub(r"[^0-9a-z]+", "", s)  # Anführungszeichen, Bindestriche, Leerraum egal


def quote_in_text(quote: str, text: str, *, min_chars: int = 25) -> bool:
    """True, wenn das Zitat (bis auf Formatierung) zusammenhängend im Originaltext steht."""
    q = _norm(quote)
    return len(q) >= min_chars and q in _norm(text)
