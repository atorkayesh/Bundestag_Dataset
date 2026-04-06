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
3.  Remove standalone page numbers (a line that is only digits, optionally
    preceded/followed by whitespace, and optionally followed by a Roman numeral
    prefix such as "II" or "III" used in the table-of-contents pages).
4.  Remove the table of contents block.
    The ToC is identified as the block between the session heading and the
    first substantive speech line. It consists of speaker-name / agenda-item
    lines paired with dot-leaders and page numbers (e.g. "Dr. Müller . . . 42").
    A conservative anchor-based approach is used so that real speech is never
    accidentally removed.
5.  Remove footnote lines.
    Footnotes are lines that start with a footnote marker (*, †, a), b), 1),
    digits followed by ) or .) and contain short explanatory text, OR lines
    that are clearly annex/footer material ("* aufgrund gesetzlichen
    Mutterschutzes", ISSN lines, printer colophon lines, etc.).
6.  Remove column/margin position markers left by OCR: isolated "(A)", "(B)",
    "(C)", "(D)" tokens that appear on their own line or inline.
7.  Rejoin words broken by end-of-line hyphenation (applies to all sessions;
    born-digital PDFs also produce soft-hyphen breaks in the extracted text).
8.  Normalise whitespace: collapse multiple blank lines to a single blank line
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
#   Sometimes followed on the same line by a speaker name in brackets:
#   "Deutscher Bundestag – 20. Wahlperiode – 10. Sitzung. Berlin, ... 473"
RE_HEADER_NEW = re.compile(
    r"^Deutscher\s+Bundestag\s*[–—-]\s*\d{1,2}\.\s*Wahlperiode.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Old format (OCR'd, WP1-10):
#   "Deutscher Bundestag — 1. Sitzung. Bonn, Mittwoch, den 7. September 1949"
#   The OCR sometimes splits this across two lines; we catch both.
RE_HEADER_OLD = re.compile(
    r"^Deutscher\s+Bundestag\s*[–—-]\s*\d{1,3}\.\s*Sitzung\..*$",
    re.IGNORECASE | re.MULTILINE,
)

# Parenthesised speaker continuation tags that appear inline in headers of
# old sessions, e.g. "(Alterspräsident Löbe)" on its own line directly after
# a header — these are column-attribution artefacts, not speech.
RE_HEADER_SPEAKER_TAG = re.compile(
    r"^\(\s*[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\s\.]+\)\s*$",
    re.MULTILINE,
)

# --- Step 3: standalone page numbers ---
# A line that contains only digits (the page number), optionally with a
# Roman-numeral prefix used in ToC pages (I, II, III, IV …).
RE_PAGE_NUMBER = re.compile(
    r"^\s*(?:[IVXivx]{1,5}\s+)?\d{1,4}\s*$",
    re.MULTILINE,
)

# --- Step 4: table of contents ---
# ToC lines are characterised by a dot-leader (two or more ". ") followed by
# a page number at the end.  We collect contiguous blocks of such lines and
# remove the whole block.
RE_TOC_LINE = re.compile(
    r"^.+(?:\.\s{0,2}){3,}\d{1,4}\s*[A-D]?\s*$",
    re.MULTILINE,
)

# Anchor that marks the definitive end of the ToC and the start of the
# proceedings: the session opening line or first substantive speaker.
# We only remove ToC lines that appear BEFORE this anchor.
RE_TOC_ANCHOR = re.compile(
    r"(?:"
    r"Die\s+Sitzung\s+wird\s+(?:um|eröffnet)"   # old: "Die Sitzung wird um … eröffnet"
    r"|Beginn:\s*\d{1,2}[:.]\d{2}\s*Uhr"         # new: "Beginn: 13.00 Uhr"
    r"|(?:Alterspräsident|Präsident(?:in)?)\s+\w+\s*:"  # first presidential speaker line
    r")",
    re.IGNORECASE,
)

# --- Step 5: footnotes ---
# Line-level footnote markers at the start of a line.
RE_FOOTNOTE_MARKER = re.compile(
    r"^\s*(?:"
    r"\*\s+"           # asterisk footnote: "* aufgrund …"
    r"|†\s+"           # dagger
    r"|\d{1,2}[.)]\s+" # numbered: "1) …" or "1. …"
    r"|[a-z][.)]\s+"   # lettered: "a) …"
    r")",
    re.MULTILINE,
)

# Specific known footer/colophon patterns (exact line matches).
RE_COLOPHON = re.compile(
    r"^\s*(?:"
    r"Gesamtherstellung:.*"
    r"|Vertrieb:.*Bundesanzeiger.*"
    r"|ISSN\s+\d[\d\-]+"
    r"|BUB\s+Gebr\."
    r"|Druck\s+und\s+Vertrieb.*"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# --- Step 6: column/margin position markers ---
# Isolated "(A)", "(B)", "(C)", "(D)" — on their own line or surrounded only
# by whitespace.  These are OCR artefacts from the two-column layout.
RE_MARGIN_MARKER = re.compile(
    r"(?:^|\s)\([ABCD]\)(?:\s|$)",
    re.MULTILINE,
)

# --- Step 7: hyphenation ---
# A word ending in a hyphen at end of line, followed by a newline and the
# continuation of the word (lowercase or uppercase continuation).
# We rejoin and drop the hyphen.
# Handles both plain "-\n" and soft-hyphen "\xad\n".
RE_HYPHEN_BREAK = re.compile(
    r"(\w)[-\xad]\n(\w)",
)

# --- Step 8: whitespace normalisation ---
# Three or more consecutive blank lines → one blank line.
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
    # Remove parenthesised speaker attribution tags left on their own line
    # directly under old-format headers.
    text = RE_HEADER_SPEAKER_TAG.sub("", text)
    return text


def remove_page_numbers(text: str) -> str:
    """Step 3 — remove lines that contain only a page number."""
    return RE_PAGE_NUMBER.sub("", text)


def remove_toc(text: str) -> str:
    """
    Step 4 — remove the table of contents block.

    Strategy:
      - Find the position of the ToC anchor (first substantive proceedings
        line).  Everything before the anchor is the 'preamble' zone.
      - Within the preamble, remove all lines that look like ToC entries
        (dot-leader + page number).
      - Leave the anchor line and everything after it untouched.

    If no anchor is found the function returns the text unchanged rather
    than risk removing real speech.
    """
    anchor = RE_TOC_ANCHOR.search(text)
    if anchor is None:
        return text  # no anchor found — play it safe

    anchor_pos = anchor.start()
    preamble = text[:anchor_pos]
    body = text[anchor_pos:]

    cleaned_preamble = RE_TOC_LINE.sub("", preamble)
    return cleaned_preamble + body


def remove_footnotes(text: str) -> str:
    """
    Step 5 — remove footnote lines and colophon/printer lines.

    Footnote lines are removed in full (to end of line).  We use a line-by-line
    approach so that we never accidentally eat into following paragraphs.
    """
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        # Skip known colophon patterns (matched against the full line)
        if RE_COLOPHON.match(line):
            continue
        # Skip lines that start with a footnote marker
        if RE_FOOTNOTE_MARKER.match(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def remove_margin_markers(text: str) -> str:
    """Step 6 — remove OCR column/margin markers (A) (B) (C) (D)."""
    # Replace with a single space to avoid accidentally joining surrounding words.
    return RE_MARGIN_MARKER.sub(" ", text)


def rejoin_hyphens(text: str) -> str:
    """
    Step 7 — rejoin words broken by end-of-line hyphenation.

    'Bun-\\ndesrepublik'  →  'Bundesrepublik'

    This is applied globally (old and new sessions alike).  The regex only
    fires when both sides of the hyphen are word characters, so legitimate
    dashes in compound words that happen to fall at end of line are safe
    as long as the next line starts with a lowercase letter (which is the
    typical continuation pattern).  Uppercase continuations are also handled
    for cases like proper nouns split across lines.
    """
    return RE_HYPHEN_BREAK.sub(r"\1\2", text)


def normalise_whitespace(text: str) -> str:
    """
    Step 8 — strip trailing spaces from every line and collapse runs of
    more than one blank line into a single blank line.
    """
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = RE_EXCESS_BLANK.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Full cleaning pipeline
# ---------------------------------------------------------------------------

PIPELINE = [
    ("remove_page_breaks",   remove_page_breaks),
    ("remove_headers",       remove_headers),
    ("remove_page_numbers",  remove_page_numbers),
    ("remove_toc",           remove_toc),
    ("remove_footnotes",     remove_footnotes),
    ("remove_margin_markers",remove_margin_markers),
    ("rejoin_hyphens",       rejoin_hyphens),
    ("normalise_whitespace", normalise_whitespace),
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

    # Find all extracted .txt files, excluding already-cleaned ones and the
    # metadata subfolder.
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

        # Skip check
        if not force and (str(out_path) in done or out_path.exists()):
            print(f"[SKIP] {txt_path.name}")
            skip_count += 1
            continue

        print(f"[PROC] {txt_path.name} ...", end=" ", flush=True)

        try:
            raw = txt_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"READ ERROR\n  → {e}")
            error_count += 1
            continue

        try:
            cleaned_text = clean(raw)
        except Exception as e:
            print(f"CLEAN ERROR\n  → {e}")
            error_count += 1
            continue

        try:
            out_path.write_text(cleaned_text, encoding="utf-8")
            with open(log_path, "a", encoding="utf-8") as lg:
                lg.write(str(out_path) + "\n")
            print(f"OK → {out_path.relative_to(in_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  → {e}")
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