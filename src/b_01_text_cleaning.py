"""Clean extracted Bundestag Plenarprotokoll .txt files.

Steps (in order): column-order fix → page breaks → CID artefacts → headers →
ToC removal → page numbers → footnotes → margin markers → bare separators →
back-matter refs → whitespace → hyphenation → column interleave → 2nd header pass.
Outputs *_cleaned.txt alongside originals. Resume-safe; existing files skipped unless --force.
"""

# defaults — override via --preprocessed-dir
_DEFAULT_PREPROCESSED = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"

import argparse
import re
import sys
from pathlib import Path

from utils import append_log

# Must match TXT_PAGE_SEP in b_00 exactly.
_TXT_PAGE_SEP = "\n\n--- Page Break ---\n\n"



# Matches a line that contains ONLY a single column marker "(A)", "(B)", "(C)",
# or "(D)" (with optional leading/trailing horizontal whitespace).  This is
# intentionally strict: interjection lines like "(Beifall bei der SPD)" have
# more than one character inside the parens and are never matched.
_RE_COL_MARKER_LINE = re.compile(r"^[ \t]*\(([ABCD])\)[ \t]*$", re.MULTILINE)

# Canonical A→B→C→D ordering for sorting column blocks.
_COL_ORDER: dict[str, int] = {"A": 0, "B": 1, "C": 2, "D": 3}

RE_PAGE_BREAK = re.compile(
    r"^-{3,}\s*Page Break\s*-{3,}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# New format (born-digital, WP11+):
#   "Deutscher Bundestag – 20. Wahlperiode – 10. Sitzung. Berlin, Mittwoch, den 12. Januar 2022"
# [ \t] instead of \s throughout: headers are single-line; \s would match \n and
# cause cross-line backtracking on files that are mostly whitespace (e.g. CID-artefact files).
# [.,]? instead of \.? for the WP number separator: WP11–WP12 OCR sometimes reads
# the period as a comma ("11, Wahlperiode" instead of "11. Wahlperiode").
RE_HEADER_NEW = re.compile(
    r"^[ \t]*(?:\d{1,5}\*?[ \t]+)?(?:\([ABCD]\))*[ \t]*"
    r"Deutscher[ \t]+Bundestag[ \t]*[–—-][ \t]*\d{1,2}[ \t]*[.,]?[ \t]*Wahlperiode.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Old format (OCR'd, WP1-10):
#   "Deutscher Bundestag — 1. Sitzung. Bonn, Mittwoch, den 7. September 1949"
# WP01–WP09 mid-session headers carry a two-part prefix: page number then WP number,
# e.g. "688 2. Deutscher Bundestag — 20. Sitzung." — the extra (?:\d{1,2}[.,]?[ \t]+)?
# group absorbs the WP number so the anchor still matches at line start.
RE_HEADER_OLD = re.compile(
    r"^[ \t]*(?:\d{1,5}\*?[ \t]+)?(?:\d{1,2}[.,]?[ \t]+)?(?:\([ABCD]\))*[ \t]*"
    r"Deutscher[ \t]+Bundestag[ \t]*[–—-][ \t]*\d{1,3}[ \t]*[.,]?[ \t]*Sitzung\..*$",
    re.IGNORECASE | re.MULTILINE,
)

# Inline header: same header text fused mid-line due to two-column OCR merging.
# pdftotext sometimes places a right-column page header on the same output line as
# left-column speech text, e.g.:
#   "öffent690 2. Deutscher Bundestag — 20. Sitzung. Bonn, ..."
# rejoin_hyphens also produces mid-line headers when a word ends with a hyphen just
# before a header that survived the anchored pass:
#   "Entwick-\nDeutscher Bundestag — 11. Wahlperiode..." → "Entwick-Deutscher Bundestag..."
# The leading WP-number `\b\d{1,2}[.,]` token is made optional so that headers fused
# WITHOUT a leading digit (the born-digital WP11/12 case) are also caught.
# [.,] handles OCR period/comma confusion on the WP number separator.
RE_HEADER_INLINE = re.compile(
    r"(?:\b\d{1,2}[ \t]*[.,][ \t]*)?Deutscher[ \t]+Bundestag[ \t]*[–—-][^\n]*"
    r"(?:Wahlperiode|Sitzung\.)[^\n]*",
    re.IGNORECASE,
)

# Parenthesised speaker-attribution tags left on their own line directly
# under old-format headers — column-layout OCR artefacts, not speech.
RE_HEADER_SPEAKER_TAG = re.compile(
    r"^\(\s*[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß \t\.]+\)\s*$",   # \s→[ \t]: no newlines inside [] to prevent cross-line backtracking
    re.MULTILINE,
)

# pdftotext emits "(cid:N)" when it cannot map a PDF glyph to Unicode.
# The most common is (cid:9) for the tab character, but any numeric ID
# can appear.  Strip the entire token and the surrounding whitespace context
# is then handled by the whitespace normalisation step.
#
# Special case — fully-CID-encoded PDFs: some WP14 born-digital PDFs use a
# proprietary font with a custom glyph-ID→Unicode mapping that pdftotext cannot
# resolve, so EVERY character comes out as (cid:N).  In those cases stripping
# all CIDs leaves an empty document.  We detect this (>60% of non-whitespace
# "characters" are CID tokens) and decode by finding the additive offset that
# maximises the fraction of printable Latin characters.
# Known case: 14169.pdf uses offset +29 (chr(cid+29) gives ASCII text).
RE_CID = re.compile(r"\(cid:\d+\)")

# Garbled-printable detection: some pdftotext builds decode a proprietary font's
# glyphs to the wrong Unicode code points (every non-space char shifted by -N from
# the correct value).  Known case: HPC pdftotext for WP14 14169.pdf uses shift -29,
# turning "Plenarprotokoll" into "3OHQDUSURWRNROO".  We detect this via short probe
# strings (the garbled form of words guaranteed to appear in every protocol) and fix
# by adding +shift to every printable non-space character in range 0x21–(0x7E–shift).
# Whitespace (0x20, \n, \t, \r) is left unchanged — pdftotext inserts it via PDF
# positioning commands, not via glyph lookups, so it is always correct.
_GARBLED_SHIFT_PROBES: dict[int, str] = {
    29: "3OHQDU",   # "Plenar" with every char shifted -29
}

# CID-encoded probe for pdfminer output: the sequence below is "Plenar" where
# each glyph's CID is the correct Unicode code point minus 29.
# If this exact CID sequence appears in the raw text, offset +29 is forced
# without ratio guessing (ratio detection can be fooled by rare high-CID glyphs
# that produce non-printable chars with offset 29 but printable ones with offset 0).
_CID_OFFSET_PROBES: dict[int, tuple] = {
    # offset → (cid_probe_string, post_decode_corrections_dict)
    #
    # The corrections dict fixes characters that the font places at non-standard
    # byte positions and therefore decode to the wrong code point with a plain +offset.
    # For 14169.pdf the font places German lowercase umlauts at bytes that produce
    # C1 control codes or broken-bar when shifted by +29:
    #   'ä' font-byte 108 → chr(137)=\x89  (should be chr(228)=ä)
    #   'ö' font-byte 124 → chr(153)=\x99  (should be chr(246)=ö)
    #   'ü' font-byte 129 → chr(158)=\x9e  (should be chr(252)=ü)
    #   'ß' font-byte 137 → chr(166)=¦     (should be chr(223)=ß)
    # Capital umlauts (Ä, Ö, Ü) follow the +29 rule correctly and need no fix.
    29: (
        "(cid:51)(cid:79)(cid:72)(cid:81)(cid:68)(cid:85)",  # "Plenar" CID sequence
        {"\x89": "ä", "\x99": "ö", "\x9e": "ü", "¦": "ß"},
    ),
}

# Matches lines that contain only:
#   - an optional Roman-numeral prefix   e.g. "II 42"  (ToC pages)
#   - digits (the page number)
#   - an optional trailing asterisk      e.g. "18768*" (asterisked annex pages)
#   - an optional trailing column letter A–D  e.g. "26097 A", "5952 D"
#   - optional trailing comma + more page-column refs  e.g. "5952 A, D"
# All surrounded by optional whitespace.
RE_PAGE_NUMBER = re.compile(
    r"^\s*(?:[IVXivx]{1,5}\s+)?\d{1,5}\*?(?:\s*[A-D])?(?:\s*,\s*\d{1,5}\*?(?:\s*[A-D])?)*\s*$",
    re.MULTILINE,
)


# A ToC entry line.  Must end with a page/column reference (with optional
# asterisk), preceded by one of three separator patterns:
#
#   Pattern A — dot-leader:  ". . . . . . 42 B"
#   Pattern B — wide space:  "Abwicklung der Tagesordnung   18683 C"
#   Pattern C — direct:      "Heimann, Senator des Landes Berlin . . 18683 D"
#                            (one or more dots then the number, no wide space)
#
# The non-greedy leading ".+?" ensures we require at least one character of
# real content before the separator, preventing accidental matches on lines
# that are pure dot-leaders or page numbers already stripped by step 4.
# The trailing [A-D]? and \*? handle column letters and asterisked page refs.
RE_TOC_ENTRY = re.compile(
    r"^.+?"                           # non-empty leading content
    r"(?:"
    r"(?:\.\s*){3,}"                  # Pattern A: dot-leader (3+ dots)
    r"|\s{3,}"                        # Pattern B: 3+ spaces (tabbed alignment)
    r"|(?:\s*\.\s*){1,2}\s+"          # Pattern C: 1-2 dots then space
    r")"
    r"\d{1,5}\*?\s*[A-D]?\s*$",      # page number + optional * + optional col letter
    re.MULTILINE,
)

# Spaced-letter headings used as ToC section titles in some WPs:
#   "I n h a l t :"   "I N H A L T"
RE_TOC_HEADING = re.compile(
    r"^\s*(?:[A-ZÄÖÜa-zäöü]\s){2,}[A-ZÄÖÜa-zäöü]\s*:?\s*$",
    re.MULTILINE,
)

# Unambiguous proceedings openers — cannot appear inside a ToC.
# Matched against the full text; the match position becomes the anchor.
RE_ANCHOR_STRONG = re.compile(
    r"(?:"
    r"^\s*Beginn\s*:\s*\d{1,2}[:.]\d{2}\s*Uhr\s*$"        # "Beginn: 13.00 Uhr"
    r"|Die\s+Sitzung\s+wird\s+(?:um\s+\d|eröffnet)"        # "Die Sitzung wird um 16 Uhr …"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Fallback anchor: a speaker-attribution line of the form "Name Name:" or
# "Präsidentin Bärbel Bas:" that is preceded by a blank line.
# Requires a blank line before to exclude ToC parenthetical fragments.
RE_ANCHOR_SPEAKER = re.compile(
    r"(?<=\n\n)"                              # preceded by blank line
    r"(?:Alterspräsident|Vizepräsident(?:in)?|Präsident(?:in)?)"
    r"\s+[A-ZÄÖÜ][^\n:]{1,60}:\s*$",
    re.MULTILINE,
)

RE_FOOTNOTE_MARKER = re.compile(
    r"^\s*(?:"
    r"\*\s+"           # asterisk footnote
    r"|†\s+"           # dagger
    r"|\d{1,2}[.)]\s+" # numbered: "1) …" or "1. …"
    r"|[a-z][.)]\s+"   # lettered: "a) …"
    r")",
    re.MULTILINE,
)

# Colophon/printer lines — both multi-line and single-line concatenated form.
RE_COLOPHON = re.compile(
    r"^\s*(?:"
    r"Gesamtherstellung\s*:.*"
    r"|Satz\s*:\s*Satzweiss.*"
    r"|Druck\s*:\s*Printsystem.*"
    r"|Vertrieb\s*:.*(?:Bundesanzeiger|betrifft-gesetze).*"
    r"|ISSN\s+[\d\-]+"
    r"|BUB\s+Gebr\."
    r"|Druck\s+und\s+Vertrieb.*"
    # Single-line concatenation of the above (born-digital WP19+):
    r"|Satz\s*:.*ISSN\s+[\d\-]+"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Isolated "(A)", "(B)", "(C)", "(D)" on their own line or inline.
# Also catches the concatenated form "(A)(C)(B)(D)" found at session ends.
RE_MARGIN_MARKER = re.compile(
    r"(?:^|\s)\([ABCD]\)(?:\s|$)"
    r"|(?:\([ABCD]\)){2,}",        # concatenated form
    re.MULTILINE,
)

# Lines consisting solely of one or two hyphen/dash/em-dash characters.
# These are OCR artefacts from the two-column vertical rule.
RE_BARE_SEPARATOR = re.compile(
    r"^\s*[-–—]{1,2}\s*$",
    re.MULTILINE,
)

# Lines in the "Amtliche Mitteilungen" / EU-document appendix that consist
# only of reference codes.  Matched line-by-line inside a detected zone.
RE_BACKM_REF = re.compile(
    r"^\s*(?:"
    r"(?:Drucksache\s+[\d/,\s]+)"           # "Drucksache 16/12954 Nr. A.2"
    r"|(?:Ratsdokument\s+[\d/]+)"            # "Ratsdokument 8182/09"
    r"|(?:EuB-EP\s+[\d;/\s\w\-]+)"          # "EuB-EP 1891; P6_TA-PROV(2009)0077"
    r"|(?:P\d+_TA[^\n]{0,60})"              # "P6_TA-PROV(2009)0077"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Heading that introduces an "Amtliche Mitteilungen" back-matter block.
RE_BACKM_START = re.compile(
    r"^Amtliche\s+Mitteilungen\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Matches a word character, a plain or soft hyphen, optional horizontal
# whitespace (born-digital PDFs sometimes have a trailing space after the
# hyphen before the newline), one or more newlines, and the first character
# of the continuation word.
# [ \t]* handles trailing spaces before \n (normalise_whitespace would strip
# those spaces AFTER this step, creating a new hyphen-newline pattern).
# \n+ (not \n{1,2}) because some scanned pages produce 3-4 newlines between
# line parts when the break falls near a page boundary.
# Two-case replacement logic:
#   lowercase continuation -> line-break artefact -> remove hyphen and join
#     e.g.  "Bun-\ndesrepublik"      ->  "Bundesrepublik"
#           "Her-\n\nren"            ->  "Herren"
#           "Deutsch-\n\n\n\nland"  ->  "Deutschland"
#   uppercase continuation -> genuine compound hyphen -> keep hyphen, drop newline(s)
#     e.g.  "Baden-\n\nWürttemberg"  ->  "Baden-Württemberg"
#           "CDU-\n\nCSU"           ->  "CDU-CSU"
RE_HYPHEN_BREAK = re.compile(r"(\w)[-\xad][ \t]*\n+(\w)", re.UNICODE)

RE_EXCESS_BLANK = re.compile(r"\n{3,}")

# Born-digital Bundestag PDFs (WP11+) use a two-column layout: speech text
# on the left, interjections (Beifall, Zurufe, etc.) on the right.  pdfminer
# reads pages in horizontal bands, so a right-column interjection block is
# inserted mid-sentence, splitting a single sentence into a fragment above and
# a lowercase continuation below.
#
# Detection pattern:
#   Group 1 — a non-parenthesised text line  (the sentence fragment)
#   Group 2 — one or more interjection blocks in parentheses, separated by
#              blank lines  (the interleaved right-column content)
#   Group 3 — a line starting with a lowercase letter  (the continuation)
#
# Replacement: fragment + " " + continuation + "\n\n" + interjection block(s)
# Applied in a loop until no more matches (handles multiple consecutive
# interjections splitting the same sentence).
# Interjection-paragraph detector: a paragraph whose first line starts with
# "(" followed immediately by one of the known floor-reaction keywords.
# Matching only the START of the paragraph avoids any backtracking risk.
_INTERJECTION_KW = (
    r"Beifall|Zurufe?|Heiterkeit|Lachen|Widerspruch|Gegenrufe?"
    r"|Lebhafter?|Anhaltender?|Stürmischer?|Erneuter?"
    r"|Vereinzelt|Sehr |Wiederholte?|Große[nr]?"
)
RE_INTJ_PARA = re.compile(r"^\((?:" + _INTERJECTION_KW + r")", re.MULTILINE)


# Individual cleaning steps

def remove_page_breaks(text: str) -> str:
    """Step 1 — remove '--- Page Break ---' markers."""
    return RE_PAGE_BREAK.sub("", text)


def remove_headers(text: str) -> str:
    """Step 2 — remove repeated page headers (old and new format)."""
    text = RE_HEADER_NEW.sub("", text)
    text = RE_HEADER_OLD.sub("", text)
    text = RE_HEADER_SPEAKER_TAG.sub("", text)
    # Non-anchored pass: catches headers fused mid-line by two-column OCR merging
    # (e.g. "öffent690 2. Deutscher Bundestag — 20. Sitzung. Bonn, ...").
    # Runs after the anchored passes so it only fires on genuine residuals.
    text = RE_HEADER_INLINE.sub("", text)
    return text


def remove_cid_artefacts(text: str) -> str:
    """Strip (cid:N) tokens; for fully-CID-encoded PDFs decode via offset (+29 for WP14 14169.pdf).

    Also detects garbled-printable output where pdftotext decoded all glyphs with a wrong
    code-point shift (e.g. shift -29, turning "Plenarprotokoll" into "3OHQDUSURWRNROO").
    """
    cid_values = RE_CID.findall(text)

    # --- Mode B: no CID tokens but garbled printable characters ---
    if not cid_values:
        for shift, probe in _GARBLED_SHIFT_PROBES.items():
            if probe in text:
                ceiling = 0x7E - shift          # max input char that stays in printable range
                return "".join(
                    chr(ord(c) + shift) if (0x21 <= ord(c) <= ceiling) else c
                    for c in text
                )
        return text

    # Estimate fraction of content that is CID tokens.
    non_cid = RE_CID.sub("", text).replace("\n", "").replace(" ", "")
    if len(cid_values) <= len(non_cid):
        # Normal case: CIDs are a minority — just strip them.
        return RE_CID.sub("", text)

    # Fully-CID-encoded document: try to decode.
    def _decode(offset: int) -> str:
        def _sub(m: "re.Match") -> str:
            cp = int(m.group(0)[5:-1]) + offset   # (cid:N) → N + offset
            return chr(cp) if 0x20 <= cp <= 0xFFFF else ""
        return RE_CID.sub(_sub, text)

    # Fast path: if a known CID-sequence probe is found, the offset is certain.
    # Ratio-based detection can be fooled when a handful of high-CID glyphs produce
    # non-printable chars with the correct offset but printable chars with offset 0.
    for forced_offset, (cid_probe, corrections) in _CID_OFFSET_PROBES.items():
        if cid_probe in text:
            decoded = _decode(forced_offset)
            if corrections:
                decoded = decoded.translate(str.maketrans(corrections))
            return decoded

    def _printable_ratio(s: str) -> float:
        chars = [c for c in s if c not in " \n\t\r"]
        if not chars:
            return 0.0
        ok = sum(1 for c in chars if 0x20 <= ord(c) <= 0x7E or 0xC0 <= ord(c) <= 0x2AF)
        return ok / len(chars)

    best_offset, best_ratio = 29, 0.0          # 29 is the known offset for 14169.pdf
    for offset in [29, 30, 27, 28, 31, 32, 26, 25, 33, 0]:
        ratio = _printable_ratio(_decode(offset))
        if ratio > best_ratio:
            best_ratio, best_offset = ratio, offset

    if best_ratio >= 0.85:
        return _decode(best_offset)

    # No clean decoding found — fall back to stripping.
    return RE_CID.sub("", text)


def remove_page_numbers(text: str) -> str:
    """Step 5 — remove lines that contain only a page/column reference."""
    return RE_PAGE_NUMBER.sub("", text)


def remove_toc(text: str) -> str:
    """Remove the ToC and session cover preamble by discarding everything before the first anchor.

    Two-pass: strong anchor ("Beginn: HH.MM Uhr" / "Die Sitzung wird eröffnet"),
    then fallback speaker anchor (first presidential speaker-colon line after blank line).
    Returns text unchanged if no anchor is found.
    """
    # Pass 1: strong anchor
    anchor = RE_ANCHOR_STRONG.search(text)

    # Pass 2: fallback speaker anchor
    if anchor is None:
        anchor = RE_ANCHOR_SPEAKER.search(text)

    if anchor is None:
        return text  # no anchor found — play it safe

    return text[anchor.start():]


def remove_footnotes(text: str) -> str:
    """Remove footnote lines (*, †, numbered) and colophon/printer lines."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        if RE_COLOPHON.match(line):
            continue
        if RE_FOOTNOTE_MARKER.match(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def remove_margin_markers(text: str) -> str:
    """Remove isolated (A)/(B)/(C)/(D) column markers and concatenated forms like (A)(C)(B)(D)."""
    return RE_MARGIN_MARKER.sub(" ", text)


def remove_bare_separators(text: str) -> str:
    """Remove lines of only one or two hyphens/dashes (OCR column-rule artefacts)."""
    return RE_BARE_SEPARATOR.sub("", text)


def remove_backm_references(text: str) -> str:
    """Remove EU/Ratsdokument reference lines in back-matter blocks (after "Amtliche Mitteilungen")."""
    start = RE_BACKM_START.search(text)
    if start is None:
        return text

    preamble = text[:start.start()]
    backm    = text[start.start():]
    cleaned_backm = RE_BACKM_REF.sub("", backm)
    return preamble + cleaned_backm


def rejoin_hyphens(text: str) -> str:
    """Rejoin end-of-line hyphenation: lowercase continuation → join, uppercase → keep hyphen.

    Uses \\n+ (not \\n{1,2}) because some scanned pages produce 3–4 newlines at page boundaries.
    Handles both plain hyphens and soft-hyphens (U+00AD).
    """
    def _fix(m: re.Match) -> str:
        before, after = m.group(1), m.group(2)
        if after.isupper():
            return before + "-" + after   # keep hyphen for genuine compounds; drop newlines
        return before + after             # remove hyphen for line-break artefacts

    return RE_HYPHEN_BREAK.sub(_fix, text)


def normalise_whitespace(text: str) -> str:
    """
    Step 11 — strip trailing spaces and collapse excess blank lines.
    """
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = RE_EXCESS_BLANK.sub("\n\n", text)
    return text.strip()


def _fix_page_column_order(page: str) -> str:
    """Sort column blocks within one page to canonical A→B→C→D order."""
    markers = [
        (m.group(1), m.start(), m.end())
        for m in _RE_COL_MARKER_LINE.finditer(page)
    ]
    if len(markers) < 2:
        return page  # not enough markers to detect or correct ordering

    letters = [letter for letter, _s, _e in markers]

    # Detect out-of-order: any C/D appearing before a later A/B → ACBD / CDAB / etc.
    saw_right_col = False
    needs_reorder = False
    for letter in letters:
        if letter in ("C", "D"):
            saw_right_col = True
        elif letter in ("A", "B") and saw_right_col:
            needs_reorder = True
            break

    if not needs_reorder:
        return page

    # Text before the first column marker (page header, stray lines, etc.) is
    # kept as-is; only the marker blocks themselves are reordered.
    pre_text = page[: markers[0][1]]

    # Each block spans from its marker line start to just before the next marker.
    blocks: list[tuple[str, str]] = []
    for i, (letter, start, _end) in enumerate(markers):
        block_end = markers[i + 1][1] if i + 1 < len(markers) else len(page)
        blocks.append((letter, page[start:block_end]))

    # Stable sort: same-letter duplicates (rare edge case) keep original order.
    sorted_blocks = sorted(blocks, key=lambda x: _COL_ORDER.get(x[0], 99))
    return pre_text + "".join(content for _letter, content in sorted_blocks)


def fix_column_order(text: str) -> str:
    """Fix two-column horizontal-band interleaving in born-digital PDFs (ACBD → ABCD).

    pdftotext reads pages in horizontal bands, producing ACBD block order instead
    of the correct column order A→B→C→D. ~45,000 pages affected across WP13–WP20.
    """
    pages = text.split(_TXT_PAGE_SEP)
    fixed_pages = [_fix_page_column_order(page) for page in pages]
    return _TXT_PAGE_SEP.join(fixed_pages)


def _is_interjection_para(para: str) -> bool:
    """Return True if *para* is a floor-reaction block like '(Beifall bei …)'."""
    return bool(RE_INTJ_PARA.match(para))


def fix_column_interleave(text: str) -> str:
    """Reorder interjection blocks inserted mid-sentence by two-column pdfminer band reading.

    Pattern: sentence-fragment + (Beifall/Zuruf block) + lowercase-continuation →
    reordered to: sentence + continuation → interjection. Loops until stable.
    """
    paras = text.split("\n\n")
    changed = True
    while changed:
        changed = False
        new_paras: list[str] = []
        i = 0
        while i < len(paras):
            p = paras[i]
            # Skip paragraphs that themselves start with "(" — they are
            # interjections or parenthetical asides, not sentence fragments.
            if p.lstrip().startswith("("):
                new_paras.append(p)
                i += 1
                continue
            # Collect the run of interjection paragraphs immediately following.
            j = i + 1
            while j < len(paras) and _is_interjection_para(paras[j]):
                j += 1
            # Pattern requires at least one interjection AND a lowercase
            # continuation paragraph right after the interjection run.
            if (j > i + 1                               # at least one interjection
                    and j < len(paras)                  # continuation exists
                    and paras[j]                        # non-empty
                    and paras[j][0] in
                        "abcdefghijklmnopqrstuvwxyzäöüß"):
                new_paras.append(p + " " + paras[j])   # fragment + continuation
                new_paras.extend(paras[i + 1 : j])     # interjections after
                i = j + 1
                changed = True
            else:
                new_paras.append(p)
                i += 1
        paras = new_paras
    return "\n\n".join(paras)


# A speaker qualifier split across a line break by the two-column layout:
#
#   Dr. Jan-Niclas Gesenhues (BÜNDNIS 90/DIE
#   GRÜNEN):
#
# b_02's Pattern B requires the parenthetical to contain no newline, so a
# wrapped label makes the whole attribution invisible and the speech is
# absorbed into the preceding speaker's record.  Long surnames push the
# label over the column edge, so the loss is not random: it falls mostly on
# BÜNDNIS 90/DIE GRÜNEN members and on double-barrelled (often women's) names.
#
# The trailing colon is the discriminator.  An interjection such as
# "(Beifall bei der CDU/CSU\nund der SPD)" is never followed by one, so it is
# left alone.
_RE_WRAPPED_QUALIFIER = re.compile(r"\(([^()\n]{0,60})\n+[ \t]*([^()\n]{0,60})\)([ \t]*:)")

# Only rejoin content that is actually a speaker qualifier.  Without this the
# rule would also join page and document references — "(Drucksachen 1252,\n573,
# 614):" — which b_02 could then mistake for a name + qualifier and turn into
# a speaker that never existed.
_RE_QUAL_BEIFALL = re.compile(r"\bbegrüßt\b", re.I)

_RE_QUAL_PARTY = re.compile(
    r"^(?:BÜNDNIS\s*90|DIE\s+GRÜNEN?|GRÜNEN?|DIE\s+LINKE|LINKE|PDS|CDU|CSU|SPD|FDP|F\.D\.P\.|"
    r"AFD|DP|KPD|BP|WAV|SSW|Z|GB/BHE|DRP|FU|FVP|DA|SRP|SPP|SDP|GRUPPE|FRAKTIONSLOS|PARTEILOS)"
    r"[A-ZÄÖÜ0-9/.\s\-]*$", re.I)
_RE_QUAL_PLACE = re.compile(r"^[A-ZÄÖÜ][a-zäöüß\-]+(?:\s+[A-ZÄÖÜ][a-zäöüß\-]+){0,2}$")
_RE_QUAL_REJECT = re.compile(r"Drucksache|Sitzung|Anlage|Umdruck|\d{3,}", re.I)

# A party or place qualifier is short.  Where a column interleave has dropped a
# sentence between the two halves of a label, re.IGNORECASE lets that prose
# through the pattern's trailing character class and the whole fragment is
# accepted:
#
#     Albert Schmidt (Hitzhofen) (BÜNDNIS 90/DIE
#     einer Senkung der Abgaben für die Sozialkassen ver
#     GRÜNEN): ...
#
# which gave one WP13 speech a 70-character party value.  The length cap is
# what rules that out.  Dropping re.IGNORECASE instead would also have rejected
# legitimate qualifiers — "PDS/Linke Liste", "GRÜNE/Bündnis 90", "Gruppe der
# PDS" — and so lost roughly 2,500 genuine joins.  The cap does not apply to
# the Beifall branch, whose annotations are long by nature.
_MAX_LABEL_QUAL_LEN = 40


def _is_speaker_qualifier(text: str) -> bool:
    """True when a joined parenthetical looks like a party, place or Beifall note."""
    if _RE_QUAL_REJECT.search(text):
        return False
    if _RE_QUAL_BEIFALL.search(text):
        return True
    if len(text) > _MAX_LABEL_QUAL_LEN:
        return False
    return bool(_RE_QUAL_PARTY.match(text) or _RE_QUAL_PLACE.match(text))


def rejoin_wrapped_qualifiers(text: str) -> str:
    """Join a speaker's parenthesised qualifier that the column layout split."""
    def _sub(m: re.Match) -> str:
        joined = " ".join((m.group(1) + " " + m.group(2)).split())
        if not _is_speaker_qualifier(joined):
            return m.group(0)
        return f"({joined}){m.group(3)}"
    return _RE_WRAPPED_QUALIFIER.sub(_sub, text)


# Full cleaning pipeline

PIPELINE = [
    ("fix_column_order",         fix_column_order),       # step 0 — before page-break removal so pages are still individually addressable; before margin-marker removal so (A)-(D) are still present
    ("remove_page_breaks",       remove_page_breaks),
    ("remove_cid_artefacts",     remove_cid_artefacts),   # before headers: some headers have (cid:9) between page-no and "Deutscher"
    ("remove_headers",           remove_headers),
    ("remove_toc",               remove_toc),             # must run BEFORE remove_page_numbers
    ("remove_page_numbers",      remove_page_numbers),    # strips refs ToC step relied on
    ("remove_footnotes",         remove_footnotes),
    ("remove_margin_markers",    remove_margin_markers),
    ("remove_bare_separators",   remove_bare_separators),
    ("remove_backm_references",  remove_backm_references),
    ("normalise_whitespace",     normalise_whitespace),   # strip trailing spaces BEFORE hyphen-join so blank lines with spaces don't hide patterns
    ("rejoin_hyphens",           rejoin_hyphens),
    ("fix_column_interleave",    fix_column_interleave),  # must run AFTER normalise_whitespace (needs clean \n\n separators) and rejoin_hyphens
    ("remove_headers",           remove_headers),         # second pass: catches headers fused mid-line by rejoin_hyphens or moved by fix_column_interleave
    ("rejoin_wrapped_qualifiers", rejoin_wrapped_qualifiers),  # last: operates on the final text b_02 will match against
]


def clean(text: str) -> str:
    """Run the full cleaning pipeline on a text string."""
    for _name, fn in PIPELINE:
        text = fn(text)
    return text


# Path helpers

def cleaned_path_for(txt_path: Path) -> Path:
    """
    Given the path to an extracted .txt file, return the path for the
    corresponding _cleaned.txt file in the same folder.

    e.g.  .../WP01/1949/WP01_Session001.txt
          → .../WP01/1949/WP01_Session001_cleaned.txt
    """
    return txt_path.with_name(txt_path.stem + "_cleaned.txt")


# Main runner

def run(force: bool = False, preprocessed_dir=None, wp_filter=None) -> None:
    in_root = Path(preprocessed_dir or _DEFAULT_PREPROCESSED)

    if not in_root.exists():
        print(f"ERROR: Preprocessed folder not found: {in_root}")
        sys.exit(1)

    log_path = in_root / "cleaning.log"
    done: set[str] = set()
    if not force and log_path.exists():
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    metadata_dir = in_root / "metadata"
    _NON_SESSION = {"validation_report", "validation_report_cleaned"}
    txt_files = sorted(
        p for p in in_root.rglob("*.txt")
        if not p.stem.endswith("_cleaned")
        and p.stem not in _NON_SESSION
        and metadata_dir not in p.parents
    )

    print(f"Found {len(txt_files)} extracted .txt file(s) under {in_root}")

    ok_count = skip_count = error_count = 0

    for txt_path in txt_files:
        # WP filter: skip sessions not in the requested Wahlperioden.
        # Path structure: .../WP{NN}/{year}/session_file
        if wp_filter is not None:
            wp_dir = txt_path.parent.parent.name  # e.g. "WP07"
            if wp_dir.upper().startswith("WP"):
                if wp_dir[2:].zfill(2) not in wp_filter:
                    skip_count += 1
                    continue

        out_path = cleaned_path_for(txt_path)

        if not force and (str(out_path) in done or out_path.exists()):
            print(f"[SKIP] {txt_path.name}")
            skip_count += 1
            continue

        print(f"[PROC] {txt_path.name} ...", end=" ", flush=True)

        try:
            raw = txt_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"READ ERROR\n  -> {e}")
            error_count += 1
            continue

        try:
            cleaned_text = clean(raw)
        except Exception as e:
            print(f"CLEAN ERROR\n  -> {e}")
            error_count += 1
            continue

        try:
            out_path.write_text(cleaned_text, encoding="utf-8")
            append_log(log_path, str(out_path))  # flock-safe for concurrent SLURM jobs
            print(f"OK -> {out_path.relative_to(in_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  -> {e}")
            error_count += 1

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bundestag text cleaner")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing _cleaned.txt files.",
    )
    parser.add_argument(
        "--preprocessed-dir",
        default=None,
        metavar="PATH",
        help=f"Root preprocessed folder (input and output). Default: {_DEFAULT_PREPROCESSED}",
    )
    parser.add_argument(
        "--wp",
        default=None,
        metavar="WP",
        help="Comma-separated Wahlperiode numbers to process (e.g. '14' or '01,02'). "
             "Default: all WPs.",
    )
    args = parser.parse_args()

    pre_dir = args.preprocessed_dir or _DEFAULT_PREPROCESSED
    wp_filter = (
        {w.strip().zfill(2) for w in args.wp.split(",")}
        if args.wp else None
    )

    if args.force:
        print("Force mode: existing _cleaned.txt files will be overwritten.\n")
    print("Bundestag text cleaner")
    print(f"Input/Output: {pre_dir}")
    if wp_filter:
        print(f"WP filter   : {sorted(wp_filter)}")
    print()
    run(force=args.force, preprocessed_dir=pre_dir, wp_filter=wp_filter)