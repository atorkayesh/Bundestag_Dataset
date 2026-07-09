"""
c_02_find_missed_speakers.py
============================
Scans every _cleaned.txt in the preprocessed corpus and reports lines that
look like speaker attributions but were NOT matched by the current
RE_SPEAKER_LINE / RE_INLINE_SPEAKER patterns in b_02_speaker_identifier.py.

These are "candidate missed speakers" — systematic gaps in the detection
rules that should be fixed in b_02 before re-running the full pipeline.

Output
------
  • Console: top-N unmatched patterns ranked by frequency across the corpus
  • CSV:      every candidate line with WP, session, line number, raw text
  • Log:      per-WP summary counts

Usage
-----
  python src/c_02_find_missed_speakers.py \\
      --preprocessed-dir data/preprocessed \\
      --output-dir outputs \\
      [--top 40] \\
      [--min-freq 2]        # ignore one-off lines
      [--wp 05]             # restrict to a single WP for quick testing
"""

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the *current* speaker-line regexes directly from b_02 so this script
# always stays in sync with whatever patterns are live.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from b_02_speaker_identifier import RE_SPEAKER_LINE, RE_INLINE_SPEAKER

# ---------------------------------------------------------------------------
# Loose "candidate" regex
# ---------------------------------------------------------------------------
# Deliberately broad: catches any line that MIGHT be a speaker attribution.
# False positives are filtered out by checking against RE_SPEAKER_LINE /
# RE_INLINE_SPEAKER matches, and by the IGNORE patterns below.
#
# A candidate line must:
#   1. Start at the beginning of a line (^, MULTILINE)
#   2. Begin with something name-like (uppercase letter, known title, noble
#      particle, or Frau/Herr)
#   3. Be between 5 and 160 characters long
#   4. End with a bare colon at end-of-line  OR  a colon followed immediately
#      by non-whitespace content (inline speech)
# ---------------------------------------------------------------------------
RE_CANDIDATE = re.compile(
    r"^"
    r"(?:"
    r"(?:Frau|Herr)\s+[A-ZÄÖÜ\w]"           # Frau/Herr prefix
    r"|(?:Dr\.|Prof\.|D\.)\s*\S"             # academic title
    r"|(?:von|van|de|zu|vom|zur)\s+[A-ZÄÖÜ]" # noble particle
    r"|[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-]{2,}"       # plain uppercase name (≥3 chars)
    r")"
    r"[^\n]{4,160}"
    r"(?::\s*$|:\s+\S)",                      # ends with ':' or ': text'
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Lines to suppress even when RE_CANDIDATE matches — these are structural
# headings, procedural annotations, or OCR artefacts that end with a colon
# but are not speaker attributions.
# ---------------------------------------------------------------------------
_SUPPRESS = [
    re.compile(r"^(?:Tagesordnung|Drucksache|Anlage|Anmerkung|Übersicht|Tabelle)", re.IGNORECASE),
    re.compile(r"^(?:Wir kommen|Es folgt|Ich rufe|Die Sitzung|Der Bundestag)", re.IGNORECASE),
    re.compile(r"^(?:Ergebnis|Beschluss|Abstimmung|Namentliche)", re.IGNORECASE),
    re.compile(r"\bSeite\s+\d"),           # page references
    re.compile(r"^\d+\.\s"),              # numbered list items  e.g. "1. Beratung:"
    re.compile(r"^—"),                    # dash continuations
    re.compile(r"^\("),                   # interjection lines starting with (
    re.compile(r"Bundestagsdrucksache"),
    re.compile(r"^(?:a|b|c|d)\)"),        # sub-list items  a) b) c)
]


def _suppress(line: str) -> bool:
    """Return True if line should be suppressed (known non-speaker pattern)."""
    stripped = line.strip()
    return any(p.search(stripped) for p in _SUPPRESS)


# ---------------------------------------------------------------------------
# Normalise a candidate line for grouping / display
# ---------------------------------------------------------------------------

def _normalise(line: str) -> str:
    """Collapse runs of whitespace, strip trailing colon and speech text."""
    # Remove everything after the colon (inline speech)
    colon = line.find(":")
    if colon != -1:
        line = line[:colon]
    # Collapse whitespace
    return re.sub(r"\s+", " ", line).strip()


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def scan_session(cleaned_path: Path) -> list[dict]:
    """
    Return a list of candidate missed-speaker dicts for one session.

    Each dict has:
        wp          int   — Wahlperiode number
        session     str   — e.g. "WP05_Session003"
        line_no     int   — 1-based line number in cleaned.txt
        raw         str   — raw matched text (colon + any inline text stripped)
        normalised  str   — collapsed whitespace version (for counting)
    """
    try:
        text = cleaned_path.read_text(encoding="utf-8")
    except Exception:
        return []

    # All positions already matched by the current b_02 patterns.
    # RE_SPEAKER_LINE starts with ^\s* so m.start() may land on a leading \n
    # (blank separator line) rather than on the first letter of the name.
    # We add BOTH the raw start AND the first non-whitespace position so that
    # RE_CANDIDATE (which uses plain ^ and starts on the first letter) is
    # correctly excluded.
    matched_starts: set[int] = set()
    for m in RE_SPEAKER_LINE.finditer(text):
        matched_starts.add(m.start())
        matched_starts.add(m.start() + len(m.group(0)) - len(m.group(0).lstrip()))
    for m in RE_INLINE_SPEAKER.finditer(text):
        matched_starts.add(m.start())
        matched_starts.add(m.start() + len(m.group(0)) - len(m.group(0).lstrip()))

    # Parse WP and session from filename.
    stem = cleaned_path.stem.replace("_cleaned", "")   # e.g. WP05_Session003
    wp_str = stem.split("_")[0].replace("WP", "")
    try:
        wp = int(wp_str)
    except ValueError:
        wp = 0

    # Build a line-start → line-number map for reporting.
    line_starts: list[int] = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def _line_no(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1   # 1-based

    candidates = []
    for m in RE_CANDIDATE.finditer(text):
        if m.start() in matched_starts:
            continue                       # already captured — not missed
        raw = m.group(0)
        if _suppress(raw):
            continue
        norm = _normalise(raw)
        if len(norm) < 5:
            continue
        candidates.append({
            "wp":         wp,
            "session":    stem,
            "line_no":    _line_no(m.start()),
            "raw":        raw.replace("\n", " <NL> "),  # make newlines visible in CSV/console
            "normalised": norm,
        })

    return candidates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Find potential missed speaker attributions.")
    ap.add_argument("--preprocessed-dir", required=True,
                    help="Root of the preprocessed data tree (WP01…WP21 subfolders).")
    ap.add_argument("--output-dir", default=None,
                    help="Directory for CSV output.  Defaults to preprocessed-dir.")
    ap.add_argument("--top", type=int, default=40,
                    help="Number of top unmatched patterns to display (default 40).")
    ap.add_argument("--min-freq", type=int, default=2,
                    help="Only show patterns appearing ≥ this many times (default 2).")
    ap.add_argument("--wp", default=None,
                    help="Restrict scan to a single WP, e.g. --wp 05.")
    args = ap.parse_args()

    pre_dir  = Path(args.preprocessed_dir)
    out_dir  = Path(args.output_dir) if args.output_dir else pre_dir.parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect all cleaned.txt files.
    if args.wp:
        wp_glob = f"WP{args.wp.zfill(2)}"
        cleaned_files = sorted(pre_dir.glob(f"{wp_glob}/**/*_cleaned.txt"))
    else:
        cleaned_files = sorted(pre_dir.glob("WP*/**/*_cleaned.txt"))

    print(f"Scanning {len(cleaned_files)} cleaned session files …\n")

    all_candidates: list[dict] = []
    wp_counts: dict[int, int] = defaultdict(int)

    for i, cf in enumerate(cleaned_files, 1):
        cands = scan_session(cf)
        all_candidates.extend(cands)
        for c in cands:
            wp_counts[c["wp"]] += 1
        if i % 500 == 0:
            print(f"  … {i}/{len(cleaned_files)} files scanned, "
                  f"{len(all_candidates)} candidates so far")

    print(f"\nTotal candidate missed-speaker lines: {len(all_candidates)}")
    print(f"Across {len(wp_counts)} WPs\n")

    # -----------------------------------------------------------------------
    # Per-WP summary
    # -----------------------------------------------------------------------
    print("Per-WP candidate counts:")
    print(f"  {'WP':>4}  {'Candidates':>10}")
    for wp in sorted(wp_counts):
        print(f"  {wp:>4}  {wp_counts[wp]:>10}")

    # -----------------------------------------------------------------------
    # Top patterns by frequency
    # -----------------------------------------------------------------------
    freq: Counter = Counter(c["normalised"] for c in all_candidates)
    top = [(pat, cnt) for pat, cnt in freq.most_common(args.top)
           if cnt >= args.min_freq]

    print(f"\nTop {args.top} unmatched patterns (min frequency {args.min_freq}):")
    print(f"  {'Count':>6}  Pattern")
    print(f"  {'------':>6}  -------")
    for pat, cnt in top:
        display = pat if len(pat) <= 90 else pat[:87] + "…"
        print(f"  {cnt:>6}  {display}")

    # -----------------------------------------------------------------------
    # Show one example line per top pattern
    # -----------------------------------------------------------------------
    print("\nExample lines for top patterns (with WP and session):")
    shown: set[str] = set()
    for pat, cnt in top:
        if pat in shown:
            continue
        shown.add(pat)
        # Find first example
        for c in all_candidates:
            if c["normalised"] == pat:
                raw_display = c["raw"][:110]
                print(f"  [{cnt:>4}x]  {c['session']} line {c['line_no']:>5}: {raw_display}")
                break

    # -----------------------------------------------------------------------
    # Write full CSV
    # -----------------------------------------------------------------------
    csv_path = out_dir / "c_02_missed_speakers.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["wp", "session", "line_no", "raw", "normalised"])
        writer.writeheader()
        writer.writerows(all_candidates)

    print(f"\nFull candidate list written to: {csv_path}")
    print(f"Total rows: {len(all_candidates)}")


if __name__ == "__main__":
    main()
