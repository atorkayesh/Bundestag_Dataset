"""Identify speakers in cleaned Bundestag Plenarprotokoll text and segment speeches.

Input: *_cleaned.txt  →  Output: *_speakers.json per session.
Patterns A/B/C/D per docs/reqs.txt (chair / member+party / official role / procedural role).
Resume-safe; existing _speakers.json files skipped unless --force.
"""

# defaults — override via CLI flags
_DEFAULT_PREPROCESSED = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"
_DEFAULT_PARTIES_CSV  = None  # auto-resolved from __file__ if not specified
# ============================================================

import argparse
import csv
import functools
import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from utils import append_log


def _load_known_parties(csv_path=None) -> frozenset:
    """Load party abbreviations from the CSV, plus known era-specific variants.

    Parameters
    ----------
    csv_path : str | Path | None
        Explicit path to the parties CSV.  When None, the file is resolved
        automatically from __file__ (works when the repo has its standard
        layout — docs/ is a sibling of src/).
    """
    if csv_path is None:
        csv_path = Path(__file__).parent.parent / "docs" / "political-parties-past-75-years.csv"
    else:
        csv_path = Path(csv_path)
    abbrevs = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                abbr = row.get("abbreviation", "").strip()
                if abbr:
                    abbrevs.add(abbr)
    # Era-specific variants not in the CSV
    abbrevs.update({
        "F.D.P.",               # WP10-12 spelling of FDP
        "CDU",                  # standalone (without /CSU)
        "CSU",                  # standalone
        "GRÜNE",                # early Green party
        "DIE GRÜNEN",           # mid-era Green party
        "GRÜNEN",
        "BÜNDNIS 90",           # standalone pre-merger (WP12, East German alliance)
        "BÜNDNIS 90/DIE GRÜNEN",# post-merger canonical form
        "BÜNDNIS 90/GRÜNE",     # transitional name (WP12)
        "PDS/Linke Liste",      # WP12 variant
        "DIE LINKE",            # canonical post-2007 name (CSV may only carry "Die Linke")
        "fraktionslos",         # lowercase used in some eras
        "Fraktionslos",
        "DKP",                  # Deutsche Kommunistische Partei (WP06–WP10)
        "NPD",                  # Nationaldemokratische Partei Deutschlands (WP05–WP06)
        "SDP",                  # keep for lookup; normalised → SPD in _PARTY_NORMALISE
        "parteilos",            # independent member (used in some eras)
    })
    return frozenset(abbrevs)


_KNOWN_PARTIES: frozenset = _load_known_parties()

_BUNDESLAENDER: frozenset = frozenset({
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
    "MecklenburgVorpommern",   # OCR: hyphen dropped
})


# Optional title prefix.  Built from two components:
#
#   _DOC_UNIT — one doctorate token, repeated zero or more times:
#     • Dr.                regular doctorate
#     • Dr. h. c.          honorary doctorate (honoris causa); OCR may capitalise → [Hh]/[Cc]
#     • Dr. h. c. mult.    multiple honorary doctorates
#     • Dr. E. h.          Ehren halber (German honorary, used without discipline suffix)
#     • Dr.-Ing. E. h.     honorary engineering doctorate (Ehren halber after discipline suffix)
#     • Dr.-Med., etc.     discipline-specific doctorate (any suffix after the hyphen)
#     • D.                 theological doctorate (Doktor der Theologie), e.g. D. Dr. Gerstenmaier
#
#   _TITLE — optional Frau/Herr, optional Prof., then zero or more _DOC_UNIT tokens.
#
# Covered combinations (non-exhaustive):
#   Dr. Adenauer · Prof. Dr. Foo · D. Dr. Gerstenmaier · D. Gerstenmaier
#   Dr. h. c. Erhard · Dr. Dr. h. c. Erhard · Dr. h. c. mult. Bar
#   Dr. E. h. Veit · Dr.-Ing. E. h. Schuberth · Dr. h. c. Dr.-Ing. E. h. Müller
#
# _DOC_UNIT handles hyphenated discipline suffixes with optional whitespace on either
# side of the hyphen (OCR variants): Dr.-Ing., Dr. -Ing., Dr.- Ing., Dr.-lng., etc.
# Honorific qualifiers: "h. c." (honoris causa) or "E. h." (Ehren halber),
# optionally followed by "mult." for multiple h. c. degrees.
_DOC_UNIT = r"(?:Dr\.(?:\s*-\s*[A-Za-z]+\.)?(?:\s+(?:[Hh]\.\s+[Cc]\.(?:\s+mult\.)?|[Ee]\.\s+[Hh]\.))?\s+|D\.\s+)"
_TITLE    = rf"(?:(?:Frau|Herr)\s+)?(?:Prof\.\s+)?(?:{_DOC_UNIT})*"

# A single name component starting with an uppercase letter.
# The body character class is extended to U+00C0–U+024F (Latin Extended-A/B)
# to cover Turkish (ğ, ı, ş), Romanian (ș, ț), Hungarian (ő, ű), etc.
# that appear in names of German MPs with migration backgrounds.
_NAME_PART = r"[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\u00C0-\u024F\-]+"

# A middle initial, e.g. the "L." in "Dr. Heinrich L. Kolb (FDP)".
# Allowed as an optional component between name parts in _FULL_NAME.
_INITIAL = r"[A-ZÄÖÜ]\."

# Optional lowercase German nobility particle (von, van, de, zu, vom, zur).
# Two-word forms (von der, van der) are listed first so they match before the
# single-word fallback.  Used both at the START of the name ("von Hassel",
# "de Maizière") and BETWEEN name parts ("Jörg van Essen",
# "Ursula von der Leyen").
_NOBLE_PARTICLE = r"(?:von\s+der|van\s+der|von|van|de|zu|vom|zur)\s+"

# Full name: title + optional noble particle + at least one name component.
# Between name parts an optional noble particle or middle initial (_INITIAL)
# is also accepted, e.g. "Jörg van Essen", "Ursula von der Leyen",
# "Heinrich L. Kolb".
# Single tokens are accepted to cover "Brandt, Bundeskanzler:" style.
_FULL_NAME = (
    rf"{_TITLE}"
    rf"(?:{_NOBLE_PARTICLE})?"
    rf"{_NAME_PART}"
    rf"(?:\s+(?:(?:{_NOBLE_PARTICLE}){_NAME_PART}|{_INITIAL}|{_NAME_PART}))*"
)

# Ministerial/official role keywords (used in Pattern C).
_OFFICIAL_ROLES = (
    r"Bundes(?:minister(?:in)?|kanzler(?:in)?)"
    r"|Parl\.\s+Staatssekretär(?:in)?"
    r"|Staatssekretär(?:in)?"
    r"|Staatsminister(?:in)?"
    r"|Ministerpräsident(?:in)?"
    r"|Schriftführer(?:in)?"           # Bundestag teller/secretary role (early sessions)
    r"|Präsident\s+des\s+Bundesrat(?:es|s)"  # Bundesrat president speaking in plenary (e.g. Arnold)
    r"|Senator(?:in)?"                 # city-state (Hamburg, Bremen, Berlin) Bundesrat delegates
    r"|Regierender\s+Bürgermeister(?:in)?"  # Berlin head of government in Bundesrat capacity
)

# Parliamentary procedural role labels (used in Pattern D).
# These appear as suffixes after party qualifiers in older sessions,
# e.g. "Dr. Kather (GB/BHE), Anfragender:"
_PARLIAMENTARY_ROLES = (
    r"Anfrage(?:nder|nderin|r|rin)"  # Anfragender / Anfragerin (questioner)
    r"|Berichterstatter(?:in)?"      # Rapporteur
    r"|Abgeordnete[rs]?"             # rare explicit label in very early sessions
)

# Main speaker-attribution regex.
# Matches a complete line that is a speaker attribution ending with ":".
#
# Pattern A — chair roles (Alterspräsident, Präsident, Vizepräsident)
# Pattern B — member name + one or more parenthesised qualifiers (location / party)
# Pattern C — name + comma + official role (minister, chancellor, state secretary …)
#
# The line must NOT start with "(" (that would be an interjection block).
# ^\s* instead of ^ to tolerate leading whitespace in older sessions (fix for WP01/WP09).
RE_SPEAKER_LINE = re.compile(
    rf"^\s*(?:"
    # Pattern A: chair / presiding officer.
    # Optional "Frau"/"Herr" may appear BEFORE the role keyword in older sessions,
    # e.g. "Frau Präsidentin Roth:" as well as the standard "Präsidentin Roth:".
    # An optional beifall annotation between name and colon is also allowed,
    # e.g. "Alterspräsident Dr. Adenauer (mit lebhaftem Beifall begrüßt) :"
    rf"(?P<chair>(?:Frau\s+|Herr\s+)?(?:Alterspräsident(?:in)?|Vizepräsident(?:in)?|Bundespräsident(?:in)?|Präsident(?:in)?)"
    rf"\s+{_FULL_NAME}(?:\s*\([^)]*\bbegrüßt\b[^)]*\))*)"
    rf"|"
    # Pattern B: member with at least one parenthesised qualifier
    rf"(?P<member>{_FULL_NAME}(?:\s+\([^)\n]+\))+)"
    rf"|"
    # Pattern C: name + comma + official role + optional description.
    # Allow at most ONE OCR line-break (with optional blank line) inside the role
    # description so that:
    #   "Niederalt, Bundesminister für Angelegenheiten\ndes Bundesrates und der Länder:"
    #   "Widmann-Mauz, Parl. Staatssekretärin\n\nbeim Bundesminister für Gesundheit:"
    # are captured.  Negative lookahead blocks TOC section headings that start with
    # "Tagesordnungspunkt N", "Anlage N", or a bare 4+-digit page number from being
    # mistaken for a role continuation.
    rf"(?P<official>{_FULL_NAME},\s*(?:{_OFFICIAL_ROLES})[^\n:]{{0,80}}"
    rf"(?:\n\n?(?!Tagesordnungspunkt\s*\d|Anlage\s*\d|\d{{4,}}\s)[^\n:]{{0,80}})?)"
    rf"|"
    # Pattern D: name + qualifier(s) + comma + parliamentary procedural role
    # e.g. "Dr. Kather (GB/BHE), Anfragender"
    rf"(?P<member_role>{_FULL_NAME}(?:\s+\([^)\n]+\))+,\s*(?:{_PARLIAMENTARY_ROLES})[^\n:]{{0,80}})"
    rf")"
    rf"\s*:\s*$",
    re.MULTILINE,
)

# Inline speaker: all three patterns at start of a line where the colon is followed
# by content on the same line (not end-of-line).
# Covers:
#   A — chair title + name          (used for WP01/WP09 full-inline format)
#   B — name + parenthesised qualifiers   (Zwischenfragen in normal sessions)
#   C — name + comma + official role
# ^\s* tolerates leading whitespace.
RE_INLINE_SPEAKER = re.compile(
    rf"^\s*(?:"
    # Pattern A inline: chair / presiding officer.
    # Optional "Frau"/"Herr" may precede the role keyword.
    # Optional beifall annotation between name and colon is also allowed.
    rf"(?P<chair_inline>(?:Frau\s+|Herr\s+)?(?:Alterspräsident(?:in)?|Vizepräsident(?:in)?|Bundespräsident(?:in)?|Präsident(?:in)?)"
    rf"\s+{_FULL_NAME}(?:\s*\([^)]*\bbegrüßt\b[^)]*\))*)"
    rf"|"
    # Pattern B inline: member name + one or more parenthesised qualifiers
    rf"(?P<member_inline>{_FULL_NAME}(?:\s+\([^)\n]+\))+)"
    rf"|"
    # Pattern C inline: name + comma + official role.
    # Same blank-line continuation rule as RE_SPEAKER_LINE Pattern C; same TOC
    # negative lookahead blocks "Tagesordnungspunkt N" / "Anlage N" / page numbers.
    rf"(?P<official_inline>{_FULL_NAME},\s*(?:{_OFFICIAL_ROLES})[^\n:]{{0,80}}"
    rf"(?:\n\n?(?!Tagesordnungspunkt\s*\d|Anlage\s*\d|\d{{4,}}\s)[^\n:]{{0,80}})?)"
    rf"|"
    # Pattern D inline: name + qualifier(s) + comma + parliamentary procedural role
    rf"(?P<member_role_inline>{_FULL_NAME}(?:\s+\([^)\n]+\))+,\s*(?:{_PARLIAMENTARY_ROLES})[^\n:]{{0,80}})"
    rf")"
    rf"\s*:\s*(?=\S)",
    re.MULTILINE,
)

# Used inside parse_speaker to break a chair line into role + name.
# Optional "Frau"/"Herr" prefix before the role keyword is consumed but not stored.
RE_CHAIR_PREFIX = re.compile(
    r"^(?:Frau\s+|Herr\s+)?(Alterspräsident(?:in)?|Vizepräsident(?:in)?|Bundespräsident(?:in)?|Präsident(?:in)?)\s+(.+)$",
    re.IGNORECASE,
)

# Used inside parse_speaker to break an official line into name + role.
RE_OFFICIAL_SPLIT = re.compile(
    rf"^({_FULL_NAME}),\s*({_OFFICIAL_ROLES})(.*)$",
)

# Pattern D split: name + qualifiers + comma + parliamentary role.
# e.g. "Dr. Kather (GB/BHE), Anfragender" →
#       name="Dr. Kather", qualifiers="(GB/BHE)", role="Anfragender"
RE_MEMBER_ROLE_SPLIT = re.compile(
    rf"^({_FULL_NAME})((?:\s+\([^)\n]+\))+),\s*({_PARLIAMENTARY_ROLES}.{{0,80}})$"
)

# State-level role prefix that may appear BEFORE the name (Pattern B base_name cleanup).
RE_STATE_ROLE_PREFIX = re.compile(
    r"^(Regierender\s+Bürgermeister(?:in)?|Ministerpräsident(?:in)?)\s+(.+)$"
)

# Matches the parsed role string of a presiding officer.
# Used in the TOC preamble filter inside split_speeches().
_RE_CHAIR_ROLE = re.compile(
    r"(?:Alters|Vize|Bundes)?Präsident(?:in)?$", re.IGNORECASE
)

# Patterns that definitively identify a speech body as a TOC page-reference list
# rather than actual speech content.  Used in split_speeches() to discard
# section-level TOC entries in early WPs (WP01–WP05) before the first chair.
#
# NOTE: \d{2,} (not \d{4,}) because early WPs (WP03–WP05) have 2-3 digit page
# numbers (e.g. "69 A"–"136 D" in WP05, starting from page 69 of the printed
# document), while later WPs accumulate 4-digit page counts.
_RE_TOC_MARKERS = re.compile(
    r"(?:"
    r"\bals\s+Berichterstatter\b"       # "als Berichterstatter 2402 A"
    r"|\bSchriftlicher\s+Bericht\b"     # "Schriftlicher Bericht 2460"
    r"|\bAnfragender\s+\d{2,}"         # "Dr. X, Anfragender 10785C" or "Anfragender 42 B"
    r"|\bFrage\s+\d+\s+des\s+Abg\."   # "Frage 9 des Abg. Baier:" (Fragestunde TOC body)
    r"|^\S.*\d{2,}\s*[A-D]\s*$"       # line ending "Name 71 B" / "Lücke (CDU/CSU) 2403 D"
    r")",                               # column letter required → avoids year-only false positives
    re.IGNORECASE | re.MULTILINE,
)

# A page reference embedded in a speaker attribution line, e.g. "2019 C" in
# "Schäffer, Bundesminister 2019 C Frage 8 des Abg. Baier (Mosbach)", or
# "71 A" in "Grund, Staatssekretär 71 A Frage des Abg. Ertl" (WP05 TOC).
# Legitimate speaker attributions never contain a bare digit sequence followed
# by a column letter A–D — this pattern is exclusive to session-opening TOC
# entries where pdftotext -raw concatenates page-ref + next question title.
# Uses \d{2,} (not \d{4,}) to catch early-WP 2-3 digit page refs (WP03–WP05).
_RE_TOC_RAW_PAGEREF = re.compile(r"\b\d{2,}\s*[A-D]\b")


def _is_toc_body(text: str) -> bool:
    """Return True if the speech body consists of TOC page-reference entries."""
    stripped = text.strip()
    if not stripped:
        return True
    return bool(_RE_TOC_MARKERS.search(stripped))


def _is_toc_entry(sp: dict) -> bool:
    """Return True if this speech dict is a TOC artifact.

    Two signals, either sufficient:
    1. Body text contains TOC page-reference patterns (_is_toc_body).
    2. The raw speaker attribution itself contains an embedded page reference
       such as "2019 C" — this exclusively occurs in session-opening TOC
       entries where the format is "Name, Role PAGENUM COL Frage N des Abg. X".
    """
    if _is_toc_body(sp.get("text", "")):
        return True
    if _RE_TOC_RAW_PAGEREF.search(sp.get("speaker_raw", "")):
        return True
    return False

# A parenthesised qualifier, e.g. "(SPD)" or "(Minden)".
RE_PAREN_QUALIFIER = re.compile(r"\(([^)]+)\)")

# Drucksache / parliamentary reference — always ignore as qualifier.
RE_DRUCKSACHE = re.compile(r"^(?:Drucksache|Frage[n]?\s)", re.IGNORECASE)

# Beifall (applause) annotation appended to a role or party field, e.g.
# "(von Abgeordneten der SPD mit Beifall begrüßt)".  Strip from roles;
# treat as disqualifying garbage in party qualifiers.
RE_BEIFALL = re.compile(r"\s*\([^)]*\bbegrüßt\b[^)]*\)", re.IGNORECASE)

# Strips "Abgeordnete/n/r" prefix variants (incl. OCR typos "Abgeordenten",
# "Abgeordeten") and the no-space variant ("AbgeMichaela" → "Michaela").
_RE_NAME_ABGEORDNETE = re.compile(
    r'^(?:Abgeordne\w*|Abge(?=[A-Z]))\s*', re.IGNORECASE
)
# Strips leading German function words that leaked from the preceding sentence.
# "Aber auch Roth" → "Roth"; "Aber Schmidt" → "Schmidt"
_RE_NAME_WORD_PREFIX = re.compile(
    r'^(?:Aber\s+auch\s+|Aber\s+|Auch\s+)', re.IGNORECASE
)

# Parenthetical interjection in speech body text.
# In Bundestag Plenarprotokolle every parenthetical within a speech is an
# audience reaction or interjection, never the speaker's own words, e.g.:
#   (Beifall bei der SPD)
#   (Heiterkeit und Beifall)
#   (Widerspruch bei der CDU/CSU)
#   (der SPD und dem BÜNDNIS 90/DIE GRÜNEN: Oh!)
#   (Dr. Müller (CDU/CSU): Das stimmt nicht!)
# The pattern matches a single non-nested parenthetical.  Applied iteratively
# in _strip_interjections() to handle one level of nesting without backtracking.
# The original nested pattern r"\((?:[^()]*|\([^()]*\))*\)" caused catastrophic
# backtracking on WP01 OCR text that contains many unmatched '(' characters.
RE_PARENTHETICAL = re.compile(r"\([^()]*\)")

# Party-field garbage: Drucksache session-number references or
# Tagesordnungspunkt labels that leaked into the party field via OCR.
RE_PARTY_GARBAGE = re.compile(
    r"^\d"
    r"|\bDrucksache\b|\bDr\.\s+ucksache\b|\bDr\s+ucksache\b"  # "Dr ucksache" OCR split
    r"|\bDruck-\s*sache\b|\bDrucksa-\s*che\b"   # line-break within "Drucksache" (pdftotext artifact)
    r"|\bTagesordnungspunkt\b",
    re.IGNORECASE,
)

# Hyphen-break artefacts in party names not fully joined by text cleaning,
# e.g. "BÜNDNIS 90/DIE GRÜ-NEN" → "BÜNDNIS 90/DIE GRÜNEN".
_PARTY_DEHYPHEN: list = [
    (re.compile(r"\bGRÜ-\s*NEN\b"),  "GRÜNEN"),   # covers "GRÜ-NEN" and "GRÜ- NEN"
    (re.compile(r"\bLIN-\s*KE\b"),   "LINKE"),
    (re.compile(r"\bBÜND-\s*NIS\b"), "BÜNDNIS"),
]

# Regex for the severe variant where a full speaker attribution line was
# embedded inside the party field by two-column OCR leakage, e.g.
# "BÜNDNIS 90/DIE GRÜ-Thomas Rachel, Parl. Staatssekretär...-NEN".
# Matches any "GRÜ-<5+ chars>-NEN" pattern (short "GRÜ-NEN" is handled above).
RE_PARTY_EMBEDDED_NAME = re.compile(
    r"^BÜNDNIS\s*90/DIE\s*GRÜ-.{5,}-NEN\s*$",
    re.IGNORECASE,
)

# Canonical party spellings.  Applied as a final lookup after all other
# party-field cleaning.  Keys are exact post-cleaned strings (after slash
# normalisation, so keys must NOT contain " / " or "/ " or " /").
_PARTY_NORMALISE: dict[str, str] = {
    # CDU/CSU OCR variants ------------------------------------------------
    "CDU %CSU":  "CDU/CSU",   "CDU ' CSU": "CDU/CSU",
    "CDU 'CSU":  "CDU/CSU",   "CDU /CUS":  "CDU/CSU",
    "CDU CSU":   "CDU/CSU",   "CDU, CSU":  "CDU/CSU",
    "CDU. CSU":  "CDU/CSU",   "CDU/CDU":   "CDU/CSU",
    "CDU/CUS":   "CDU/CSU",   "CDU/SCSU":  "CDU/CSU",
    "CDU: CSU":  "CDU/CSU",   "CDU; CSU":  "CDU/CSU",
    "CDUCSU":    "CDU/CSU",   "CDT T/CSU": "CDU/CSU",
    "CSU/CDU":   "CDU/CSU",   "CSU/CSU":   "CDU/CSU",
    # "alle/beide CDU/CSU" — multiple members speaking jointly
    "alle CDU/CSU":             "CDU/CSU",
    "beide CDU/CSU":            "CDU/CSU",
    "alle BÜNDNIS 90/DIE GRÜNEN": "BÜNDNIS 90/DIE GRÜNEN",
    # BÜNDNIS 90 slash/spacing/encoding OCR variants --------------------
    "BÜNDNIS/90 DIE GRÜNEN":   "BÜNDNIS 90/DIE GRÜNEN",
    "BÜNDNIS 90/Die GRÜNEN":   "BÜNDNIS 90/DIE GRÜNEN",
    "BÜNDNIS90/ DIE GRÜNEN":   "BÜNDNIS 90/DIE GRÜNEN",
    "BÜNDNIS90/DIE GRÜNEN":    "BÜNDNIS 90/DIE GRÜNEN",   # post-slash-norm form of above
    "BÜNGNIS 90/DIE GRÜNEN":   "BÜNDNIS 90/DIE GRÜNEN",   # OCR: NG instead of ND
    "BÜNNIS 90/DIE GRÜNEN":    "BÜNDNIS 90/DIE GRÜNEN",   # OCR: missing D
    "BÜNDNIS 90/DIE GRÜNNEN":  "BÜNDNIS 90/DIE GRÜNEN",   # double N
    "BÜNDNIS 90/DIE GÜNEN":    "BÜNDNIS 90/DIE GRÜNEN",   # OCR: missing R
    "BÜNDNIS 90/DIE GÜNEN":    "BÜNDNIS 90/DIE GRÜNEN",   # OCR: missing R (alt encoding)
    "B NDNIS 90/DIE GR NEN":   "BÜNDNIS 90/DIE GRÜNEN",   # Ü lost in encoding
    "BÜNDNIS 90/DIEGRÜNEN":    "BÜNDNIS 90/DIE GRÜNEN",   # no space before DIE
    "Bündnis 90/DIEGRÜNEN":    "BÜNDNIS 90/DIE GRÜNEN",   # mixed-case + no space
    "BÜNDNIS DIE GRÜNEN":      "BÜNDNIS 90/DIE GRÜNEN",   # OCR: missing "90/"
    "Bündnis 90/GRUNE":        "BÜNDNIS 90/DIE GRÜNEN",   # missing Ü and DIE
    "Bündnis 90/GRÜNE":        "BÜNDNIS 90/DIE GRÜNEN",   # lower-b variant
    "Grüne/Bündnis 90":        "BÜNDNIS 90/DIE GRÜNEN",   # reversed order
    "DIE GRÜNEN/Bündnis 90":   "BÜNDNIS 90/DIE GRÜNEN",
    # CDU guest / OCR artifacts
    "CDU-Gast": "CDU/CSU",
    "CD U":     "CDU/CSU",
    # BÜNDNIS 90/DIE GRÜNEN variants (post-1993 merger) -------------------
    "BÜNDNIS 90/D1E GRÜNEN":  "BÜNDNIS 90/DIE GRÜNEN",   # digit 1 → I
    "BÜNDNISS 90/DIE GRÜNEN": "BÜNDNIS 90/DIE GRÜNEN",   # double S
    "BÜNDNS 90/DIE GRÜNEN":   "BÜNDNIS 90/DIE GRÜNEN",   # missing I
    "Bündnis 90/DIE GRÜNEN":  "BÜNDNIS 90/DIE GRÜNEN",
    "Bündnis 90/Die Grünen":  "BÜNDNIS 90/DIE GRÜNEN",
    "Bündnis 90/GRÜNE":       "BÜNDNIS 90/DIE GRÜNEN",
    "Bündnis90/Die Grünen":   "BÜNDNIS 90/DIE GRÜNEN",
    "BÜNDNIS 90/GRÜNE":       "BÜNDNIS 90/DIE GRÜNEN",
    "GRÜNE/Bündnis 90":       "BÜNDNIS 90/DIE GRÜNEN",
    "DIE GRÜNEN":             "BÜNDNIS 90/DIE GRÜNEN",   # post-merger variant without BÜNDNIS 90
    # GRÜNE variants (pre-merger, WP10-12) --------------------------------
    "GRUÑE": "GRÜNE",
    # FDP variants --------------------------------------------------------
    "F.D.P":  "FDP",   "F.D.P.": "FDP",
    "F.D:P.": "FDP",   "FPD":    "FDP",
    "FD?":    "FDP",   "F13":    "FDP",   # OCR misreads of FDP
    "FDP-Hosp.": "FDP",                   # FDP hospitant (attached non-member)
    # FVP (Freie Volkspartei, WP02-03) ------------------------------------
    "F VP": "FVP",
    # DIE LINKE -----------------------------------------------------------
    "Die Linke": "DIE LINKE",
    # PDS variants --------------------------------------------------------
    "PDS/LinkeListe":  "PDS/Linke Liste",
    "PDS/LL":          "PDS/Linke Liste",
    "Gruppe der PDS":  "PDS",
    # DP variants ---------------------------------------------------------
    "DP [FVP]":  "DP/FVP",   "DP[FVP]": "DP/FVP",
    "DP-Gast":   "DP",
    # SPD OCR artifacts ---------------------------------------------------
    "SPP": "SPD",
    "SDP": "SPD",   # OCR letter-swap (WP02-08 scanned PDFs); all 46 instances verified SPD members
    # FU OCR artifact -----------------------------------------------------
    "EU":  "FU",    # OCR misread of FU (Föderalistische Union) in WP01 scanned PDFs
    # fraktionslos / parteilos capitalisation variants --------------------
    "fraktionlos":   "fraktionslos",
    "Fraktionslos":  "fraktionslos",
    "Fraktionsios":  "fraktionslos",   # OCR: l→i
    "Franktionslos": "fraktionslos",   # OCR: extra n
    "Frakionslos":   "fraktionslos",   # OCR: missing T
    "Parteilos":     "parteilos",      # capital P variant
    # Gruppe der PDS typo
    "Guppe der PDS": "PDS",
    # CDU/CSU guest variants
    "CDU/CSU-Gast":  "CDU/CSU",
    # beide → strip; normalise to the underlying party abbreviation
    "beide F.D.P.":  "F.D.P.",
    "beide FDP":     "FDP",
}

# City names that may appear as party values due to double-location parsing.
# When a member has (City1) (City2) qualifiers, City2 gets mis-assigned as party.
_CITY_NOT_PARTY: frozenset = frozenset({
    "Frankfurt", "Recklinghausen", "Wangen",
    # Cities confirmed from party-field audit (should be location or null)
    "Soest", "Bremerhaven", "Oberhausen", "Lindlar", "Düsseldorf",
})

# Stage-direction / procedural annotations that leak into the party field.
# These are never party names and must be nulled out.
RE_PARTY_STAGE_DIRECTION = re.compile(
    r"^(?:"
    r"vom\s+Platz(?:e)?\s+(?:aus\s+)?sprechend"
    r"|zum\s+Präsident"
    r"|auf\s+den\s+Abg\."
    r"|der\s+Abgeordnete\s+trägt"
    r"|Senator\s+des\s+Landes"           # role leaked into party field
    r"|(?:Justiz|Innen|Finanz|Wirtschafts?)minister\s+des\s+Landes"
    r"|Minister\s+des\s+Landes"
    r"|Parl(?:amentarisch(?:er?|es?))?\.\s*Staatssekretär(?:in)?"
    r"|Parlamentarische[rs]?\s+Staatssekretär(?:in)?"
    r"|Staatsminister(?:in)?(?:\s+(?:bei|im|in|AA)\b|\s+[A-Z]{2}\b)"
    r"|Bundesminister(?:in)?\s+(?:der|für|des)\b"
    r"|Bundesministerium\s+(?:der|für|des)\b"
    r"|Schriftführer(?:in)?"
    r")",
    re.IGNORECASE,
)

# Guest/observer annotations (not regular party members).
RE_PARTY_GUEST = re.compile(
    r"^(?:Gast|gast)(?:\s+(?:FDP|SPD|CDU|CSU|GRÜNE|LINKE|DIE\s+LINKE))?$"
    r"|^SPD-Gast$",
    re.IGNORECASE,
)


# Applied in sequence inside _normalise_role(); each entry is (pattern, repl).
_ROLE_OCR_FIXES: list[tuple[re.Pattern, str]] = [
    # ---- Strip two-column OCR leakage FIRST (embedded name/sentence) -----
    # Handle the specific "beim/im Bun-<leakage>" pattern BEFORE the general
    # strip rule, so the full context word is preserved.
    (re.compile(r"\bbeim\s+Bun-[^\n]*$",          re.I), "beim Bundesminister"),
    (re.compile(r"\bim\s+Bun-[^\n]*$",            re.I), "im Bundesministerium"),
    (re.compile(r"\bbei\s+der\s+Bun-[^\n]*$",     re.I), "bei der Bundesministerin"),
    # "beim Bundes-[Surname]" — the general leakage strip requires 2+ words, so
    # single-surname leaks ("beim Bundes-Gramling", "beim Bundes-Renner") slip through.
    (re.compile(r"\bbeim\s+Bundes-\w+.*",          re.I | re.DOTALL), "beim Bundesminister"),
    (re.compile(r"\bbei\s+der\s+Bundes-\w+.*",    re.I | re.DOTALL), "bei der Bundesministerin"),
    (re.compile(r"\bStaatsminister(?:in)?\s+bei\s+der\s+Bundes-\w+.*",
                re.I | re.DOTALL), "Staatsminister"),
    # "beim Bundesmi-[uppercase-leakage]" — truncated mid-syllable with column break.
    (re.compile(r"\bbeim\s+Bundesmi-[A-ZÄÖÜ].*",  re.I | re.DOTALL), "beim Bundesminister"),
    (re.compile(r"\bbei\s+der\s+Bundesmi-[A-ZÄÖÜ].*", re.I | re.DOTALL), "bei der Bundesministerin"),
    # "im Bun[non-des-content]" — content-stream leakage without hyphen.
    # "im Bundesministerium..." is preserved by (?!des); all other "im Bun..." is garbage.
    (re.compile(r"\bim\s+Bun(?!des)\S*.*",         re.I | re.DOTALL), "im Bundesministerium"),
    # "des In-[leakage]" — truncated "des Innern" with right-column content after hyphen.
    (re.compile(r"\bdes\s+In-\S*.*",               re.I | re.DOTALL), "des Innern"),
    # "Staatsminister im Auswär-[leakage]"
    (re.compile(r"\bim\s+Auswär-[A-ZÄÖÜ].*",      re.I | re.DOTALL), "im Auswärtigen Amt"),
    # "Auswärti-[leakage]" — "Auswärtigen" truncated mid-word with right-column content.
    (re.compile(r"\bAuswärti-[^\n]*",              re.I), "Auswärtigen Amt"),
    # "im Auswärti" / "im Auswär" — truncated without hyphen.
    (re.compile(r"\bim\s+Auswärti\b",              re.I), "im Auswärtigen Amt"),
    # General strip: role text ends with "Xxx-Name Name" or "-Vizepräsident..." etc.
    # These arise when pdfminer band-reads insert right-column content into the
    # role description mid-word (e.g. "Sozia-Hubertus Heil, Bundesminister...").
    (re.compile(
        r"-(?:(?:Dr\.\s+|Frau\s+|Herr\s+)?[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß]+(?:\s+[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß]+)+\s*[,(]"
        r"|Vizepräsident\w*|Präsident\w*"
        r"|Tagesordnungspunkt|Zusatztagesordnungspunkt|Überweisungsvorschlag"
        r"|Persönliche\s+Bemerkungen?|Fragestunde|Drucksache"
        r"|Ich\s+rufe|Wir\s+kommen"
        r"|GRÜNEN?\)|NEN\))[^\n]*$",
        re.I,
    ), ""),

    # ---- Em-dash / en-dash leakage (pdftotext two-column artifact) -------
    # pdftotext sometimes inserts a dash separator where the left column text
    # ends and right-column content begins:
    #   "Bundesminister für Arbeit und Sozialord— Kommunen..." → "Sozialord"
    #   "Parl. Staatssekretär – Antrag" → "Parl. Staatssekretär"
    # Strip from the dash (with at least one trailing space) to end.
    # Allow zero or one space BEFORE the dash so "Sozialord— " is also caught.
    (re.compile(r"\s*[—–]\s+.*", re.DOTALL), ""),

    # ---- pdftotext column markers embedded mid-word ----------------------
    # pdftotext inserts (B) / (D) column-boundary markers mid-word:
    # "Aus(B) wärtigen" → "Auswärtigen", "In(D) nern" → "Innern",
    # "Natur(B) schutz" → "Naturschutz", "wirtschaftli(B) che" → "wirtschaftliche",
    # "Vertei(D) digung" → "Verteidigung", "Stadt(B) entwicklung" → "Stadtentwicklung"
    (re.compile(r"\s*\([BD]\)\s*"), ""),

    # ---- Two-column mid-word split at "Bun" level --------------------------------
    # pdftotext inserts right-column text between "Bun" and "desminister" when the
    # word "Bundesminister" spans the column boundary.
    # The letter after "Bun" is ALWAYS uppercase in such leakage; "d" in
    # "Bundesminister" is lowercase — safe discriminator.
    (re.compile(r'\bBun([A-ZÄÖÜ].*?)desminister\b', re.DOTALL), 'Bundesminister'),
    # Truncated "beim/im BunXXX" with no recoverable "desminister" → strip tail
    (re.compile(r'\s+(?:beim|im)\s+Bun[A-ZÄÖÜ](?!desminister).*$', re.DOTALL), ''),

    # ---- Complete truncated word stubs left after leakage strip ----------
    (re.compile(r"\bSozia\s*$",         re.I), "Soziales"),
    (re.compile(r"\bSozial\s*$",        re.I), "Soziales"),
    (re.compile(r"\bAus\s*$"),               "Auswärtigen"),
    (re.compile(r"\bWirt\s*$",          re.I), "Wirtschaft"),
    (re.compile(r"\bWirts\s*$",         re.I), "Wirtschaft"),
    (re.compile(r"\bVertei\s*$",        re.I), "Verteidigung"),

    # ---- Page-reference contamination ------------------------------------
    # Some role strings have Kürschner page refs appended: "... 12710B, C ergebnisse"
    # or TOC number leakage: "Staatssekretär ... . . . 8711C, D Kaffee-text".
    # Strip from the first 4+-digit number followed by a column letter to end.
    (re.compile(r"[\s.]*\d{4,}[A-D]\b.*", re.DOTALL), ""),

    # ---- Fragestunde TOC dot-leader contamination ------------------------
    # WP03–WP09 Fragestunde sections open with a mini-TOC:
    #   "Bundesminister . . . . . A  Frage des Abg. Dr. Schmidt"
    #   "Bundesminister . . . . . B  Frage der Abg. Frau Müller"
    # Pattern C captures "Bundesminister" as the role start and the
    # dot-leaders + question reference as the role description.
    # Fix: strip everything from the first sequence of two or more
    # space-separated dots (TOC leaders) onwards.
    # Two or more spaced dots = TOC leader; single "." after abbreviations
    # ("Parl.", "Dr.") is never preceded by a space, so this is safe.
    (re.compile(r"\s+\.[\s.]*\..*", re.DOTALL), ""),
    # "(BMI) A Frage ..." — ministry abbreviation + column marker variant
    (re.compile(r"\s+\([A-ZÄÖÜ]{2,}\)\s+[A-D]\s+Frage.*", re.DOTALL | re.I), ""),
    # Single-dot column marker: " . A Frage des Abg." / " . B text..."
    # (one spaced dot before the column letter, no dot-leaders)
    (re.compile(r"\s+\.\s+[A-D]\b.*", re.DOTALL), ""),
    # No-dot standalone column marker: " A Frage des Abg." / " A, B Fragen..."
    # In legitimate German role strings A/B/C/D never appear as standalone words
    # (surrounded by spaces); they exclusively mark Fragestunde TOC columns here.
    (re.compile(r"\s+[A-D](?:,\s*[A-D])*\s+.*", re.DOTALL), ""),
    # Ministry abbreviation residue "(BMI)" "(AA)" etc. left at end after the
    # column-marker strip (the abbreviation itself was never part of the role).
    # Also handles fullwidth parens （BMI） that pdftotext emits from some PDFs.
    (re.compile(r"\s*[\(（][A-ZÄÖÜ]{2,5}[\)）]\s*$"), ""),

    # ---- pdftotext Tagesordnungspunkt / procedural suffix ----------------
    # pdftotext embeds Fragestunde agenda items into role strings:
    #   "Bundesminister BK Tagesordnungspunkt 1" → "Bundesminister"
    #   "Parl. Staatssekretärin Zusatztagesordnungspunkt 2" → "Parl. Staatssekretärin"
    #   "Bundesminister BMI Zusatztagesordnungspunkt 6" → "Bundesminister"
    # Optional 2–4-char all-caps ministry abbreviation (BK, AA, BMI, BMA …)
    # between the role label and the keyword.
    (re.compile(r"\s+(?:[A-Z]{2,4}\s+)?(?:Zusatz)?[Tt]agesordnungspunkt\b.*", re.DOTALL), ""),
    # Procedural section headers leaking into role strings:
    #   "Bundesminister Befragung der Bundesregierung" → "Bundesminister"
    #   "Bundesminister Weitere Fragen" / "Zur Geschäftsordnung" etc.
    #   "Parl. Staatssekretär BMA Zu Protokoll gegebene Reden zu" → "Parl. Staatssekretär"
    #   "Bundesministerin BMJ Zu Protokoll gegebene Reden zu" → "Bundesministerin"
    (re.compile(
        r"\s+(?:[A-Z]{2,5}\s+)?"    # optional ministry abbreviation (BMA, BMVEL …)
        r"(?:Befragung\s+der\s+Bundesregierung|Weitere\s+Fragen|Zur\s+Geschäftsordnung"
        r"|Vereinbarte\s+Debatte|Zusatzpunkt\b|Fragestunde\b"
        r"|Zu\s+Protokoll\b"
        r"|Antrag\s+der\s+Bundesregierung\b"
        r"|Beschlussempfehlung\b"
        r"|Bericht\s+des\s+(?:Petitions|Ausschuss)"
        r"|Ausschusses?\s+für\b"
        r"|Abgabe\s+einer\s+Erkl[äa]rung\b).*",
        re.DOTALL | re.I,
    ), ""),
    # Ministry abbreviation (without parens) + everything after it:
    #   "Bundesminister BMI" → "Bundesminister"
    #   "Bundesministerin BMBF" → "Bundesministerin"
    #   "Parl. Staatssekretär BMVg" → "Parl. Staatssekretär"
    #   "Parl. Staatssekretärin BMWi Vizepräsident Dr. X" → "Parl. Staatssekretärin"
    #   "Staatsminister AA" → "Staatsminister"
    # Pattern: BM + 1–4 letters (handles mixed-case: BMVg, BMBau, BMWi, BMVEL, BMVBW)
    # plus AA (Auswärtiges Amt) and BK (Bundeskanzleramt).
    (re.compile(r"\s+\b(?:AA|BK|BM[A-Za-z]{1,4})\b.*", re.DOTALL), ""),

    # ---- Article / preposition OCR confusions ----------------------------
    # "vier Finanzen" → "der Finanzen"  (v/d and i/e optical swap in WP01-era)
    (re.compile(r"\bvier\s+(?=Finanzen|Innern|Justiz|Verteid|Wirtsch|Arbeit)", re.I), "der "),
    # "Beis" → "des"  (d→B and s split — one-off OCR error in WP03-era)
    (re.compile(r"\bBeis\b(?=\s+Innern|\s+Außer|\s+Auswär)", re.I), "des"),
    # Stray apostrophe BEFORE the role keyword: "Bundesminister' für ..." → "Bundesminister für"
    (re.compile(r"(Bundesminister(?:in)?|Bundeskanzler(?:in)?)\s*'\s*", re.I), r"\1 "),

    # ---- "Bundesminister" compound splits and OCR typos ------------------
    (re.compile(r"Bundes[-)\s]{1,3}minister(?!\w)", re.I), "Bundesminister"),
    (re.compile(r"Bundesdes\s*minister",             re.I), "Bundesminister"),
    (re.compile(r"Bundes\s+mint?ser",                re.I), "Bundesminister"),  # "mintser"
    (re.compile(r"Bundesmins[it]{1,2}er",            re.I), "Bundesminister"),
    (re.compile(r"Bundesmi\s+nister",                re.I), "Bundesminister"),
    (re.compile(r"Bundesmini\s*ster(?!\w)",          re.I), "Bundesminister"),
    # "Bundesministerfür" / "BundesministerderJustiz" — missing space before article.
    # Replace "Bundesminister(in)?<article>" with "Bundesminister(in)? <article>".
    # Extra spaces are collapsed by the "  +" rule at the end.
    (re.compile(r"(Bundesminister(?:in)?)\s*(für|der|des|beim|im)(?=\s|[A-ZÄÖÜa-zäöü])", re.I),
                                                             r"\1 \2 "),

    # ---- Two-column mid-word split INSIDE "Bundesminister" -------------------
    # pdftotext inserts right-column content between "Bundesmini" and "ster".
    # The first char of inserted text is always uppercase.
    (re.compile(r'\bBundesmini([A-ZÄÖÜ].*?)ster\b', re.DOTALL), 'Bundesminister'),
    # Hyphen-split variants: "Bundesmini-" / "Bundesmini- -ster" / "Bundesmini- . ster"
    (re.compile(r'\bBundesmini-\s*\.?\s*-?\s*ster\b', re.I), 'Bundesminister'),
    # Truncated "Bundesmini" before a preposition:
    #   "beim Bundesmini für Arbeit" → "beim Bundesminister für Arbeit"
    (re.compile(r'\bBundesmini\s+(?=für|der|des|beim|im\b)', re.I), 'Bundesminister '),
    # "Bundeskanzleramt" with OCR space: "Bundeskanzler amt" → "Bundeskanzleramt"
    (re.compile(r"\bBundeskanzler(in)?\s+(amts?|amt)\b", re.I), r"Bundeskanzler\1\2"),

    # ---- "Bundesministerium" OCR variants --------------------------------
    # Split variants only (require whitespace) — don't change correctly spelled form
    (re.compile(r"Bundesministe\s+riums?\b",     re.I), "Bundesministerium"),
    (re.compile(r"Bundesministeri\s+ums?\b",     re.I), "Bundesministerium"),
    (re.compile(r"Bundesministenium\b",          re.I), "Bundesministerium"),
    (re.compile(r"Bundesminirium\b",             re.I), "Bundesministerium"),
    (re.compile(r"Bundesministreium\b",          re.I), "Bundesministerium"),
    (re.compile(r"Bundesministrium\b",           re.I), "Bundesministerium"),
    (re.compile(r"Bundesministrum\b",            re.I), "Bundesministerium"),
    (re.compile(r"Bundesministesterium\b",       re.I), "Bundesministerium"),
    (re.compile(r"Bundesministe\s+rium\b",       re.I), "Bundesministerium"),
    (re.compile(r"Bundesminis\s+ter(?:ium)?\b",  re.I), "Bundesministerium"),
    (re.compile(r"Bundesministe\s*r\b(?!\w)",    re.I), "Bundesminister"),
    (re.compile(r"Bundesmini\s+sterium\b",       re.I), "Bundesministerium"),
    (re.compile(r"Bundesmini\s+ster(?:ium)?\b",  re.I), "Bundesministerium"),
    (re.compile(r"Bundesminin+ister(?:ium)?\b",  re.I), "Bundesministerium"),
    (re.compile(r"Bundesminimini\w+",            re.I), "Bundesministerium"),
    (re.compile(r"Bundesmimini\w+",              re.I), "Bundesministerium"),
    # "im/beim Bundesministeriums" — wrong case; "des Bundesministeriums" is correct German
    (re.compile(r"\b(im|beim)\s+Bundesministeriums\b"), r"\1 Bundesministerium"),

    # ---- "beim/im" OCR variants ------------------------------------------
    # "dm" → "im" (optical confusion d/i before "Bundesministerium")
    (re.compile(r"\bdm\s+(?=Bundesministerium)", re.I), "im "),
    # "heim" / "beire" / "beirr" / "belm" → "beim" before "Bundesminister"
    (re.compile(r"\bheim\s+(?=Bundes)",          re.I), "beim "),
    (re.compile(r"\bbe[ilr][mr]e?\s+(?=Bundes)", re.I), "beim "),
    # "b e im" → "beim"
    (re.compile(r"\bb\s+e\s+im\b",              re.I), "beim"),

    # ---- Preposition "des/der/für/im" OCR variants -----------------------
    # "ides", "deis", "dies", "dse", "dier" → "des"
    (re.compile(r"\b(?:ides|deis|dies|dse|dier)\s+(?=(?:Innern|Auswärtigen|Bundesm|Bundes|Justiz|Verteidi|Finan|Landes?))",
                re.I), "des "),
    # "innderdeutsche" / "innerdeutsch" variants
    (re.compile(r"\binn+erdeutsch",              re.I), "innerdeutsch"),
    # "fair" / "fur" → "für" before known ministry department words
    (re.compile(r"\b(?:fair|fur)\s+(?=(?:Ernährung|Vertriebene|Wirtschaft|Arbeit|Gesundh|Bildung|Justiz|Raumord|Familie|Verkehr|Umwelt|Post|Forsch))",
                re.I), "für "),

    # ---- Stray punctuation around prepositions / articles ----------------
    # E.g. "für. Ernährung", "der- Justiz", "des; Innern", "!des Innern"
    # Case 1: punct is attached TO the preposition and followed by a word.
    (re.compile(r"\b(für|der|des|die|dem|den|beim|im|am|zu|zur|zum|von|bei)\s*[.;,\-!]+\s+(?=\w)", re.I), r"\1 "),
    # Case 2: punct is before the preposition: " .der", " !des", " ;im"
    (re.compile(r"(?<=\s)[.;!]+(?=(?:für|der|des|die|dem|den|beim|im|am|zu|zur|zum)\b)", re.I), ""),
    # Case 3: stray leading hyphen or period before a capitalised word: " -Wirtschaft"
    (re.compile(r"(?<=\s)[-.](?=[A-ZÄÖÜ])"), ""),
    # Case 4: stray period after a ≥5-letter word before a preposition.
    # Fixed-length lookbehind (?<=\w{4}) ensures this won't strip "Dr." or "Parl."
    # (those abbreviations have <4 chars before the dot-bearing letter).
    (re.compile(r"(?<=\w{4})([a-zäöüß])\.\s+(?=(?:der|des|die|dem|den|für|beim|im|am|zu|zur|zum|von)\b)"), r"\1 "),

    # ---- Article / genitive corrections from corpus evidence -----------------
    (re.compile(r"\bFinanzzen\b",               re.I), "Finanzen"),  # double-z OCR
    (re.compile(r"\bdes\s+(?=Justiz\b)",        re.I), "der "),
    (re.compile(r"\bdes\s+(?=Verteidigung\b)",  re.I), "der "),
    (re.compile(r"\bdes\s+Innen\b(?!r)",        re.I), "des Innern"),
    (re.compile(r"\bfür\s*\.\s*das\b",          re.I), "für das"),
    (re.compile(r"\bführ\b"),                         "für"),
    (re.compile(r"\bfind\s+(?=Sozialordnung\b)", re.I), "und "),
    (re.compile(r"\bfür\s+[•·]\s*",             re.I), "für "),
    (re.compile(r"\bAuswä\s+rtigen\b",           re.I), "Auswärtigen"),
    (re.compile(r"\bfür\s+das\s+das\b",          re.I), "für das"),

    # ---- "Sozialordnung" typos / splits ----------------------------------
    (re.compile(r"Sozial\s+ordnung",            re.I), "Sozialordnung"),
    (re.compile(r"Sozialordnun{2,}g",           re.I), "Sozialordnung"),
    (re.compile(r"Sozialord\s+nung",            re.I), "Sozialordnung"),
    (re.compile(r"Sozialordun+g\b",             re.I), "Sozialordnung"),  # "Sozialordunng"
    (re.compile(r"Sozilordnug\b",               re.I), "Sozialordnung"),
    (re.compile(r"Sozialond\w+",                re.I), "Sozialordnung"),
    (re.compile(r"Sozialaufgaben\b",            re.I), "Sozialordnung"),  # "Sozialaufgaben" → era label
    # "Sozia-Ordnung" / "Sozial-Drdnung" / "Sozial-Ordnung"
    (re.compile(r"\bSozial[-–]\s*[Oo]rd",       re.I), "Sozialord"),
    # "Sozial-Drdnung" / "SozialDrdnung" — OCR swap D→O inside "Ord"
    # "Drdnung" = D-r-d-n-u-n-g; pattern: [Dd]rdnung covers this
    (re.compile(r"\bSozial-?[Dd]rdnung\b",       re.I), "Sozialordnung"),

    # ---- "Landwirtschaft" typos / splits ---------------------------------
    (re.compile(r"\bLand\s*-?\s*wir\s*t\s*schaft\b",    re.I), "Landwirtschaft"),
    (re.compile(r"\bLandwir[ts]{1,2}ch\s*aft\b",        re.I), "Landwirtschaft"),
    (re.compile(r"\bLandwirtschaft[en]+\b",             re.I), "Landwirtschaft"),
    (re.compile(r"\bLandwirt\s*wirtschaft\b",          re.I), "Landwirtschaft"),  # doubled
    (re.compile(r"\bLandwirtschaft\s*und\s*Forsten\b",  re.I), "Landwirtschaft und Forsten"),
    (re.compile(r"\bLandwirtschaft\s*und\b",            re.I), "Landwirtschaft und"),
    (re.compile(r"\bLandwirtschat\w*\b",                re.I), "Landwirtschaft"),
    (re.compile(r"\bLandwirts\s+chaft\b",               re.I), "Landwirtschaft"),
    (re.compile(r"\bLandwirt\s+schaft\b",               re.I), "Landwirtschaft"),
    (re.compile(r"\bLandw\s+i\s*rtschaft\b",            re.I), "Landwirtschaft"),
    (re.compile(r"\bLand\s+wirtschaft\b",               re.I), "Landwirtschaft"),
    (re.compile(r"\bLandschaft\b"),                           "Landwirtschaft"),  # OCR drop
    # "Landwirtschaft und .Forsten" / "und 'Forsten" etc.
    (re.compile(r"\bund\s+[.'`]\s*Forsten\b",           re.I), "und Forsten"),
    # "Landwirtschaft und und Forsten" (doubled "und")
    (re.compile(r"\bund\s+und\s+Forsten\b",             re.I), "und Forsten"),
    # "Fortsen" / "Forsten." (trailing dot)
    (re.compile(r"\bFortsen\b",                         re.I), "Forsten"),
    (re.compile(r"\bForsten\.",                         re.I), "Forsten"),

    # ---- "Wirtschaft" typos / splits (standalone) ------------------------
    (re.compile(r"\bWi\s+rtschaft\b",                   re.I), "Wirtschaft"),
    (re.compile(r"\bWirt\s+[Ss]chaft\b",                re.I), "Wirtschaft"),
    (re.compile(r"\bWirts\s*ch\s+aft\b",                re.I), "Wirtschaft"),
    (re.compile(r"\bWirschaft\b",                       re.I), "Wirtschaft"),
    (re.compile(r"\bWartschaft\b",                      re.I), "Wirtschaft"),
    (re.compile(r"\bWirtchaft\b",                       re.I), "Wirtschaft"),
    (re.compile(r"\bW\s+i\s*rtschaft\b",                re.I), "Wirtschaft"),
    (re.compile(r"\bvv'irtschafl?\b",                   re.I), "Wirtschaft"),
    (re.compile(r"\bWutschaft\b",                       re.I), "Wirtschaft"),
    # ---- Two-column mid-word split in "Wirtschaft" ----------------------------
    (re.compile(r'\bWirt([A-ZÄÖÜ].*?)schaft\b', re.DOTALL), 'Wirtschaft'),

    # ---- Other common split/OCR words ------------------------------------
    (re.compile(r"\bBau\s+wesens?\b",           re.I), "Bauwesen"),
    (re.compile(r"\bVer[-']\s*kehr\b",          re.I), "Verkehr"),
    (re.compile(r"\bVer\s+kehr\b",              re.I), "Verkehr"),
    (re.compile(r"\bV\s+rkehr\b",               re.I), "Verkehr"),
    (re.compile(r"\bVerker\b",                  re.I), "Verkehr"),
    (re.compile(r"\bVerkekr\b",                 re.I), "Verkehr"),
    (re.compile(r"\bJ\s+ugend\b",               re.I), "Jugend"),
    (re.compile(r"\bFami\s+lie\b",              re.I), "Familie"),
    (re.compile(r"\bFamalie\b",                 re.I), "Familie"),
    (re.compile(r"\bFamile\b",                  re.I), "Familie"),
    (re.compile(r"\bFamin\s*e\b",               re.I), "Familie"),
    (re.compile(r"\bFaimilie\b",                re.I), "Familie"),
    # ---- Two-column mid-word split "Se[SPEECH]nioren" -------------------------
    (re.compile(r'\bSe([A-ZÄÖÜ].*?)nioren\b', re.DOTALL), 'Senioren'),
    (re.compile(r'\bSe--?\s+nioren\b',         re.I), 'Senioren'),
    (re.compile(r"\bGe\s+sundheit",             re.I), "Gesundheit"),
    (re.compile(r"\bGesunclheit",               re.I), "Gesundheit"),
    (re.compile(r"\bGesundheist\s*wesen\b",     re.I), "Gesundheitswesen"),  # full compound
    (re.compile(r"\bGesundhe[ia]st",            re.I), "Gesundheit"),
    (re.compile(r"\bGesundhteis\w+",            re.I), "Gesundheit"),
    (re.compile(r"\bGesundheit\s*wesen\b",      re.I), "Gesundheitswesen"),  # missing -s-
    (re.compile(r"\bForschun\b",                re.I), "Forschung"),
    (re.compile(r"\bIn{3,}ern\b",               re.I), "Innern"),         # "Innnern"
    (re.compile(r"\bInne\s+rn\b",               re.I), "Innern"),
    (re.compile(r"\bInnere\s*n?\b",             re.I), "Innern"),         # "Inneren" → "Innern"
    (re.compile(r"\bInern\b",                   re.I), "Innern"),         # missing n
    (re.compile(r"\bInnenre\b",                 re.I), "Innern"),         # OCR "re" suffix
    (re.compile(r"\bInnerv\b"),                        "Innern"),         # OCR v→n
    (re.compile(r"\bAuswärten\b",               re.I), "Auswärtigen"),
    (re.compile(r"\bAuswär\s+tigen\b",          re.I), "Auswärtigen"),
    (re.compile(r"\bAuswattigen\b",             re.I), "Auswärtigen"),
    (re.compile(r"\bAuswär\b\s+",               re.I), "Auswärtigen "),
    (re.compile(r"\bAuwärtigen\b",              re.I), "Auswärtigen"),
    (re.compile(r"\bAuswärtigen\s+Amis\b",      re.I), "Auswärtigen Amts"),
    (re.compile(r"\bAuswärtigen\s+Art\b",       re.I), "Auswärtigen Amt"),
    (re.compile(r"\bAuswärte\s*n\b",            re.I), "Auswärtigen"),
    (re.compile(r"\bAuswärtigen\s+Amts\s+\d+", re.I), "Auswärtigen Amts"),
    (re.compile(r"\bAuswärtngen\b",             re.I), "Auswärtigen"),
    (re.compile(r"\bVerteidigng\b",             re.I), "Verteidigung"),
    (re.compile(r"\bVertreidigung\b",           re.I), "Verteidigung"),
    (re.compile(r"\bVerteidignug\b",            re.I), "Verteidigung"),
    (re.compile(r"\bVerteiligung\b",            re.I), "Verteidigung"),
    (re.compile(r"\bJusitz\b",                  re.I), "Justiz"),
    (re.compile(r"\bJuistz\b",                  re.I), "Justiz"),   # OCR transposition ui→u
    (re.compile(r"\bRaumord\s+nung\b",          re.I), "Raumordnung"),
    (re.compile(r"\bRaumord\b\s*$",             re.I), "Raumordnung"),
    (re.compile(r"\bStädte\s+bau\b",            re.I), "Städtebau"),
    (re.compile(r"\bStädt(?:ie|e)\s*bau\b",     re.I), "Städtebau"),
    # ---- Two-column mid-word split in "Raumordnung" --------------------------
    # "Raumord[SPEECH]nung" — negative lookahead excludes direct "Raumordnung";
    # any char other than 'n' (which starts the legitimate "nung") triggers recovery.
    (re.compile(r'\bRaumord(?!nung\b).*?nung\b', re.DOTALL), 'Raumordnung'),
    (re.compile(r"\bWissen\s+schaft",           re.I), "Wissenschaft"),
    (re.compile(r"\bWissens\s+enschaftl",       re.I), "wissenschaftl"),
    (re.compile(r"\bwissensenschaft",           re.I), "wissenschaft"),
    # ---- Two-column mid-word split in "Wissenschaft" -------------------------
    # Uppercase-start leakage: "Wis[SPEECH]senschaft" where speech begins with capital
    (re.compile(r'\bWis([A-ZÄÖÜ].*?)senschaft\b', re.DOTALL), 'Wissenschaft'),
    # Lowercase-start leakage: "Wis[speech]senschaft" where speech begins lowercase
    # ("Wishabe...", "Wismich...", "Wisihr...", "Wistatsächlich...")
    # Safe: "Wissenschaft" itself has "Wiss..." → after "Wis"+"s", remaining="enschaft"
    # which does not contain "senschaft" (only "schaft"), so no false match.
    (re.compile(r'\bWis[a-zäöüß].*?senschaft\b', re.DOTALL), 'Wissenschaft'),
    (re.compile(r"\bTechnoolog",                re.I), "Technolog"),
    (re.compile(r"\bZusamemnarbeit\b",          re.I), "Zusammenarbeit"),
    # "Landwirtschäft" (ä→a) / "Landwirtschaut" (f→u OCR swap)
    (re.compile(r"\bLandwirtsch(?:[aäu]ft|[aä]ut)\b", re.I), "Landwirtschaft"),
    # "tür" → "für" (f/t OCR swap; standalone "tür" = "door" never appears in role strings)
    (re.compile(r"\btür\b"),                          "für"),
    # "innern" (lowercase-i) → "Innern"
    (re.compile(r"\binnern\b"),                        "Innern"),
    # "Familien fragen" → "Familienfragen" (OCR space in compound)
    (re.compile(r"\bFamilien\s+fragen\b",       re.I), "Familienfragen"),
    # "Bun desminister" / "Bun deskanzler" — OCR split inside "Bundes"
    (re.compile(r"\bBun\s+des(?=minister|ministerium|kanzler)", re.I), "Bundes"),
    # "Bundesministererin" → "Bundesministerin" (doubled -er- suffix)
    (re.compile(r"\bBundesministererin\b",       re.I), "Bundesministerin"),
    (re.compile(r"\bZusammenarbeit\s+",         re.I), "Zusammenarbeit "),
    (re.compile(r"\bZusammenarb\s+eit\b",       re.I), "Zusammenarbeit"),
    (re.compile(r"\bWohnungswesan\b",           re.I), "Wohnungswesen"),
    (re.compile(r"\bWohnunsgwesen\b",           re.I), "Wohnungswesen"),
    (re.compile(r"\bWohnungs\s+wesen\b",        re.I), "Wohnungswesen"),
    (re.compile(r"\bAngelegenheit\s+en\b",      re.I), "Angelegenheiten"),
    (re.compile(r"\bAngelegen\s+heiten\b",      re.I), "Angelegenheiten"),
    (re.compile(r"\bFamilienund\b",             re.I), "Familien und"),
    (re.compile(r"\bFernmeldwesen\b",           re.I), "Fernmeldewesen"),
    (re.compile(r"\bFermeldewesen\b",           re.I), "Fernmeldewesen"),
    (re.compile(r"\bFernmeldeweesen\b",         re.I), "Fernmeldewesen"),
    (re.compile(r"\bFernmeldewesens\b",         re.I), "Fernmeldewesen"),
    (re.compile(r"\bNa\s+turschutz\b",          re.I), "Naturschutz"),
    (re.compile(r"\bNadesminister\b",           re.I), "Bundesminister"),  # OCR Na→Bu
    (re.compile(r"\bBundesaufgaben\b",          re.I), "besondere Aufgaben"),
    (re.compile(r"\bbesondere\s+Auf\s+gaben\b", re.I), "besondere Aufgaben"),
    (re.compile(r"\bbesondere\s+Auf-\s*gaben\b",re.I), "besondere Aufgaben"),
    (re.compile(r"\bbesondere\s+Augaben\b",     re.I), "besondere Aufgaben"),
    (re.compile(r"\bBundeskanzamtes\b",         re.I), "Bundeskanzleramtes"),
    (re.compile(r"\bBundeskanzlerleramt\b",     re.I), "Bundeskanzleramt"),
    (re.compile(r"\bBundespressechef\b",        re.I), "Bundespressechef"),
    (re.compile(r"\bBundespressiechef\b",       re.I), "Bundespressechef"),
    (re.compile(r"\bBundesrpressechef\b",       re.I), "Bundespressechef"),
    # Staatsminister OCR variants
    (re.compile(r"\bStaatsministerr\b",         re.I), "Staatsminister"),
    (re.compile(r"\bAuswärti-?\s*gen\s+Amt\b",  re.I), "Auswärtigen Amt"),
    (re.compile(r"\bAuswärtigen\s+Auswärtigen\s+Amt\b", re.I), "Auswärtigen Amt"),
    (re.compile(r"\bAuswärtigen\s+Amt\s*\)\s*$", re.I), "Auswärtigen Amt"),
    # "Amt." trailing dot
    (re.compile(r"\bAmt\.\s*$",                 re.I), "Amt"),
    # Parl. Staatssekretär spacing: "Staatssekretärbeim" → "Staatssekretär beim"
    (re.compile(r"\bStaatssekretär(bei|im)\b",  re.I), r"Staatssekretär \1"),
    (re.compile(r"\bStaatssekretärs\b",         re.I), "Staatssekretär"),  # genitive bleed
    (re.compile(r"\bBundesminsterin\b",         re.I), "Bundesministerin"),
    (re.compile(r"\bBundesminsiter\b",          re.I), "Bundesminister"),
    (re.compile(r"\bBundesminster\b",           re.I), "Bundesminister"),
    (re.compile(r"\bBundesmister\b",            re.I), "Bundesminister"),
    (re.compile(r"\bBundesnister\b",            re.I), "Bundesminister"),
    (re.compile(r"\bBunidesminister\b",         re.I), "Bundesminister"),
    (re.compile(r"\bBundesdesminister\b",       re.I), "Bundesminister"),
    (re.compile(r"\bBundesministerin\s+für\b",  re.I), "Bundesministerin für"),

    # ---- OCR line-break hyphenation --------------------------------------
    # "Finan- zen" → "Finanzen", "Se- nioren" → "Senioren", etc.
    # Only joins when the continuation starts lowercase and is NOT a function word
    # (conjunction or preposition), so "Post- und" and "Minister- für" are excluded.
    (re.compile(
        r"([A-Za-zÄÖÜäöüß])-\s+"
        r"(?!und\b|oder\b|für\b|der\b|des\b|die\b|dem\b|den\b|beim\b|im\b|am\b|zu\b|zur\b|zum\b|von\b|bei\b)"
        r"([a-zäöüß])"
    ), r"\1\2"),
    # Stray trailing hyphen before a preposition: "Bundesminister- für" → "… für"
    (re.compile(r"(\w)-\s+(?=(?:für|der|des|die|beim|im|am|zu|zur|zum)\b)"), r"\1 "),

    # ---- inline punctuation / spacing artefacts --------------------------
    # "i m " → "im "
    (re.compile(r"\bi\s+m\b"),                        "im"),
    # "b e im" / "b und esminister" space-split OCR
    (re.compile(r"\bb\s+u\s*nd\s+es",           re.I), "Bundes"),
    # "undGesundheit" → "und Gesundheit" (missing space before capitalised word)
    (re.compile(r"\bund([A-ZÄÖÜ])"),                  r"und \1"),
    # "unid" / "mid" → "und" (OCR transposition)
    (re.compile(r"\bunid\b",                    re.I), "und"),
    (re.compile(r"\bmid\b"),                          "und"),  # "Landwirtschaft mid Forsten"
    # " - " between words (spurious OCR dash, not a legitimate compound)
    (re.compile(r"(?<=\w) - (?=\w)"),                 " "),
    # Lone digit between words ("Bundesminister 1 des Innern" → "…des Innern")
    (re.compile(r"(?<=\s)\d+\s+(?=[a-zäöüA-ZÄÖÜ])"), ""),
    # Space + comma/apostrophe/backtick before a word
    (re.compile(r"\s+[,'`]\s*(?=[a-zäöüA-ZÄÖÜ])"),   " "),
    # Isolated apostrophe-word like "'für" → "für"
    (re.compile(r"(?<=[räöü])'(?=[a-zäöü])"),         " "),
    # "(" before a lowercase word in the middle ("Bundesminister (des" → "… des")
    (re.compile(r"\s+\(\s*(?=[a-zäöü])"),              " "),
    # Leading "(" before a word
    (re.compile(r"^\(\s*"),                            ""),
    # Leading single punctuation chars
    (re.compile(r"^[,'!.]+\s*"),                       ""),
    # Standalone ")" or "(" surrounded by spaces in the middle
    (re.compile(r"\s+[)(]\s+"),                        " "),
    # Trailing comma, stray ")", asterisk, or isolated hyphen
    (re.compile(r"[,)*]+\s*$"),                        ""),
    (re.compile(r"\s+-\s*$"),                          ""),
    # Trailing period after a word (but not "Dr." or "Parl.")
    (re.compile(r"(?<=[a-zäöüA-ZÄÖÜ])\.\s*$"),        ""),
    # Underscore instead of space
    (re.compile(r"_"),                                 " "),
    # Stray em-dash at start (e.g. "— Der Vorsitzende")
    (re.compile(r"^[—–]\s*"),                          ""),
    # Stray semicolons / plus signs used as punctuation
    (re.compile(r"(?<=\s)[;+](?=\s)"),                 ""),

    # ---- Institution form instead of person form -------------------------
    # "Parl. Staatssekretär beim Bundesministerium für X" →
    # "Parl. Staatssekretär beim Bundesminister für X"
    # "beim" governs persons; "im" governs institutions; mixing them is an OCR error.
    (re.compile(r"\bbeim\s+Bundesministerium\b",   re.I), "beim Bundesminister"),
    # "im Bundesminister des/für X" (person form after "im") →
    # "im Bundesministerium des/für X"
    (re.compile(r"\bim\s+Bundesminister\b(?!ium)", re.I), "im Bundesministerium"),
    # "bei Bundesminister" (missing -m suffix on "beim") → "beim Bundesminister"
    (re.compile(r"\bbei\s+Bundesminister\b",       re.I), "beim Bundesminister"),
    # "im für Europa" — spurious "im" before "für" (OCR split or leakage)
    (re.compile(r"\bim\s+für\b",                   re.I), "für"),

    # ---- "a. D." (außer Dienst) + quoted sentence -----------------------
    # "Bundesminister a. D., hat über eine Jamaika-Koalition ... gesagt"
    # The "a. D." suffix is sometimes legitimate but is always followed by a
    # quoted sentence when the role is contaminated.  Strip everything from
    # " a. D." onwards; the base role ("Bundesminister") remains.
    (re.compile(r"\s+a\.\s*D\b.*",                 re.I | re.DOTALL), ""),

    # ---- Bundesland in square brackets -----------------------------------
    # "Ministerpräsident [Bayern]" / "Ministerpräsident [Hessen] – Zuruf von der SPD"
    (re.compile(r"\s*\[[^\]]*\].*",                re.DOTALL), ""),

    # ---- Quoted speech contamination -------------------------------------
    # Role string bleeds into quoted speech body:
    #   "Staatssekretär im Finanzministerium, hat dazu gesagt"
    #   "Bundesminister Habeck, haben das offen zugegeben"
    # Strip from ", [finite-verb]" onwards.
    (re.compile(
        r",\s+(?:hat\b|haben\b|ist\b|war\b|sind\b|sagt\b|sagte\b|wird\b|wurde\b"
        r"|haben\s+das\b|einfach\s+zu\b)",
        re.I | re.DOTALL,
    ), ""),
    # Speech-text leakage without preceding comma (pdftotext two-column artifact):
    #   "Bundeskanzler Brandt seinerzeit bei der Beratung..." → "Bundeskanzler"
    #   "Bundesminister Habeck das offen zugegeben" → "Bundesminister"
    (re.compile(
        r"\s+(?:seinerzeit\b|damals\b|hat\s+das\s+offen\b|das\s+offen\b).*",
        re.I | re.DOTALL,
    ), ""),
    # Foundation / institution names masquerading as roles:
    # "Bundeskanzler-Willy-Brandt-Stiftung" — hyphenated proper name chain.
    (re.compile(r"-[A-ZÄÖÜ][a-zäöüß]+-[A-ZÄÖÜ][a-zäöüß]+-Stiftung\b.*", re.DOTALL), ""),

    # ---- Additional quoted-speech / ceremonial phrase contamination -------
    # "dazu gesagt" without preceding "hat"
    (re.compile(r",?\s+dazu\s+gesagt\b.*",             re.I | re.DOTALL), ""),
    # "mit Beifall begrüßt" / "von Abg. ... mit Beifall begrüßt"
    (re.compile(r"\s+(?:von\s+Abg\.|mit\s+Beifall\b).*", re.I | re.DOTALL), ""),
    # "zur FDP gewandt" / "an CDU/CSU gewandt" — direction annotations
    (re.compile(r",?\s+(?:zur?|an)\s+\w.*\bgewandt\b.*", re.I | re.DOTALL), ""),
    # "Noch einmal" / "wenn Sie sagen" / "ist" — speech-text fragments
    (re.compile(
        r"\s+(?:noch\s+einmal\b|wenn\s+Sie\s+sagen\b|Ich\s+rufe\b|ist\s*$"
        r"|Frage\s*$)",
        re.I | re.DOTALL,
    ), ""),
    # "dem letzten Jahrhundert wird folgendes Zitat zugeschrieben" — quoted-speech leak
    (re.compile(r"\s+dem\s+letzten\b.*", re.I | re.DOTALL), ""),
    # "Das macht doch gar keinen Sinn..." — speech-text leak mid-role
    (re.compile(r"\s+Das\s+macht\b.*", re.DOTALL), ""),
    # "der Sie und mich... Justiz" — formal "Sie" after preposition marks speech-text start;
    # recover the ministry word if it appears at the end of the leaked fragment
    (re.compile(r"\bder\s+Sie\b.*?(Justiz|Finanzen|Innern|Verteidigung|Wirtschaft)\s*$",
                re.DOTALL), r"der \1"),
    # "der [15+ chars of speech text]...teidigung" — split "Ver-|teidigung" where "Ver"
    # was consumed by the leakage; the trailing "teidigung" identifies the ministry
    (re.compile(r"\bder\s+\w.{15,}?teidigung\s*$", re.DOTALL), "der Verteidigung"),
    # "und [Firstname Lastname]" at end — leaked adjacent-column speaker name
    # Two consecutive title-case words after "und" = person name, not a ministry term
    (re.compile(r"\bund\s+[A-ZÄÖÜ][a-zäöüß]+\s+[A-ZÄÖÜ][a-zäöüß]+\s*$"), ""),

    # ---- "Staatsminister im Auswärtigen [leakage]" normalization ----------
    # All valid occurrences end with "Auswärtigen Amt".  pdftotext regularly
    # inserts right-column content (names, speech sentences, "perma", etc.)
    # between "Auswärtigen" and "Amt" — or omits "Amt" entirely.
    # Rule: keep up to "Auswärtigen" and append " Amt".
    (re.compile(r"(Staatsminister(?:in)?\s+im\s+Auswärtigen).*", re.I | re.DOTALL),
     r"\1 Amt"),

    # ---- Präsident / Vizepräsident name leakage after a role title -------
    # "Bundesminister für Verkehr Präsident Wolfgang Thierse" → "…für Verkehr"
    # Fires when (Vize)?Präsident(in) appears after a word char (not at start).
    (re.compile(
        r"(?<=\w)\s+(?:Vize)?[Pp]räsident(?:in)?\b\s+[A-ZÄÖÜ].*",
        re.DOTALL,
    ), ""),

    # ---- Party leakage in role field -------------------------------------
    # "Parl. Staatssekretär BÜNDNIS 90/DIE GRÜNEN" → "Parl. Staatssekretär"
    # "Parl. Staatssekretär DIE LINKE" → "Parl. Staatssekretär"
    # "Bundesministerin Fraktion der CDU/CSU" → "Bundesministerin"
    (re.compile(
        r"\s+(?:BÜNDNIS\s+90/DIE\s+GRÜNEN|DIE\s+GRÜNEN|DIE\s+LINKE|CDU/CSU|SPD\b"
        r"|Fraktion\s+der\s+\w+|LINKE\b)\s*$",
        re.I,
    ), ""),

    # ---- Bare role title + direct name — 5 simple direct patterns ----------
    # "Bundeskanzler Brandt" → "Bundeskanzler"
    # "Bundesminister Habeck" → "Bundesminister"
    # "Staatsminister Gaddum" → "Staatsminister"
    # "Ministerpräsident Osswald" → "Ministerpräsident"
    # Logic: legitimate role qualifiers always start with a LOWERCASE preposition
    # (für, der, des, beim, im …). A word starting with [A-ZÄÖÜ] followed by a
    # lowercase char is a proper name — strip the name (and any trailing text).
    # This replaces the earlier complex negative-lookahead rule which silently
    # failed due to Python regex interactions with the alternation and anchoring.
    (re.compile(r"^(Bundeskanzler(?:in)?)\s+[A-ZÄÖÜ][a-zäöüß].*$",    re.DOTALL), r"\1"),
    (re.compile(r"^(Bundesminister(?:in)?)\s+[A-ZÄÖÜ][a-zäöüß].*$",   re.DOTALL), r"\1"),
    (re.compile(r"^(Staatsminister(?:in)?)\s+[A-ZÄÖÜ][a-zäöüß].*$",   re.DOTALL), r"\1"),
    (re.compile(r"^(Staatssekretär(?:in)?)\s+[A-ZÄÖÜ][a-zäöüß].*$",   re.DOTALL), r"\1"),
    (re.compile(r"^(Ministerpräsident(?:in)?)\s+[A-ZÄÖÜ][a-zäöüß].*$", re.DOTALL), r"\1"),

    # ---- Double-role deduplication ----------------------------------------
    # The role description sometimes appears TWICE in the matched string:
    #   "Bundesminister für Wirtschaft. Bundesminister für Wirtschaft"
    #   "Bundesminister für X. Dr. Name, Bundesminister für X"
    #   "Bundesminister der Verteidigung von Hassel, Bundesminister der Verteidigung"
    # Strip from the separator (period or comma) that is preceded by 5+ word
    # characters (so "Parl." and "Dr." abbreviations are excluded) up to and
    # including the repeated role keyword plus everything that follows.
    # (?<=\w{5}) is a fixed-length lookbehind (5 chars), safe for re module.
    (re.compile(
        r"(?<=\w{5})[,.]\s+(?:[^:]*?)"
        r"(?:Bundesminister(?:in)?\b|Bundeskanzler(?:in)?\b"
        r"|Staatssekretär(?:in)?\b|Staatsminister(?:in)?\b"
        r"|Vizepräsident(?:in)?\b).*",
        re.DOTALL,
    ), ""),

    # ---- Genitive case normalisation -------------------------------------
    # "des Bundesministerium" (missing genitive -s) → "des Bundesministeriums"
    (re.compile(r"\bdes\s+Bundesministerium\b(?!s)", re.I), "des Bundesministeriums"),

    # ---- OCR split in "wirtschaftliche" ----------------------------------
    (re.compile(r"\bwirtschaft\s+liche\b",             re.I), "wirtschaftliche"),
    (re.compile(r"\bwirtschaftli\s+che\b",             re.I), "wirtschaftliche"),

    # ---- Trailing unclosed parenthetical ---------------------------------
    # "(Niedersachsen", "(Saarland", "(SPD", "(CDU/CSU" etc. at end of string
    # (closing ")" was on the next line and was stripped elsewhere).
    (re.compile(r"\s+\([^)]*$"), ""),

    # ---- Trailing Fragestunde page numbers --------------------------------
    # After the column-letter strip, a bare page number is left stranded:
    #   "Bundesminister 1238"  (from "Bundesminister 1238 A Frage des Abg.")
    #   "Bundesminister . 1279" (from "Bundesminister . 1279 A Frage des Abg.")
    #   "Staatssekretär D, 7584" (column letter + comma precede the number)
    # Step 1: strip any remaining " D," or " D, " before the page number.
    (re.compile(r"\s+[A-D],?\s+\d+\s*$"), ""),
    # Step 2: strip bare trailing page number (2+ digits), optionally preceded
    # by whitespace and/or a single period (the . left from ". [letter] Frage").
    (re.compile(r"[\s.]*\d{2,}\s*$"), ""),

    # ---- Trailing stray period after a space "Bundesminister ." ----------
    (re.compile(r"\s+\.\s*$"), ""),

    # ---- Late-stage backup strips (run after all earlier rules) -------------
    # Duplicate parens-strip: catches "(BMI)", "(AA)" etc. that were NOT at
    # end-of-string when the earlier parens rule ran (because column-marker /
    # dot-leader text still followed them).  After those rules stripped the
    # trailing context, the abbreviation is now at end-of-string — re-strip it.
    (re.compile(r"\s*[\(（][A-ZÄÖÜ]{2,5}[\)）]\s*$"), ""),
    # Trailing column letter with optional comma: " D," / " A " at end
    (re.compile(r"\s+[A-D][,]?\s*$"), ""),
    # Period followed by Präsident/Vizepräsident name leakage
    # "Bundesminister für Wirtschaft. Präsident Wolfgang Thierse" → "…für Wirtschaft"
    (re.compile(r"\.\s+(?:Vize)?[Pp]räsident(?:in)?\b.*", re.DOTALL), ""),
    # Period followed by a discourse-marker / pronoun (speech-text bleed)
    # "Bundesminister für Wirtschaft. Er hat ..." → "Bundesminister für Wirtschaft"
    (re.compile(r"\.\s+(?:[Ee]r\b|[Ii]ch\b|[Jj]a\b|[Ww]ir\b|[Bb]itte,|[Hh]err\b).*$",
                re.DOTALL), ""),
    # Trailing "Dr. Name" bleed appended to an otherwise-clean role:
    # "Bundesminister für Wirtschaft Dr. Müller" → "Bundesminister für Wirtschaft"
    (re.compile(r"(?<=\w)\s+Dr\.\s+[A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?\s*$"),
                ""),
    # "von Abgeordneten ..." procedural leakage
    (re.compile(r"\s+von\s+Abgeordneten\b.*", re.I | re.DOTALL), ""),

    # ---- Further late-stage cleanup ------------------------------------------
    # "beim Bundesminister Bundeskanzler..." without separator → strip second role
    (re.compile(r'(beim\s+Bundesminister)\s+Bundeskanzler\b.*', re.I | re.DOTALL), r'\1'),
    # Trailing conjunction/article at end of role (truncated OCR line)
    (re.compile(r"\s+und\s*$", re.I), ""),
    (re.compile(r"\s+den\s*$", re.I), ""),
    # Person-name leaked at end after role: "...der Verteidigung von Hassel"
    # Excludes hyphenated Bundesland names (Nordrhein-Westfalen, Schleswig-Holstein …)
    (re.compile(r"\s+von\s+(?![A-ZÄÖÜ][a-zäöüß]+-)[A-ZÄÖÜ][a-zäöüß]+\s*$"), ""),
    # Multi-column-letter with trailing comma: " C, D, Lufthansa" → ""
    (re.compile(r"\s+[A-D](?:,\s*[A-D])+,\s*.*", re.DOTALL), ""),

    # ---- Private-use Unicode  (PDF font ligature/space artifact) ------
    # Appears as a stray character in role strings after pdftotext extraction.
    (re.compile(r''), ''),

    # ---- Degree sign ° used as OCR replacement for letters/spaces ----------
    # pdftotext occasionally emits '°' for unrecognised glyph encodings.
    # "im° Bundesministerium" → "im Bundesministerium",  "f°r" → "für"
    (re.compile(r'°'), ''),

    # ---- One-stray-letter "im" prefix variants (OCR misread) ---------------
    # pdftotext inserts one extra letter before "im Bundesm":
    #   "tim Bundesministerium" / "rim Bundesmin" / "sim …" / "lim …"
    # The stray letter is never "e" (which would be "eim" = "beim"), "d", or "n".
    (re.compile(r'\b[trslb]im\s+(?=Bundesmin|Bundes)', re.I), 'im '),

    # ---- "Fe rn" split inside "Fernmeldewesen" ------------------------------
    # pdftotext column boundary splits "Fern" as "Fe rn".
    # Also handles "Fern -melde" and "Fern melde" variants.
    (re.compile(r'\bFe\s+rn\b',              re.I), 'Fern'),
    (re.compile(r'\bFern\s*-\s*(?=[Mm]elde)', re.I), 'Fern'),
    (re.compile(r'\bFern\s+melde',           re.I), 'Fernmelde'),

    # ---- "wirtschaftlich(e/en)" OCR space insertions -----------------------
    # Various column-boundary split positions inside "wirtschaftlich":
    #   "wi rt liche", "wirt schaftliche", "wirtscha ftliche"
    (re.compile(r'\bwi\s+rt\s*(?:lich|schaftlich)', re.I), 'wirtschaftlich'),
    (re.compile(r'\bwirt\s+schaftlich',             re.I), 'wirtschaftlich'),
    (re.compile(r'\bwirtscha\s+ftlich',             re.I), 'wirtschaftlich'),

    # ---- "So zialordnung" OCR split at different position ------------------
    # Complements existing "Sozial ordnung" rule; handles split after "So".
    (re.compile(r'\bSo\s+zial', re.I), 'Sozial'),

    # ---- "Bauund" → "Bau und" (missing space) -------------------------------
    # "Bundesminister für Verkehr, Bauund Wohnungswesen"
    (re.compile(r'\bBauund\b'), 'Bau und'),

    # ---- ",. " stray period after comma before capitalised word ------------
    # "Ernährung,. Landwirtschaft" / "Raumordnung,- Bauwesen" → clean forms.
    (re.compile(r',\s*[.\-]\s*(?=[A-ZÄÖÜ])', re.I), ', '),

    # ---- "L andwirtschaft" (space inside compound) -------------------------
    (re.compile(r'\bL\s+andwirtschaft\b', re.I), 'Landwirtschaft'),

    # ---- "Land-. wirtschaft" / "Land -wirtschaft" variants ----------------
    (re.compile(r'\bLand\s*-\s*[.\s]*[Ww]irtschaft\b'), 'Landwirtschaft'),

    # ---- "nund" → "n und" ---------------------------------------------------
    # OCR merges trailing "n" of a word with "und": "Familien und" → "Familiennund"
    (re.compile(r'\bnund\b'), 'n und'),

    # ---- "des Finanzen" → "der Finanzen" (wrong genitive article) ----------
    # Correct German: "Bundesministeriums der Finanzen" (not "des Finanzen").
    (re.compile(r'\bdes\s+Finanzen\b', re.I), 'der Finanzen'),

    # ---- "innerdeutsche. Beziehungen" stray period --------------------------
    (re.compile(r'\binnerdeutsche\.\s+Beziehungen', re.I), 'innerdeutsche Beziehungen'),
    (re.compile(r'\binnderdeutsche\b', re.I), 'innerdeutsche'),

    # ---- "und Bundesminister(in)" at end of role (dual-role string) --------
    # "Bundesminister des Innern und Bundesminister der Justiz" → "…des Innern"
    (re.compile(r'\s+und\s+Bundesminister(?:in)?\b.*', re.I | re.DOTALL), ''),

    # ---- Bundesland name hyphen canonicalization ---------------------------
    # "Nordrhein Westfalen" / "Baden Württemberg" / "Rheinland Pfalz" →
    # correct hyphenated forms.
    (re.compile(r'\bNordrhein\s+Westfalen\b',  re.I), 'Nordrhein-Westfalen'),
    (re.compile(r'\bBaden\s+Württemberg\b',    re.I), 'Baden-Württemberg'),
    (re.compile(r'\bBaden-Wüttemberg\b',       re.I), 'Baden-Württemberg'),
    (re.compile(r'\bRheinland\s+Pfalz\b',      re.I), 'Rheinland-Pfalz'),

    # ---- Doubled Ministerpräsident at end of string -------------------------
    # "Ministerpräsident des Freistaats Thüringen. Ministerpräsident" → clean
    (re.compile(r'(?<=\w)\.\s*Ministerpräsident(?:in)?\s*$', re.I), ''),

    # ---- Procedural section headers not caught earlier ---------------------
    # "Staatsminister die Tagesordnungspunkte a und b auf" → "Staatsminister"
    (re.compile(
        r'\s+(?:Aktuelle\s+Stunde\b|Unterrichtung\s+durch\s+die\s+Bundesregierung'
        r'|die\s+Tagesordnungspunkte?\s+[a-zA-Z]\s+und\b)',
        re.I | re.DOTALL,
    ), ''),

    # ---- "Bundesminis t er" / "Bundesmin isterium" OCR splits ---------------
    (re.compile(r'\bBundesminis\s+t\s*er\b',  re.I), 'Bundesminister'),
    (re.compile(r'\bBundesmini\s+star\b',     re.I), 'Bundesminister'),
    (re.compile(r'\bBundesmini\s+ser\b',      re.I), 'Bundesminister'),
    (re.compile(r'\bBundesmini\s+er\b',       re.I), 'Bundesminister'),
    (re.compile(r'\bBundesm\s+inister\b',     re.I), 'Bundesminister'),
    (re.compile(r'\bBundes\s+minister\b',     re.I), 'Bundesminister'),
    (re.compile(r'\bBundes\s+kanzler\b',      re.I), 'Bundeskanzler'),
    (re.compile(r'\bBundeskanzler\s+amt\b',   re.I), 'Bundeskanzleramt'),

    # ---- "Bundesschatzmini sterium" / "Bundesschatz ministerium" space -----
    (re.compile(r'\bBundesschatz\s+ministeriums?\b',  re.I), 'Bundesschatzministerium'),
    (re.compile(r'\bBundesmini\s+steriums?\b',        re.I), 'Bundesministeriums'),

    # ---- "BundesministeriumfürX" missing space before preposition ----------
    # "Bundesministeriumfür" → "Bundesministerium für"
    (re.compile(r'\bBundesministeriums?(für|der|des|des\s)', re.I), r'Bundesministerium \1'),
    # "BundesministeriumFürWirtschaft" (CamelCase after no space)
    (re.compile(r'\bBundesministeriums?([A-ZÄÖÜ][a-zäöüß])', re.I), r'Bundesministerium \1'),

    # ---- "Post und Fernmeldewesen" → "Post- und Fernmeldewesen" ------------
    (re.compile(r'\bPost\s+und\s+Fernmeldewesen\b', re.I), 'Post- und Fernmeldewesen'),
    (re.compile(r'\bPost\s+-\s*und\s+Fernmeldewesen\b', re.I), 'Post- und Fernmeldewesen'),

    # ---- Trailing stray prepositions (truncated OCR line) ------------------
    (re.compile(r'\s+für\s*$', re.I), ''),
    (re.compile(r'\s+der\s*$', re.I), ''),
    (re.compile(r'\s+von\s*$', re.I), ''),

    # ---- "desminister" isolated (partial word leakage) ---------------------
    # "beim desminister des Innern" — the "Bundes" was eaten by column leakage.
    (re.compile(r'\bdesminister\b', re.I), 'Bundesminister'),

    # ---- "Wirt -schaft" hyphen variant (not caught by existing space rule) --
    (re.compile(r'\bWirt\s*-\s*[Ss]chaft\b', re.I), 'Wirtschaft'),

    # ---- "wirtschaft lichen" genitive/dative form split --------------------
    (re.compile(r'\bwirtschaft\s+lichen\b',    re.I), 'wirtschaftlichen'),
    (re.compile(r'\bwirtschaftli\s+chen\b',    re.I), 'wirtschaftlichen'),

    # ---- "Aus wärtigen" → "Auswärtigen" (split at different position) ------
    (re.compile(r'\bAus\s+wärtigen\b',         re.I), 'Auswärtigen'),

    # ---- "und. Forsten" / "und. Landwirtschaft" — stray period after "und" -
    (re.compile(r'\bund\.\s+(?=[A-ZÄÖÜ])',     re.I), 'und '),

    # ---- "Bundesrates" / "Bundesrats" → canonical "Bundesrates" -----------
    # "des Bundesrats" = colloquial; "des Bundesrates" = official German spelling.
    (re.compile(r'\bdes\s+Bundesrats\b',       re.I), 'des Bundesrates'),

    # ---- "Bundersegierung" OCR typo → "Bundesregierung" -------------------
    (re.compile(r'\bBundersegierung\b',        re.I), 'Bundesregierung'),

    # ---- "Parl. Staatssekretärin beim Bundesminister Wirtschaft" -----------
    # Missing "für" between role title and ministry name.
    # Fires only when after "[P/p]arl. Staatssekretär(in) beim Bundesminister(in)"
    # the next non-article word is a capitalised ministry keyword (not a person name):
    # "Bundesminister Wirtschaft und Technologie" → "Bundesminister für Wirtschaft…"
    (re.compile(
        r'((?:Parl\.\s+)?Staatssekretär(?:in)?\s+beim\s+Bundesminister(?:in)?)\s+'
        r'(?=(?:Wirtschaft|Finanzen|Verkehr|Innern|Justiz|Verteidigung|Ernährung'
        r'|Forschung|Gesundheit|Bildung|Familie|Jugend|Arbeit|Umwelt|Post|Wohnen'
        r'|Raumordnung|Städtebau|Landwirtschaft|Digitales|Angelegenheiten)\b)',
        re.I,
    ), r'\1 für '),

    # ---- Verkehr-, Bau- hyphen canonicalization -----------------------------
    # "für Verkehr-, Bau- und Wohnungswesen" → "für Verkehr, Bau- und Wohnungswesen"
    (re.compile(r'\bVerkehr-,\s*Bau-\s+und\b', re.I), 'Verkehr, Bau- und'),
    (re.compile(r'\bVerkehr-,\s*Bau\s+und\b',  re.I), 'Verkehr, Bau- und'),

    # =========================================================================
    # ---- Round 6 normalization (May 2026) -----------------------------------
    # =========================================================================

    # ---- "der Innern" → "des Innern" (wrong article) ------------------------
    (re.compile(r'\bder\s+(?=Innern\b)', re.I), 'des '),

    # ---- Canonicalize "des Auswärtigen Amts" → "des Auswärtigen Amtes" -----
    # Also recover bare "des Auswärtigen" where "Amt" was truncated/dropped.
    (re.compile(r'\bdes\s+Auswärtigen\s+Amts\b',  re.I), 'des Auswärtigen Amtes'),
    (re.compile(r'\bdes\s+Auswärtigen\s*$',        re.I), 'des Auswärtigen Amtes'),

    # ---- "Gesuncl" → "Gesund" (OCR: cl = misread of d) ---------------------
    (re.compile(r'\bGesuncl', re.I), 'Gesund'),

    # ---- "Sozialondnung" → "Sozialordnung" (on/or transposition) ------------
    (re.compile(r'\bSozialondnung\b', re.I), 'Sozialordnung'),

    # ---- "Sozialord" truncated / with leakage → "Sozialordnung" -------------
    # Bare stub at end of string (no "nung" suffix).
    (re.compile(r'\bSozialord\s*$', re.I), 'Sozialordnung'),
    # Non-"nung" continuation (e.g. "Sozialordabzubauen", "Sozialord(Andres").
    (re.compile(r'\bSozialord(?!nung\b)\S+', re.I), 'Sozialordnung'),
    # Speech text interleaved between "Sozial" and "ordnung" by two-column OCR.
    # "SozialNun zur Sache ... hat ordnung" → "Sozialordnung"
    (re.compile(r'\bSozial[A-ZÄÖÜ].*?ordnung\b', re.DOTALL), 'Sozialordnung'),
    # Trailing comma + garbage left after the above repairs.
    (re.compile(r'(\bSozialordnung)\s*[,;]\s+(?!und\b|der\b|des\b).*',
                re.DOTALL | re.I), r'\1'),

    # ---- "Familien und Jugendfragen" → "Familien- und Jugendfragen" ---------
    # The official ministry name uses a hyphenated compound.
    (re.compile(r'\bFamilien\s+und\s+(?=Jugendfragen\b)', re.I), 'Familien- und '),

    # ---- "Arbeit[s]- und Sozialordnung" → "Arbeit und Sozialordnung" --------
    # The official name never used a hyphenated compound; both "Arbeit-" and
    # "Arbeits-" are OCR / typographic artefacts of the period.
    (re.compile(r'\bArbeits?-\s+und\s+Sozialordnung\b', re.I),
     'Arbeit und Sozialordnung'),

    # ---- Fragestunde TOC "letter, Frage" re-run with DOTALL -----------------
    # Re-applies the key Fragestunde-header strip with DOTALL so embedded
    # newlines (from Pattern-C multi-line matching) don't prevent .* from
    # consuming the full contaminated tail.
    # "Bundesminister . C, Frage des Abg. X" → "Bundesminister"
    # "Bundesminister D, Frage des Abg. X"   → "Bundesminister"
    (re.compile(r'\s+\.\s+[A-D],?\s*Frage\b.*', re.DOTALL | re.I), ''),
    (re.compile(r'(?<=\s)[A-D],\s+Frage\b.*',   re.DOTALL | re.I), ''),

    # ---- Final-pass trailing period (may survive late-stage rules) -----------
    (re.compile(r'\s+\.\s*$'), ''),

    # =========================================================================
    # ---- Round 7 normalization (May 2026) -----------------------------------
    # =========================================================================

    # ---- C1: "Bundesministerium" OCR variants not caught by earlier rules ----
    # Each pattern uses a capturing group for the optional genitive "s" so
    # that "des Bundesministeriums" stays grammatically correct after the fix.

    # "rn" → "m" classic OCR confusion:  "Bundesrninisteriums" → "…iums"
    (re.compile(r'\bBundesrninisterium(s?)\b', re.I), r'Bundesministerium\1'),

    # Doubled "ri" inside the stem:  "Bundesministeririums" → "…iums"
    (re.compile(r'\bBundesministeriri(um(?:s?))\b', re.I), r'Bundesministeri\1'),

    # "m" → "n" transposition in the first syllable, with space-split:
    # "Bundesmimiste riums" → "Bundesministeriums"
    (re.compile(r'\bBundesmimiste\s+rium(s?)\b', re.I), r'Bundesministerium\1'),

    # Same transposition without the space:
    # "Bundesmimisteriums" → "Bundesministeriums"
    (re.compile(r'\bBundesmimisterium(s?)\b', re.I), r'Bundesministerium\1'),

    # Doubled "n" inside the stem:  "Bundesminnisteriums" → "…iums"
    (re.compile(r'\bBundesminnisterium(s?)\b', re.I), r'Bundesministerium\1'),

    # Extra "e" inside the stem:  "Bundesminiesteriums" → "…iums"
    (re.compile(r'\bBundesminiesterium(s?)\b', re.I), r'Bundesministerium\1'),

    # ---- C2: Stray characters not yet handled --------------------------------

    # Apostrophe inside the article "de's" → "des":
    # "Staatssekretär de's Bundesministeriums" → "… des Bundesministeriums"
    (re.compile(r"\bde's\b"), 'des'),

    # Apostrophe after "Bundesministerium" before an article:
    # "im Bundesministerium' der Finanzen" → "im Bundesministerium der Finanzen"
    # (the earlier rule only covers "Bundesminister'" and "Bundeskanzler'")
    (re.compile(r'\bBundesministerium\s*\'\s*', re.I), 'Bundesministerium '),

    # Space-period-space before an article or preposition:
    # "im Bundesministerium . des Innern" → "im Bundesministerium des Innern"
    # (earlier Case 2 rule handles "." attached to a word, not isolated ". ")
    (re.compile(r'\s+\.\s+(?=(?:der|des|für|im|beim|am|zu|zur|von)\b)', re.I), ' '),

    # Stray "+" directly before "im":
    # "Staatssekretär +im Bundesministerium" → "Staatssekretär im Bundesministerium"
    # (earlier rule handles "+" surrounded by spaces; "+im" has no space between)
    (re.compile(r'\+\s*(?=im\s+)', re.I), ''),

    # Stray single letter between "Bundesministerium" and article (column markers
    # like "G" / "I" that were not stripped by the (B)/(D) rule because they lack
    # parens — e.g. "im Bundesministerium G für Verkehr", "…rium I des Innern"):
    (re.compile(r'\bBundesministerium\s+[A-Z]\s+(?=(?:für|der|des|im|beim)\b)', re.I),
     'Bundesministerium '),

    # ---- C3: "Auswärtigen" declension / transposition fixes ------------------

    # "im Auswärtige Amt" — wrong adjective declension (should be "Auswärtigen"):
    (re.compile(r'\bim\s+Auswärtige\s+Amt\b', re.I), 'im Auswärtigen Amt'),

    # "Amsw" → "Ausw" OCR transposition (a/u and m/s swapped simultaneously):
    # "Staatssekretär des Amsw ärtigen Amts" → "… des Auswärtigen Amts"
    (re.compile(r'\bAmsw', re.I), 'Ausw'),
    # After the above substitution "Ausw ärtigen" may have a stranded space;
    # close it.  (Existing rules handle "Auswä rtigen" and "Aus wärtigen" but
    # not the "Ausw ärtigen" split position.)
    (re.compile(r'\bAusw\s+ärtigen\b', re.I), 'Auswärtigen'),
    # Re-run Amts→Amtes and bare-Auswärtigen fixes that fired before Round 7:
    (re.compile(r'\bdes\s+Auswärtigen\s+Amts\b',  re.I), 'des Auswärtigen Amtes'),
    (re.compile(r'\bdes\s+Auswärtigen\s*$',        re.I), 'des Auswärtigen Amtes'),

    # ---- C4: Specific garbled-entry patterns ---------------------------------

    # "gesagt" at end of role string — speech text appended after Bundesland:
    # "Ministerpräsident des Saarlandes gesagt" → "Ministerpräsident des Saarlandes"
    # Only fires at end-of-string (never a legitimate part of a role title).
    (re.compile(r'\s+gesagt\b.*$', re.I | re.DOTALL), ''),

    # "Bundesschatzmini sterium(s)" — space-split form not caught by earlier
    # rules; capturing group preserves the genitive "s" where present.
    (re.compile(r'\bBundesschatzmini\s+sterium(s?)\b', re.I), r'Bundesschatzministerium\1'),

    # "Staatssekretär im Bundesral..." — "Bundesral" is "Bundesrat" + column
    # leakage; the ministry is irrecoverable, reduce to bare base title:
    (re.compile(r'\bim\s+Bundesral\b.*', re.I | re.DOTALL), ''),

    # "im Bundesusw." / "im Bundesusw," — "usw." (= etc.) leaked as part of
    # "Bundes" continuation; irrecoverable ministry, strip tail:
    (re.compile(r'\bim\s+Bundesusw\b.*', re.I | re.DOTALL), ''),

    # ---- Final-pass trailing period (may survive late-stage rules) -----------
    (re.compile(r'\s+\.\s*$'), ''),

    # Collapse multiple spaces
    (re.compile(r"  +"),                               " "),
]

# Dash-continuation: "— Capital..." marking the presiding officer resuming.
# Two trigger contexts (both exclusive to WP01-era full-inline sessions):
#   1. Blank-line paragraph break:  \n\n— Capital
#   2. Immediately after a closing interjection:  )\n— Capital
# Mid-sentence dashes never open a paragraph; interjections always close with ")".
RE_DASH_CHAIR = re.compile(r"(?:\n\n|\)\n)(\s*—\s+[A-ZÄÖÜ])", re.MULTILINE)

# Session ID from filename: "WP20_Session010_cleaned.txt" → "WP20_Session010".
RE_SESSION_ID = re.compile(r"(WP\d+_Session\d+)")


# Qualifier classifier

def _classify_qualifiers(qualifiers: list, party_first: bool = False) -> tuple:
    """
    Classify parenthetical qualifier strings into (party, location).

    Rules (in order):
    1. Skip Drucksache / Frage reference strings.
    2. Known Bundesland → location.
    3. Known party abbreviation → party (slash-spacing is normalised before lookup,
       so "CDU / CSU" matches the same as "CDU/CSU").
    4. First remaining ambiguous qualifier → location (city / constituency),
       OR → party when party_first=True (used for Pattern D).
    5. Second remaining ambiguous qualifier → party (or location if party_first).

    Args:
        qualifiers:  list of raw qualifier strings extracted from parentheses.
        party_first: if True, the first ambiguous qualifier is treated as party
                     rather than location.  Use for Pattern D attributions
                     (Berichterstatter / Anfragender) where the single qualifier
                     in parentheses is always the party affiliation, never a
                     Bundesland.

    Returns (party, location) — either may be None.
    """
    party = None
    location = None
    for q in qualifiers:
        q = q.strip().lstrip("('")  # leading ( or ' are OCR bracket artefacts
        q = q.replace('­', '-')  # soft hyphen → regular hyphen for dehyphen patterns
        q = q.strip()
        if not q:
            continue
        if RE_DRUCKSACHE.match(q):
            continue  # parliamentary reference — ignore
        if re.search(r'[Dd]r.{0,3}cks|Frage\s+[IVXB]?\d', q, re.IGNORECASE):
            continue  # broken Drucksache OCR variants: "Druck- sache", "Dr ucksache"
        if RE_BEIFALL.search(q) or re.search(r'\bbeifall\b|\bbegr', q, re.IGNORECASE):
            continue  # applause annotation (encoding-tolerant check)
        if RE_PARTY_STAGE_DIRECTION.match(q):
            continue  # "vom Platz aus sprechend", "auf den Abg. X zeigend"
        if re.search(r'[Dd]olmet|[Gg]ebärden', q, re.IGNORECASE):
            continue  # Gebärdensprachdolmetschung (compound word — no \b boundary before dolmet)
        if re.search(r'\bZuruf\b|\bSitzung\b|\bAnlage\b|\bFragestunde\b', q, re.IGNORECASE):
            continue  # parliamentary procedure annotations
        if q in _BUNDESLAENDER:
            location = q
        else:
            # Normalise slash spacing, apply dehyphenation and party-name
            # normalisation so "BÜNDNIS 90/DIE GRÜ-NEN" → "BÜNDNIS 90/DIE GRÜNEN"
            # before the _KNOWN_PARTIES lookup (fixes OCR line-break hyphen artefacts).
            q_norm = re.sub(r'\s*/\s*', '/', q)
            for pat, fix in _PARTY_DEHYPHEN:
                q_norm = pat.sub(fix, q_norm)
            q_norm = _PARTY_NORMALISE.get(q_norm, q_norm)
            if q_norm in _KNOWN_PARTIES:
                party = q_norm
            elif party_first:
                if party is None:
                    party = q   # first ambiguous → party (Pattern D)
                else:
                    location = q  # second ambiguous → location
            else:
                if location is None:
                    location = q  # first ambiguous → location (e.g. city)
                else:
                    party = q     # second ambiguous → party
    return party, location


# Speaker name corrections — OCR / font-encoding errors in WP01–WP03
# Maps known misspelled names (as they appear in the raw PDF attribution line)
# to their correct form. All entries verified by cross-checking against
# correctly-rendered references in body text or adjacent sessions.
# Discovered via systematic scan of all unique speaker names in WP01–WP03
# (April 2026). The dominant source is the bold attribution-header typeface
# in early scanned PDFs encoding certain glyphs differently from body text.

NAME_CORRECTIONS: dict[str, str] = {
    # a → ö (WP01 bold header font encodes ö as a)
    "Labe":                   "Löbe",
    "Dr. Barsch":             "Dr. Börsch",
    "Dr. Schone":             "Dr. Schöne",
    "Hocker":                 "Höcker",
    "Hoecker":                "Höcker",
    "Schmucker":              "Schmücker",
    # oe → ö
    "Schoettle":              "Schöttle",
    "Schoette":               "Schöttle",
    "Schoettie":              "Schöttle",
    "Schottle":               "Schöttle",
    "Koenen":                 "Könen",
    "Goetzendorff":           "Götzendorff",
    "Frau Schroeder":         "Frau Schröder",
    "Dr. Moerchel":           "Dr. Mörchel",
    "Schoenberg":             "Schönberg",
    # ue → ü
    "Dr. Wuermeling":         "Dr. Würmeling",
    "Dr. Wuermelirg":         "Dr. Würmeling",
    "Dr. Luetkens":           "Dr. Lütkens",
    "Dr. Leverkuehn":         "Dr. Leverkühn",
    # ae → ä
    "Maerkl":                 "Märkl",
    "Draeger":                "Dräger",
    # u / i → ü (OCR glyph confusion)
    "Hubner":                 "Hübner",
    "Rummele":                "Rümmele",
    "KUhn":                   "Kühn",
    "Kiihlthau":              "Kühlthau",
    # ss → ß
    "Strauss":                "Strauß",
    "Sassnick":               "Saßnick",
    "Frau Dr. Steinbiss":     "Frau Dr. Steinbiß",
    # Other OCR corruptions
    "Dr. Schafer":            "Dr. Schäfer",
    "Millier":                "Müller",
    "Milller-Hermann":        "Müller-Hermann",
    "MellIes":                "Mellies",
    "Meilies":                "Mellies",
    "Mollies":                "Mellies",
    "DraChsler":              "Drachsler",
}


def _correct_raw_name(speaker_raw: str) -> str:
    """
    Apply NAME_CORRECTIONS to a raw speaker attribution string.

    Called before parsing so that both speaker_raw and all derived fields
    (name, role, party) reflect the corrected form.  Only the first matching
    correction is applied (names in the dict are mutually exclusive).
    """
    for wrong, right in NAME_CORRECTIONS.items():
        if wrong in speaker_raw:
            return speaker_raw.replace(wrong, right)
    return speaker_raw


# Speech body cleaner

# Matches text that looks like an interjection fragment (party names, reaction words)
_RE_INTERJECTION_MARKER = re.compile(
    r"\b(?:SPD|CDU|CSU|FDP|GRÜNEN?|Grünen?|Linke|AfD|PDS|DP|KPD|SRP|GB|BHE|WAV|SSW"
    r"|Beifall|Heiterkeit|Widerspruch|Lachen|Bravo|Oh|Ah|Zuruf|Zurufe"
    r"|Abgeordneten|Fraktion|Koalition)\b",
    re.IGNORECASE,
)


def _is_orphan_tail(paragraph: str) -> bool:
    """Return True if paragraph is an orphan interjection tail.

    Two-column interleaving can split a parenthetical across the column
    boundary so that the opening '(' is dropped and only the closing ')'
    survives.  Such a fragment ends with ')' but has no '(' and contains
    a known interjection keyword.
    """
    s = paragraph.rstrip()
    return (
        s.endswith(")")
        and "(" not in s
        and bool(_RE_INTERJECTION_MARKER.search(s))
    )


@functools.lru_cache(maxsize=4096)
def _compile_running_header_patterns(
    name: str,
) -> tuple:
    """Return (pat_mid, pat_start, pat_hyph) compiled regex patterns for *name*.

    Cached so that the same speaker's patterns are compiled only once across
    the entire corpus (~1 M speeches, but only ~10 k unique speaker names).

    Three patterns are returned:
      pat_mid   — standalone name paragraph in the middle of text (\n\n...\n)
      pat_start — name at the very start of text
      pat_hyph  — name fused after a hyphen (b_01 hyphen-rejoin artefact):
                  e.g. "vorge-Alterspräsident Dr. X\nschlagen" where the
                  column-continuation label was absorbed by b_01's uppercase-
                  continuation rule (keeps hyphen, drops newline), embedding
                  the header mid-word.

    Each pattern allows an optional parliamentary role prefix (Alterspräsident,
    Vizepräsident, Präsident, Bundeskanzler, Bundesminister, Staatsminister)
    followed by an optional academic title prefix (Dr. / Prof. Dr.).  This is
    needed because Bundestag PDFs print the presiding officer's name as
    "{Role} {Name}" in the column-continuation Kolumnentitel.
    """
    escaped = re.escape(name)
    # Optional parliamentary role prefix that appears in PDF running headers.
    # Covers presiding officers (Alters/Vize/Bundes?präsident) and government
    # speakers (Bundeskanzler, Bundesminister, Staatsminister).
    role  = (
        r"(?:"
        r"(?:Alters|Vize|Bundes)?präsident(?:in)?"
        r"|Bundeskanzler(?:in)?"
        r"|Bundesminister(?:in)?"
        r"|Staatsminister(?:in)?"
        r"|Parl\.\s+Staatssekretär(?:in)?"   # Parlamentarischer Staatssekretär/in
        r")\s+"
    )
    # Optional academic / honorific title prefix.
    title = r"(?:(?:Prof\.\s+)?Dr\.\s+(?:h\.\s*c\.\s+)?)?"
    # Combined optional prefix: role (optional) then title (optional).
    prefix = rf"(?:{role})?{title}"

    # re.IGNORECASE is needed because the Kolumnentitel uses "Präsident" with
    # a capital P, while the role-prefix branch of the pattern has lowercase
    # "präsident" (the Alters/Vize/Bundes alternation implicitly lowercases it).
    # Without IGNORECASE, standalone "Präsident" / "Präsidentin" roles would
    # never match, leaving those running headers unstripped.
    flags = re.MULTILINE | re.IGNORECASE
    pat_mid   = re.compile(rf"\n\n{prefix}{escaped}\n", flags)
    pat_start = re.compile(rf"^{prefix}{escaped}\n",    flags)
    pat_hyph  = re.compile(rf"-{prefix}{escaped}\n",    flags)
    return pat_mid, pat_start, pat_hyph


def _strip_running_header(text: str, name: str) -> str:
    """
    Strip speaker-name column-continuation headers from speech body text.

    In two-column Bundestag PDFs (WP10+), the speaker's name (sometimes
    preceded by their role title) is printed at the top of each continuation
    column/page as a navigation label (Kolumnentitel).  pdftotext embeds
    these labels in the extracted speech text in three ways:

    Case 1 — standalone paragraph mid-text:
        "...nationaler Tragweite festgestellt\\n\\n"
        "Karin Maag\\n"                      ← stripped, blank line preserved
        "im November beschlossen."

    Case 2 — at the very start of the speech body:
        "Alterspräsident Dr. Heinz Riesenhuber\\n"   ← stripped entirely
        "Guten Morgen, meine Damen und Herren!"

    Case 3 — fused mid-word after a hyphen (b_01 hyphen-rejoin artefact):
        b_01 sees "vorge-\\nAlterspräsident Dr. X\\nschlagen" and applies
        the uppercase-continuation rule (keep hyphen, drop newline), giving:
        "vorge-Alterspräsident Dr. X\\nschlagen worden"
        → stripped to "vorge-\\nschlagen worden"
        → lowercase continuation re-joined to "vorgeschlagen worden"
    """
    if not name:
        return text
    pat_mid, pat_start, pat_hyph = _compile_running_header_patterns(name)
    # Case 1: standalone paragraph → collapse to a single blank-line separator
    text = pat_mid.sub("\n\n", text)
    # Case 2: name at very start → remove entirely
    text = pat_start.sub("", text, count=1)
    # Case 3: fused after hyphen → restore the bare hyphen, then re-join
    # b_01 already joined all real lowercase hyphen-continuations, so any
    # "word-\nlower" left after this substitution was created here and is safe
    # to rejoin without risk of touching legitimate hyphens.
    if pat_hyph.search(text):
        text = pat_hyph.sub("-\n", text)
        text = re.sub(r'-\n([a-zäöüß])', r'\1', text)
    return text


def _strip_interjections(text: str) -> str:
    """
    Remove all parenthetical interjections from a speech body string.

    Every parenthetical in the Bundestag speech body is an audience reaction
    (applause, heckling, laughter, etc.), never the speaker's own words.
    Two-column PDF layouts frequently cause these to be interleaved at the
    wrong position in the text (Option A); stripping them aggressively also
    eliminates correctly-placed ones (Option B), giving clean speech text.

    Pass 1 — strip complete parentheticals via RE_PARENTHETICAL.
    Pass 2 — strip orphan tails: fragments ending with ')' that have no
              matching '(' (the opening paren was lost at a column boundary).

    After removal, excess blank lines and whitespace runs are collapsed.
    """
    # Pass 1: strip complete parentheticals iteratively.
    # RE_PARENTHETICAL matches only non-nested "(...)".  Looping until stable
    # handles one level of nesting, e.g. "(Name (Party): text)" is removed in
    # two passes.  O(n) per pass — no backtracking possible.
    prev = None
    cleaned = text
    while cleaned != prev:
        prev = cleaned
        cleaned = RE_PARENTHETICAL.sub("", cleaned)
    # Pass 2: strip orphan interjection tails paragraph-by-paragraph
    paragraphs = re.split(r"\n\n", cleaned)
    paragraphs = [p for p in paragraphs if not _is_orphan_tail(p.strip())]
    cleaned = "\n\n".join(paragraphs)
    # Collapse whitespace artefacts left by removal
    cleaned = re.sub(r"[ \t]+", " ", cleaned)          # multiple spaces → one
    cleaned = re.sub(r" *\n", "\n", cleaned)            # trailing spaces on lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)        # 3+ blank lines → one
    return cleaned.strip()


# Post-extraction field cleaner

def _clean_parsed(d: dict) -> dict:
    """
    Remove OCR noise from the role and party fields of a parsed speaker dict.

    Role:
      - Strip trailing beifall (applause) parentheticals, e.g.
        "Bundeskanzler (von der SPD mit Beifall begrüßt)" → "Bundeskanzler".

    Party:
      - Strip a leading "(" left over from an unclosed parenthesis in the OCR.
      - Discard Drucksache / Tagesordnungspunkt references that leaked in.
      - Discard any value that still contains "begrüßt" (applause annotation).
      - Discard suspiciously long values (> 80 chars) which are parse artefacts.
      - Fix common hyphen-break artefacts, e.g. "GRÜ-NEN" → "GRÜNEN".

    Name:
      - Apply NAME_CORRECTIONS to fix known OCR/font-encoding errors in
        WP01–WP03 attribution headers (e.g. "Labe" → "Löbe").
    """
    # --- Role ---
    if d.get("role"):
        cleaned = RE_BEIFALL.sub("", d["role"]).strip()
        if cleaned:
            # Normalise Unicode whitespace variants before regex rules run.
            # Python re \s does NOT match Unicode spaces that PDFs emit:
            # NBSP ( ), narrow no-break ( ), thin ( ),
            # figure ( ), en ( ), em ( ).  Convert all to
            # ASCII space.  Invisible chars (soft hyphen ­, zero-width
            # space ​) are discarded.
            cleaned = (cleaned
                .replace(' ',   ' ')   # NBSP
                .replace(' ', ' ')   # narrow no-break space
                .replace(' ', ' ')   # thin space
                .replace(' ', ' ')   # figure space
                .replace(' ', ' ')   # en space
                .replace(' ', ' ')   # em space
                .replace('­',   '')    # soft hyphen
                .replace('​', '')    # zero-width space
            )
            for pat, fix in _ROLE_OCR_FIXES:
                cleaned = pat.sub(fix, cleaned)
            cleaned = cleaned.strip()
        d["role"] = cleaned or None

    # --- Party ---
    party = d.get("party")
    if party:
        party = party.lstrip("(").strip()
        if not party:
            party = None
        elif RE_PARTY_GARBAGE.search(party):
            party = None
        elif "begrüßt" in party.lower():
            party = None
        elif len(party) > 80:
            # Suspiciously long — almost certainly a hyphen-leakage artefact
            party = None
        elif RE_PARTY_EMBEDDED_NAME.match(party):
            # e.g. "BÜNDNIS 90/DIE GRÜ-<speaker text>-NEN" — two-column OCR leak
            party = "BÜNDNIS 90/DIE GRÜNEN"
        elif re.match(r"^BÜNDNIS\s*90/DIE\s*GRÜ", party, re.I) and party != "BÜNDNIS 90/DIE GRÜNEN":
            # Catch remaining BÜNDNIS artifacts not handled above:
            # "GRÜ- NEN" (space after hyphen), "GRÜ-Ute Kumpf (SPD" (no -NEN ending),
            # "GRÜMeine Damen...NEN" (no hyphen, speech text leaked in)
            party = "BÜNDNIS 90/DIE GRÜNEN"
        elif RE_PARTY_STAGE_DIRECTION.match(party):
            # Stage directions leaking into party field
            party = None
        elif RE_PARTY_GUEST.match(party):
            # Guest/observer annotations — not a real party membership
            party = None
        elif party in _CITY_NOT_PARTY:
            # City names mis-assigned as party (double-location qualifier)
            party = None
        else:
            # Normalise spaces around "/" so "CDU / CSU" → "CDU/CSU" before lookup
            party = re.sub(r'\s*/\s*', '/', party)
            for pat, fix in _PARTY_DEHYPHEN:
                party = pat.sub(fix, party)
            party = _PARTY_NORMALISE.get(party, party)
        d["party"] = party

    # --- Name: strip beifall, spurious prefixes, then OCR corrections ---
    name = d.get("name")
    if name:
        name = RE_BEIFALL.sub("", name).strip()
        # Strip "Abgeordnete/n/r" prefix variants (salutation absorbed into name)
        stripped = _RE_NAME_ABGEORDNETE.sub("", name).strip()
        # Strip leading function-word leakage ("Aber auch X" → "X")
        stripped = _RE_NAME_WORD_PREFIX.sub("", stripped).strip()
        if stripped:          # only apply if result is non-empty
            name = stripped   # "Abgeordneten" alone → keep original
        name = NAME_CORRECTIONS.get(name, name)
        d["name"] = name or d.get("name")

    return d


# Speaker line parser

def parse_speaker(raw: str) -> dict:
    """
    Break a raw speaker attribution string (colon already stripped) into
    structured fields: name, role, party, location.

    Returns a dict with keys: name, role, party, location.
    All values may be None if not determinable.
    """
    text = _ws(raw.rstrip(":"))

    # Pattern A: chair role prefix
    m = RE_CHAIR_PREFIX.match(text)
    if m:
        return {
            "name": _ws(m.group(2)),
            "role": m.group(1),
            "party": None,
            "location": None,
        }

    # Pattern D: name + qualifier(s) + comma + parliamentary procedural role
    # e.g. "Dr. Kather (GB/BHE), Anfragender"
    # The single qualifier in Pattern D is always the party affiliation —
    # Berichterstatter/Anfragender attributions never carry a Bundesland.
    # Use party_first=True so an unrecognised abbreviation (e.g. "FDP-Hosp.",
    # "FU", "BHE-DG") resolves to party rather than falling through to location.
    m = RE_MEMBER_ROLE_SPLIT.match(text)
    if m:
        name_part = _ws(m.group(1))
        qualifiers = RE_PAREN_QUALIFIER.findall(m.group(2))
        role_part = _ws(m.group(3))
        party, location = _classify_qualifiers(qualifiers, party_first=True)
        return {
            "name": name_part,
            "role": role_part,
            "party": party,
            "location": location,
        }

    # Pattern C: name + comma + official role
    m = RE_OFFICIAL_SPLIT.match(text)
    if m:
        name_part = _ws(m.group(1))
        role_part = _ws(m.group(2) + m.group(3))
        # An optional parenthetical may follow the role keyword — usually a
        # Bundesland, but OCR artefacts can inject applause annotations, party
        # names, Drucksache refs, or sign-language notes here instead.
        location = None
        party_c  = None
        state_m  = RE_PAREN_QUALIFIER.search(m.group(3))
        if state_m:
            cand = state_m.group(1).strip().lstrip("('")
            cand = cand.replace('­', '-').strip()  # soft hyphen → regular hyphen
            if not cand:
                pass
            elif (re.search(r'\bbeifall\b|\bbegr', cand, re.IGNORECASE)
                    or RE_PARTY_STAGE_DIRECTION.match(cand)
                    or RE_DRUCKSACHE.match(cand)
                    or re.search(r'[Dd]r.{0,3}cks|Frage\s+[IVXB]?\d', cand, re.IGNORECASE)
                    or re.search(r'[Dd]olmet|[Gg]ebärden', cand, re.IGNORECASE)
                    or re.search(r'\bZuruf\b|\bSitzung\b|\bAnlage\b|\bFragestunde\b|\bSeite\b', cand, re.IGNORECASE)):
                pass  # discard — not a real location
            elif cand in _BUNDESLAENDER:
                location = cand
            else:
                # Try party normalisation (e.g. minister from BÜNDNIS 90/DIE GRÜNEN)
                cand_norm = re.sub(r'\s*/\s*', '/', cand)
                for pat, fix in _PARTY_DEHYPHEN:
                    cand_norm = pat.sub(fix, cand_norm)
                cand_norm = _PARTY_NORMALISE.get(cand_norm, cand_norm)
                if cand_norm in _KNOWN_PARTIES:
                    party_c = cand_norm
                elif not RE_PARTY_GUEST.match(cand):
                    location = cand  # treat as city/constituency
        return {
            "name": name_part,
            "role": role_part,
            "party": party_c,
            "location": location,
        }

    # Pattern B: name + parenthesised qualifiers.
    # Split at the first "(" to separate base name from qualifiers.
    paren_pos = text.find("(")
    if paren_pos != -1:
        base_name = _ws(text[:paren_pos])
        qualifiers = RE_PAREN_QUALIFIER.findall(text[paren_pos:])
        party, location = _classify_qualifiers(qualifiers)

        # Check if base_name starts with a state-level role prefix
        # e.g. "Ministerpräsident Oskar Lafontaine" or "Regierender Bürgermeister Diepgen"
        role_m = RE_STATE_ROLE_PREFIX.match(base_name)
        if role_m:
            return {
                "name": _ws(role_m.group(2)),
                "role": role_m.group(1),
                "party": party,
                "location": location,
            }

        return {
            "name": base_name,
            "role": None,
            "party": party,
            "location": location,
        }

    # Fallback: treat the whole string as the name.
    return {
        "name": _ws(text),
        "role": None,
        "party": None,
        "location": None,
    }


# OCR umlaut substitution pairs — common OCR error: umlaut read as plain vowel.
_UMLAUT_PLAIN = str.maketrans("äöüÄÖÜ", "aouAOU")


def _umlaut_key(name: str) -> str:
    """Return a fold key for umlaut-insensitive comparison."""
    return name.lower().translate(_UMLAUT_PLAIN)


# Intra-session name normalization

def _levenshtein(a: str, b: str) -> int:
    """Classic Levenshtein edit distance (insert / delete / substitute)."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def _normalize_speaker_names(speeches: list) -> list:
    """
    Within a session, merge speaker name variants that differ by ≤ 1 edit
    (Levenshtein) once umlauts are folded, when they share the same role.

    This handles OCR misreads where the umlaut character itself is corrupted —
    e.g. "Löbe" → "Labe" (ö misread as a). A pure umlaut-strip grouping key
    would place "labe" and "lobe" in different buckets; Levenshtein ≤ 1 on
    the folded keys catches these cases.

    Within a role group, name variants are clustered via union-find. The
    majority surface form (highest count, tiebreak = longest) wins and every
    minority variant in the cluster has its ``name`` and ``speaker_raw``
    rewritten in place.
    """
    # 1. Group speech indices by role key (role only — not role+name).
    #    Skip role=None (regular MPs): those entries represent many different people
    #    and pairwise Levenshtein-1 on surnames would silently merge distinct speakers.
    role_groups: dict = defaultdict(list)
    for idx, sp in enumerate(speeches):
        role = sp.get("role") or ""
        if not role:
            continue  # never normalise role=None members
        role_key = _umlaut_key(role)
        role_groups[role_key].append(idx)

    # 2. Within each role group, cluster name variants by Levenshtein ≤ 1
    for indices in role_groups.values():
        if len(indices) < 2:
            continue
        name_counts: Counter = Counter(
            speeches[i]["name"] for i in indices if speeches[i].get("name")
        )
        if len(name_counts) < 2:
            continue

        variants = list(name_counts.keys())
        parent = {v: v for v in variants}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in itertools.combinations(variants, 2):
            if _levenshtein(_umlaut_key(a), _umlaut_key(b)) <= 1:
                parent[find(a)] = find(b)

        clusters: dict = defaultdict(list)
        for v in variants:
            clusters[find(v)].append(v)

        canonical: dict = {}
        for members in clusters.values():
            if len(members) < 2:
                continue
            winner = max(members, key=lambda m: (name_counts[m], len(m)))
            for m in members:
                if m != winner:
                    canonical[m] = winner

        if not canonical:
            continue

        for i in indices:
            old_name = speeches[i].get("name")
            if old_name in canonical:
                new_name = canonical[old_name]
                speeches[i]["name"] = new_name
                if speeches[i].get("speaker_raw"):
                    speeches[i]["speaker_raw"] = speeches[i]["speaker_raw"].replace(
                        old_name, new_name
                    )

    return speeches


# Dash-continuation splitting (full-inline mode only)

def _split_dash_chair(speeches: list) -> list:
    """
    In full-inline sessions (WP01 era), a speaker often resumes after an audience
    interjection without a new attribution header. Instead the transcript shows a
    new paragraph starting with '— Capital...' (em-dash + uppercase). Split each
    speech body at these points and attribute each continuation to the same speaker
    as the speech whose body contains it.

    Previously this attributed all continuations to the most recently seen chair
    (last_chair). That was wrong when an inline member's speech body contained the
    dash continuations (the member was speaking, got interrupted, then resumed).
    The fix: use the current speech's own speaker as the attribution target, so
    Renner (KPD) resuming mid-speech is attributed to Renner, not to the preceding
    chair.
    """
    expanded = []

    for speech in speeches:
        body = speech["text"]
        body_abs = speech["_body_abs_start"]

        dash_matches = list(RE_DASH_CHAIR.finditer(body))
        if not dash_matches:
            expanded.append(speech)
            continue

        speech["text"] = body[:dash_matches[0].start()].strip()
        expanded.append(speech)

        for j, dm in enumerate(dash_matches):
            dash_start = dm.start(1)
            text_start = dm.end()
            text_end = (
                dash_matches[j + 1].start()
                if j + 1 < len(dash_matches)
                else len(body)
            )
            dash_line = dm.group(1).lstrip()
            dash_text = dash_line + body[text_start:text_end]

            expanded.append({
                "speaker_raw": speech["speaker_raw"],
                "name": speech["name"],
                "role": speech["role"],
                "party": speech.get("party"),
                "location": speech.get("location"),
                "text": dash_text.strip(),
                "char_offset": body_abs + dash_start,
                "_body_abs_start": body_abs + dm.end(),
            })

    return expanded


# Dash-continuation merger (full-inline mode only)

_RE_DASH_PREFIX = re.compile(r"^—\s*")


def _merge_dash_continuations(speeches: list) -> list:
    """
    Merge consecutive same-speaker speeches where the later speech's text begins
    with an em-dash (—), as produced by _split_dash_chair().

    A dash-prefixed speech is a continuation of the preceding speech by the same
    speaker after a parenthetical audience reaction.  Merging them restores the
    single coherent speech the speaker intended.

    Conditions for merge:
      1. The later speech's text starts with '—' (after stripping whitespace).
      2. name and role are identical between the two speeches.

    The leading '— ' is stripped from the continuation text before appending so
    the merged body reads as ordinary prose.  Interjections embedded in the body
    (parenthetical audience reactions) are removed by _strip_interjections()
    downstream.
    """
    if not speeches:
        return speeches

    merged = [speeches[0]]
    for speech in speeches[1:]:
        text = speech.get("text", "")
        prev = merged[-1]
        if (
            _RE_DASH_PREFIX.match(text)
            and speech.get("name") == prev.get("name")
            and speech.get("role") == prev.get("role")
        ):
            continuation = _RE_DASH_PREFIX.sub("", text).strip()
            if continuation:
                prev["text"] = prev["text"].rstrip() + "\n\n" + continuation
            # drop the continuation speech — it is now part of prev
        else:
            merged.append(speech)

    return merged


# Whitespace normaliser

def _ws(s: str) -> str:
    """Collapse runs of spaces/tabs to a single space and strip edges.

    OCR frequently inserts extra spaces inside attributions, e.g.
    'Dr.  Adenauer  (CDU)' or 'Wolfgang  Thierse'. Normalising here
    means both ``speaker_raw`` and all derived fields (name, role …)
    are clean before any downstream analysis touches them.
    """
    return re.sub(r"\s+", " ", s).strip()


# Inline match helper

def _inline_speaker_raw(m: re.Match) -> str:
    """Extract the speaker attribution string from an RE_INLINE_SPEAKER match."""
    return _ws(
        m.group("chair_inline")
        or m.group("member_inline")
        or m.group("official_inline")
        or m.group("member_role_inline")
        or ""
    )


# Speech splitter

def split_speeches(text: str) -> list:
    """
    Find all speaker attribution lines in *text* and split the document into
    speech segments.

    Two passes:
    1. Primary pass: standalone attribution lines ending with ":" (RE_SPEAKER_LINE).
    2. Secondary pass: inline attributions "Name (...): text..." within speech bodies
       (RE_INLINE_SPEAKER) — these are Zwischenfragen / follow-up questions.

    Returns a list of dicts, one per speech, with keys:
        sequence      : 1-based speech number within the session
        speaker_raw   : full attribution string as it appears (colon stripped)
        name          : parsed speaker name
        role          : parsed role/title (Präsident, Bundesminister, …) or None
        party         : parsed party abbreviation or None
        location      : parsed geographic qualifier or None
        text          : the body of the speech (everything until the next speaker)
        char_offset   : character position of the attribution line in the source text
    """
    # --- Primary pass ---
    primary_matches = list(RE_SPEAKER_LINE.finditer(text))

    speeches = []
    for i, match in enumerate(primary_matches):
        speaker_raw = _correct_raw_name(_ws(match.group(0).rstrip(":").strip()))

        # Speech body: from end of the attribution line to start of the next one.
        body_start = match.end()
        body_end = primary_matches[i + 1].start() if i + 1 < len(primary_matches) else len(text)
        speech_text = text[body_start:body_end].strip()

        parsed = _clean_parsed(parse_speaker(speaker_raw))

        speeches.append({
            "speaker_raw": speaker_raw,
            "name": parsed["name"],
            "role": parsed["role"],
            "party": parsed["party"],
            "location": parsed["location"],
            "text": speech_text,
            "char_offset": match.start(),
            "_body_abs_start": body_start,  # temporary — removed before returning
        })

    # --- Phantom attribution filter ---
    # pdftotext repeats the active speaker's attribution line at the top of
    # every new page as a "continuation header".  b_02 sees two consecutive
    # attribution lines with no content between them and creates a speech
    # entry with text=''.  These phantom entries carry no speech content —
    # the actual text is already captured in the preceding (same-speaker) or
    # following (different-speaker) non-empty entry.  Remove them here,
    # before inline expansion, so they never reach the output JSON or dataset.
    speeches = [sp for sp in speeches if sp["text"]]

    # --- Secondary pass: inline Zwischenfragen ---
    # Case A: primary matches exist but there is text BEFORE the first one.
    # That "preamble" zone is never part of any speech body, so scan it separately
    # for inline speakers (e.g. WP02 Große Anfragen where Dr. Kather appears before
    # the first EOL-colon speaker at char_offset 84963).
    if primary_matches:
        pre_text = text[:primary_matches[0].start()]
        pre_inline = list(RE_INLINE_SPEAKER.finditer(pre_text))
        pre_speeches = []
        for i, im in enumerate(pre_inline):
            inline_raw = _correct_raw_name(_inline_speaker_raw(im))
            text_start = im.end()
            text_end = pre_inline[i + 1].start() if i + 1 < len(pre_inline) else len(pre_text)
            inline_body = pre_text[text_start:text_end].strip()
            inline_parsed = _clean_parsed(parse_speaker(inline_raw))
            pre_speeches.append({
                "speaker_raw": inline_raw,
                "name": inline_parsed["name"],
                "role": inline_parsed["role"],
                "party": inline_parsed["party"],
                "location": inline_parsed["location"],
                "text": inline_body,
                "char_offset": im.start(),
                "_body_abs_start": im.end(),
            })
        speeches = pre_speeches + speeches

    # Case B: no primary (EOL-colon) speakers were found at all — the whole document
    # uses the inline format (e.g. WP01, WP09).
    if not speeches:
        inline_matches = list(RE_INLINE_SPEAKER.finditer(text))
        for i, im in enumerate(inline_matches):
            inline_raw = _correct_raw_name(_inline_speaker_raw(im))
            text_start = im.end()
            text_end = inline_matches[i + 1].start() if i + 1 < len(inline_matches) else len(text)
            inline_body = text[text_start:text_end].strip()
            inline_parsed = _clean_parsed(parse_speaker(inline_raw))
            speeches.append({
                "speaker_raw": inline_raw,
                "name": inline_parsed["name"],
                "role": inline_parsed["role"],
                "party": inline_parsed["party"],
                "location": inline_parsed["location"],
                "text": inline_body,
                "char_offset": im.start(),
                "_body_abs_start": im.end(),
            })

        # --- Dash-continuation pass (full-inline mode only) ---
        speeches = _split_dash_chair(speeches)

    expanded = []
    for speech in speeches:
        body = speech["text"]
        body_abs = speech["_body_abs_start"]
        inline_matches = list(RE_INLINE_SPEAKER.finditer(body))

        if not inline_matches:
            expanded.append(speech)
            continue

        # Split body at each inline speaker attribution.
        # Text before the first inline speaker belongs to the outer speaker.
        outer_text = body[:inline_matches[0].start()].strip()
        speech["text"] = outer_text
        expanded.append(speech)

        for j, im in enumerate(inline_matches):
            inline_raw = _correct_raw_name(_inline_speaker_raw(im))
            # Text of this inline speech: from after the colon to the next inline match.
            text_start = im.end()
            text_end = inline_matches[j + 1].start() if j + 1 < len(inline_matches) else len(body)
            inline_body = body[text_start:text_end].strip()

            inline_parsed = _clean_parsed(parse_speaker(inline_raw))
            expanded.append({
                "speaker_raw": inline_raw,
                "name": inline_parsed["name"],
                "role": inline_parsed["role"],
                "party": inline_parsed["party"],
                "location": inline_parsed["location"],
                "text": inline_body,
                "char_offset": body_abs + im.start(),
                "_body_abs_start": body_abs + im.end(),
            })

    # --- Name normalization: fix OCR umlaut substitutions (e.g. "Labe" → "Löbe") ---
    expanded = _normalize_speaker_names(expanded)

    # --- TOC preamble filter (WP01–WP03) ---
    # In early Wahlperioden the cleaned text opens with a section-level TOC whose
    # entries use the same attribution syntax as real speakers.  The Präsident who
    # opens the actual session may be an inline speaker (not an EOL-colon primary
    # match), so this filter must run AFTER the expansion loop where inline speakers
    # are fully resolved.  At this point every speech — primary and inline — is in
    # `expanded` and the chair role is findable.
    #
    # Safety: we only drop if ALL pre-chair entries are TOC-like bodies.  If even
    # one looks like real speech (e.g. Dr. Kather in WP02 Große Anfragen), the
    # whole preamble is kept — it is legitimate content.
    _toc_chair_idx = next(
        (i for i, sp in enumerate(expanded)
         if _RE_CHAIR_ROLE.search(sp["role"] or "")),
        None,
    )
    if _toc_chair_idx is not None and _toc_chair_idx > 0:
        _pre_chair = expanded[:_toc_chair_idx]
        if all(_is_toc_entry(sp) for sp in _pre_chair):
            expanded = expanded[_toc_chair_idx:]

    # --- Merge dash-continuation speeches (WP01 full-inline mode) ---
    # After _split_dash_chair + inline expansion, consecutive same-speaker
    # speeches whose text starts with '—' are continuations of the same
    # speech separated only by audience interjections.  Merge them into one.
    expanded = _merge_dash_continuations(expanded)

    # Number sequences and remove temporary key.
    for seq, sp in enumerate(expanded, start=1):
        sp["sequence"] = seq
        sp.pop("_body_abs_start", None)

    # Re-order fields for consistent output.
    ordered = []
    for sp in expanded:
        ordered.append({
            "sequence":   sp["sequence"],
            "speaker_raw": sp["speaker_raw"],
            "name":       sp["name"],
            "role":       sp["role"],
            "party":      sp["party"],
            "location":   sp["location"],
            # Strip Kolumnentitel then audience interjections.  If stripping
            # reduces the text to empty (speech body was entirely interjections,
            # e.g. "(Beifall bei der CDU/CSU)"), keep the pre-strip text so
            # that text='' never appears in the output JSON.
            "text":       (lambda _r: _strip_interjections(_r) or _r)(
                              _strip_running_header(sp["text"], sp["name"] or "")
                          ),
            "char_offset": sp["char_offset"],
        })

    # --- Final filter: remove speeches where text reduced to empty/whitespace ---
    # After _strip_running_header, a speech whose body was nothing but the
    # speaker's continuation-column label (e.g. "Westphal\n") becomes "".
    # The earlier phantom filter (line above speeches[…if sp["text"]]) cannot
    # catch these because the original text was truthy; only here, after all
    # stripping, are they identifiable.  Remove and renumber so sequences stay
    # contiguous in the output JSON.
    ordered = [sp for sp in ordered if sp["text"].strip()]
    for seq, sp in enumerate(ordered, start=1):
        sp["sequence"] = seq

    return ordered


# Path helpers

def speakers_path_for(cleaned_path: Path) -> Path:
    """
    Return the _speakers.json path for a given _cleaned.txt path.

    e.g.  .../WP20/2022/WP20_Session010_cleaned.txt
          → .../WP20/2022/WP20_Session010_speakers.json
    """
    stem = cleaned_path.stem  # e.g. "WP20_Session010_cleaned"
    base = stem.replace("_cleaned", "")
    return cleaned_path.with_name(base + "_speakers.json")


def session_id_from_path(cleaned_path: Path) -> str:
    """Extract the session ID string from a cleaned file path."""
    m = RE_SESSION_ID.search(cleaned_path.stem)
    return m.group(1) if m else cleaned_path.stem.replace("_cleaned", "")


# Main runner

def run(force: bool = False, preprocessed_dir=None, parties_csv=None,
        wp_filter=None) -> None:
    """
    Walk the preprocessed folder, process every *_cleaned.txt file, and
    write a sibling *_speakers.json with the identified speeches.

    Parameters
    ----------
    force : bool
        If True, overwrite existing _speakers.json files.
        If False (default), skip files whose _speakers.json already exists.
    preprocessed_dir : str | Path | None
        Root of the preprocessed folder tree.  Defaults to _DEFAULT_PREPROCESSED.
    parties_csv : str | Path | None
        Path to the political-parties CSV.  When None, auto-resolved from __file__.
        Pass an explicit path on HPC where the repo layout may differ.
    wp_filter : set[str] | None
        If given, only process sessions in these Wahlperioden (zero-padded,
        e.g. {"07"}).  None means process all WPs.  This prevents concurrent
        SLURM array tasks from racing to write the same files.
    """
    global _KNOWN_PARTIES
    if parties_csv is not None:
        _KNOWN_PARTIES = _load_known_parties(parties_csv)

    in_root = Path(preprocessed_dir or _DEFAULT_PREPROCESSED)

    if not in_root.exists():
        print(f"ERROR: Preprocessed folder not found: {in_root}")
        sys.exit(1)

    log_path = in_root / "speakers.log"
    done: set = set()
    if not force and log_path.exists():
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    metadata_dir = in_root / "metadata"
    cleaned_files = sorted(
        p for p in in_root.rglob("*_cleaned.txt")
        if metadata_dir not in p.parents
    )

    print(f"Found {len(cleaned_files)} cleaned .txt file(s) under {in_root}")

    ok_count = skip_count = error_count = 0

    for cleaned_path in cleaned_files:
        # WP filter: skip sessions not in the requested Wahlperioden.
        # Path structure: .../WP{NN}/{year}/session_file
        if wp_filter is not None:
            wp_dir = cleaned_path.parent.parent.name  # e.g. "WP07"
            if wp_dir.upper().startswith("WP"):
                if wp_dir[2:].zfill(2) not in wp_filter:
                    skip_count += 1
                    continue

        out_path = speakers_path_for(cleaned_path)

        if not force and (str(out_path) in done or out_path.exists()):
            print(f"[SKIP] {cleaned_path.name}")
            skip_count += 1
            continue

        print(f"[PROC] {cleaned_path.name} ...", end=" ", flush=True)

        try:
            text = cleaned_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"READ ERROR\n  -> {e}")
            error_count += 1
            continue

        try:
            speeches = split_speeches(text)
        except Exception as e:
            print(f"PARSE ERROR\n  -> {e}")
            error_count += 1
            continue

        session_id = session_id_from_path(cleaned_path)
        record = {
            "session_id": session_id,
            "speech_count": len(speeches),
            "speeches": speeches,
        }

        try:
            out_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            append_log(log_path, str(out_path))  # flock-safe for concurrent SLURM jobs
            print(f"OK  -> {len(speeches)} speeches -> {out_path.relative_to(in_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  -> {e}")
            error_count += 1

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bundestag speaker identifier")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing _speakers.json files.",
    )
    parser.add_argument(
        "--preprocessed-dir",
        default=None,
        metavar="PATH",
        help=f"Root preprocessed folder (input and output). Default: {_DEFAULT_PREPROCESSED}",
    )
    parser.add_argument(
        "--parties-csv",
        default=None,
        metavar="PATH",
        help=(
            "Path to political-parties-past-75-years.csv. "
            "Defaults to docs/ relative to the script file."
        ),
    )
    parser.add_argument(
        "--wp",
        default=None,
        metavar="WP",
        help=(
            "Comma-separated Wahlperiode numbers to process, e.g. '14' or '1,2,14'. "
            "Zero-padding is applied automatically.  Omit to process all WPs."
        ),
    )
    args = parser.parse_args()

    pre_dir  = args.preprocessed_dir or _DEFAULT_PREPROCESSED
    wp_filter = (
        {w.strip().zfill(2) for w in args.wp.split(",")}
        if args.wp else None
    )

    if args.force:
        print("Force mode: existing _speakers.json files will be overwritten.\n")
    print("Bundestag speaker identifier")
    print(f"Input/Output: {pre_dir}")
    if wp_filter:
        print(f"WP filter:    {sorted(wp_filter)}")
    print()
    run(force=args.force, preprocessed_dir=pre_dir, parties_csv=args.parties_csv,
        wp_filter=wp_filter)
