"""
b_01_text_cleaning.py
=====================
Cleans extracted Bundestag Plenarprotokoll .txt files produced by
b_00_text_extractor.py and writes cleaned .txt files mirroring the same
folder structure.

Input  folder : preprocessed/WP{wp}/{year}/WP{wp}_Session{session}.txt
Output folder : preprocessed/WP{wp}/{year}/WP{wp}_Session{session}_cleaned.txt

Cleaning steps (applied in order)
-----------------------------------
1.  Remove page-break markers inserted by the extractor
    ("--- Page Break ---" lines).
2.  Remove repeated page headers.
    Old format : "Deutscher Bundestag — N. Sitzung. Bonn, <Weekday>, den <date>"
    New format : "Deutscher Bundestag – WW. Wahlperiode – NN. Sitzung. Berlin, ..."
    Both the full-line variant and OCR-broken multi-line variants are handled.
3.  Remove CID character-ID artefacts left by pdftotext when it cannot map
    a glyph to Unicode, e.g. "(cid:9)", "(cid:13)".
4.  Remove the table of contents block.  (Runs BEFORE page-number stripping
    so that ToC entries still carry their page-reference suffixes, which the
    detection regex relies on.)
    Strategy: scan the preamble (everything before the first substantive
    proceedings line) for lines that look like ToC entries — dot-leader,
    wide-space, or short-dot separator followed by a page/column reference.
    Also removes spaced-letter headings ("I n h a l t :").
    The proceedings anchor is found by a two-pass approach:
      Pass A — unambiguous opener: "Beginn: HH.MM Uhr" or
               "Die Sitzung wird … eröffnet".
      Pass B — fallback: first presidential speaker-colon line preceded by
               a blank line.
    If no anchor is found the step is skipped conservatively.
5.  Remove standalone page numbers.
    Matches lines that are only digits, optionally with a Roman-numeral
    prefix (ToC pages), an optional trailing asterisk (e.g. "18768*"),
    and optionally with a trailing column letter A–D (e.g. "26097 A").
6.  Remove footnote lines and printer/colophon lines.
    Footnotes are lines starting with *, †, numbered (1) / lettered (a))
    markers.  Colophon lines match known printer, ISSN, and Vertrieb
    patterns — including the single-line concatenated form produced by some
    born-digital PDFs.
7.  Remove column/margin position markers left by OCR: isolated "(A)", "(B)",
    "(C)", "(D)" tokens that appear on their own line or inline, including
    the concatenated form "(A)(B)(C)(D)" sometimes found at session end.
8.  Remove bare separator lines: lines consisting solely of one or two
    hyphen/dash characters, which are OCR column-rule artefacts.
9.  Remove back-matter reference blocks ("Amtliche Mitteilungen") that
    appear after the last "Anlage N" heading and consist exclusively of
    EU/Ratsdokument reference lines and Drucksachen numbers.  These blocks
    carry no speech content.  Removal is conservative: only lines that match
    the reference-code pattern within a detected back-matter zone are dropped.
10. Rejoin words broken by end-of-line hyphenation (applies to all sessions;
    born-digital PDFs also produce soft-hyphen breaks in the extracted text).
11. Normalise whitespace: collapse multiple blank lines to a single blank line
    and strip trailing spaces from every line.

Design notes
------------
- Each step is a pure function that takes a string and returns a string,
  making it easy to test, reorder, or disable individual steps.
- Regexes are compiled once at module level.
- The cleaner is intentionally conservative: when in doubt it keeps text
  rather than discarding it.  Speaker attribution lines, interjections,
  floor reactions, and procedural announcements are ALL preserved.
- Resume-safe: files whose _cleaned.txt already exists are skipped unless
  the --force flag is passed (see run()).
- A separate cleaning.log records each successfully cleaned file (one path
  per line) for auditing.
"""

# ============================================================
# USER SETTINGS
# ============================================================
PREPROCESSED_FOLDER = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"
# ============================================================

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# --- Step 1: page-break marker ---
RE_PAGE_BREAK = re.compile(
    r"^-{3,}\s*Page Break\s*-{3,}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# --- Step 2: page headers ---
# New format (born-digital, WP11+):
#   "Deutscher Bundestag – 20. Wahlperiode – 10. Sitzung. Berlin, Mittwoch, den 12. Januar 2022"
RE_HEADER_NEW = re.compile(
    r"^\s*(?:\d{1,5}\*?\s+)?(?:\([ABCD]\))*\s*"
    r"Deutscher\s+Bundestag\s*[–—-]\s*\d{1,2}\s*\.?\s*Wahlperiode.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Old format (OCR'd, WP1-10):
#   "Deutscher Bundestag — 1. Sitzung. Bonn, Mittwoch, den 7. September 1949"
RE_HEADER_OLD = re.compile(
    r"^\s*(?:\d{1,5}\*?\s+)?(?:\([ABCD]\))*\s*"
    r"Deutscher\s+Bundestag\s*[–—-]\s*\d{1,3}\s*\.?\s*Sitzung\..*$",
    re.IGNORECASE | re.MULTILINE,
)

# Parenthesised speaker-attribution tags left on their own line directly
# under old-format headers — column-layout OCR artefacts, not speech.
RE_HEADER_SPEAKER_TAG = re.compile(
    r"^\(\s*[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\s\.]+\)\s*$",
    re.MULTILINE,
)

# --- Step 3: CID character-ID artefacts ---
# pdftotext emits "(cid:N)" when it cannot map a PDF glyph to Unicode.
# The most common is (cid:9) for the tab character, but any numeric ID
# can appear.  Strip the entire token and the surrounding whitespace context
# is then handled by the whitespace normalisation step.
RE_CID = re.compile(r"\(cid:\d+\)")

# --- Step 4: standalone page numbers (extended) ---
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

# --- Step 5: table of contents ---

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

# --- Step 6: footnotes and colophon ---
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

# --- Step 7: column/margin position markers ---
# Isolated "(A)", "(B)", "(C)", "(D)" on their own line or inline.
# Also catches the concatenated form "(A)(C)(B)(D)" found at session ends.
RE_MARGIN_MARKER = re.compile(
    r"(?:^|\s)\([ABCD]\)(?:\s|$)"
    r"|(?:\([ABCD]\)){2,}",        # concatenated form
    re.MULTILINE,
)

# --- Step 8: bare separator lines ---
# Lines consisting solely of one or two hyphen/dash/em-dash characters.
# These are OCR artefacts from the two-column vertical rule.
RE_BARE_SEPARATOR = re.compile(
    r"^\s*[-–—]{1,2}\s*$",
    re.MULTILINE,
)

# --- Step 9: back-matter reference blocks ---
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

# --- Step 10: hyphenation ---
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

# --- Step 11: whitespace normalisation ---
RE_EXCESS_BLANK = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Individual cleaning steps
# ---------------------------------------------------------------------------

def remove_page_breaks(text: str) -> str:
    """Step 1 — remove '--- Page Break ---' markers."""
    return RE_PAGE_BREAK.sub("", text)


def remove_headers(text: str) -> str:
    """Step 2 — remove repeated page headers (old and new format)."""
    text = RE_HEADER_NEW.sub("", text)
    text = RE_HEADER_OLD.sub("", text)
    text = RE_HEADER_SPEAKER_TAG.sub("", text)
    return text


def remove_cid_artefacts(text: str) -> str:
    """Step 3 — strip (cid:N) glyph-mapping failure tokens from pdftotext."""
    return RE_CID.sub("", text)


def remove_page_numbers(text: str) -> str:
    """Step 5 — remove lines that contain only a page/column reference."""
    return RE_PAGE_NUMBER.sub("", text)


def remove_toc(text: str) -> str:
    """
    Step 4 — remove the table of contents block.

    Two-pass anchor search:
      1. Strong anchor  — unambiguous proceedings opener ("Beginn: HH.MM Uhr"
         or "Die Sitzung wird … eröffnet").  These cannot appear inside a ToC.
      2. Speaker anchor — first presidential speaker-colon line preceded by a
         blank line.  Less reliable than the strong anchor but catches sessions
         where the strong opener is absent or differently formatted.

    Within the preamble (everything before the anchor):
      - Lines matching RE_TOC_ENTRY  (dot-leader or wide-space + page ref) are
        removed.
      - Lines matching RE_TOC_HEADING (spaced-letter headings like "I n h a l t")
        are removed.

    Everything from the anchor line onward is left completely untouched.
    If no anchor is found the function returns the text unchanged.
    """
    # Pass 1: strong anchor
    anchor = RE_ANCHOR_STRONG.search(text)

    # Pass 2: fallback speaker anchor
    if anchor is None:
        anchor = RE_ANCHOR_SPEAKER.search(text)

    if anchor is None:
        return text  # no anchor found — play it safe

    anchor_pos = anchor.start()
    preamble = text[:anchor_pos]
    body = text[anchor_pos:]

    cleaned_preamble = RE_TOC_ENTRY.sub("", preamble)
    cleaned_preamble = RE_TOC_HEADING.sub("", cleaned_preamble)
    return cleaned_preamble + body


def remove_footnotes(text: str) -> str:
    """
    Step 6 — remove footnote lines and colophon/printer lines.

    Uses a line-by-line scan so that we never accidentally consume the
    following paragraph when a footnote marker appears mid-text.
    """
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
    """
    Step 7 — remove OCR column/margin markers (A) (B) (C) (D).

    Handles both isolated single markers and the concatenated form
    "(A)(C)(B)(D)" that appears at the end of some born-digital sessions.
    Replaces with a single space to avoid accidentally joining words.
    """
    return RE_MARGIN_MARKER.sub(" ", text)


def remove_bare_separators(text: str) -> str:
    """
    Step 8 — remove lines that consist solely of one or two hyphens/dashes.

    These are OCR artefacts of the vertical column rule in the two-column
    Plenarprotokoll layout.  A single '-' or '–' on its own line is never
    meaningful speech content.
    """
    return RE_BARE_SEPARATOR.sub("", text)


def remove_backm_references(text: str) -> str:
    """
    Step 9 — remove EU/Ratsdokument reference lines from back-matter blocks.

    Back-matter blocks begin with an "Amtliche Mitteilungen" heading and
    contain committee notifications and EU document reference codes.  These
    carry no speech content.

    Strategy: find each "Amtliche Mitteilungen" heading.  From that point
    to the end of the text, remove lines that match the reference-code
    pattern.  Non-matching lines (e.g. descriptive sentences) are kept,
    keeping the step conservative.
    """
    start = RE_BACKM_START.search(text)
    if start is None:
        return text

    preamble = text[:start.start()]
    backm    = text[start.start():]
    cleaned_backm = RE_BACKM_REF.sub("", backm)
    return preamble + cleaned_backm


def rejoin_hyphens(text: str) -> str:
    """
    Step 10 — rejoin words broken by end-of-line hyphenation.

    Two-case logic based on the first character of the continuation word:
      lowercase  -> line-break artefact -> remove hyphen and join
        'Bun-\\ndesrepublik'      ->  'Bundesrepublik'
        'Her-\\n\\nren'              ->  'Herren'
        'Deutsch-\\n\\n\\n\\nland'  ->  'Deutschland'
      uppercase  -> genuine compound hyphen -> keep hyphen, drop newline(s)
        'Baden-\\n\\nWürttemberg' ->  'Baden-Württemberg'
        'CDU-\\n\\nCSU'          ->  'CDU-CSU'

    Uses \\n+ (not \\n{1,2}) because some scanned pages produce 3-4 newlines
    at page boundaries; this step must run before normalise_whitespace so it
    catches all counts before they collapse to \\n\\n.
    Handles both plain hyphens and soft-hyphens (U+00AD).  Fires only when
    both sides of the break are word characters, so legitimate em-dash usage
    is unaffected.
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


# ---------------------------------------------------------------------------
# Full cleaning pipeline
# ---------------------------------------------------------------------------

PIPELINE = [
    ("remove_page_breaks",       remove_page_breaks),
    ("remove_cid_artefacts",     remove_cid_artefacts),   # before headers: some headers have (cid:9) between page-no and "Deutscher"
    ("remove_headers",           remove_headers),
    ("remove_toc",               remove_toc),        # must run BEFORE remove_page_numbers
    ("remove_page_numbers",      remove_page_numbers),  # strips refs ToC step relied on
    ("remove_footnotes",         remove_footnotes),
    ("remove_margin_markers",    remove_margin_markers),
    ("remove_bare_separators",   remove_bare_separators),
    ("remove_backm_references",  remove_backm_references),
    ("normalise_whitespace",     normalise_whitespace),   # strip trailing spaces BEFORE hyphen-join so blank lines with spaces don't hide patterns
    ("rejoin_hyphens",           rejoin_hyphens),
]


def clean(text: str) -> str:
    """Run the full cleaning pipeline on a text string."""
    for _name, fn in PIPELINE:
        text = fn(text)
    return text


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def cleaned_path_for(txt_path: Path) -> Path:
    """
    Given the path to an extracted .txt file, return the path for the
    corresponding _cleaned.txt file in the same folder.

    e.g.  .../WP01/1949/WP01_Session001.txt
          → .../WP01/1949/WP01_Session001_cleaned.txt
    """
    return txt_path.with_name(txt_path.stem + "_cleaned.txt")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(force: bool = False) -> None:
    """
    Walk the preprocessed folder, find all extracted .txt files (those whose
    name does NOT end in '_cleaned'), clean each one, and write the result as
    a sibling _cleaned.txt file.

    Parameters
    ----------
    force : bool
        If True, overwrite existing _cleaned.txt files.
        If False (default), skip files whose _cleaned.txt already exists.
    """
    in_root = Path(PREPROCESSED_FOLDER)

    if not in_root.exists():
        print(f"ERROR: Preprocessed folder not found: {in_root}")
        sys.exit(1)

    log_path = in_root / "cleaning.log"
    done: set[str] = set()
    if not force and log_path.exists():
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    metadata_dir = in_root / "metadata"
    txt_files = sorted(
        p for p in in_root.rglob("*.txt")
        if not p.stem.endswith("_cleaned")
        and metadata_dir not in p.parents
    )

    print(f"Found {len(txt_files)} extracted .txt file(s) under {in_root}")

    ok_count = skip_count = error_count = 0

    for txt_path in txt_files:
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
            with open(log_path, "a", encoding="utf-8") as lg:
                lg.write(str(out_path) + "\n")
            print(f"OK -> {out_path.relative_to(in_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  -> {e}")
            error_count += 1

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")


if __name__ == "__main__":
    # Pass --force as a command-line argument to reprocess already-cleaned files.
    force = "--force" in sys.argv
    if force:
        print("Force mode: existing _cleaned.txt files will be overwritten.\n")
    print("Bundestag text cleaner")
    print(f"Input/Output: {PREPROCESSED_FOLDER}")
    print()
    run(force=force)