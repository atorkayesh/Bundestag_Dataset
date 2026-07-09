#!/usr/bin/env python3
"""Automated speech-text quality audit for the Bundestag corpus.

Reads speeches.parquet, runs 8 content-quality checks (running headers, CID artefacts,
unrejoined hyphens, attribution leakage, overlength speeches, etc.), and writes a flagged CSV.
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd



# Check 2 — Bundestag page-header text in body
_RE_PAGE_HEADER = re.compile(
    r"Deutscher Bundestag\s*[–\-]\s*\d+\.\s*Wahlperiode",
    re.IGNORECASE,
)

# Check 3 — Unrejoined hyphen line-break: "word-\nlower"
_RE_HYPHEN_ARTIFACT = re.compile(r"\w-\n[a-zäöüß]")

# Check 4 — Attribution-looking line inside body
# Matches "\nFirstname Lastname (PARTY):\n" or "\nFirstname Lastname:\n"
# Two-to-five capitalised tokens, optional party paren, ends with colon.
_RE_ATTRIBUTION_IN_BODY = re.compile(
    r"\n"
    r"[A-ZÄÖÜ][a-zäöüßA-ZÄÖÜ\-]+"          # first token (capitalised)
    r"(?:\s+[A-ZÄÖÜ][a-zäöüßA-ZÄÖÜ\-]+){1,4}"  # 1–4 more capitalised tokens
    r"(?:\s+\([A-ZÄÖÜ][A-ZÄÖÜ/\s]{1,20}\))?"    # optional (PARTY)
    r":\s*\n",
    re.UNICODE,
)

# Check 5 — CID character-ID artefacts
_RE_CID = re.compile(r"\(cid:\d+\)")

# Check 6 — Overlength speeches (> 80 000 chars ≈ ~12 000 words)
_OVERLENGTH_CHARS = 80_000

# Check 8 — Procedure / vote-result text in body.
# Only flag patterns that should NEVER appear inside a speech body:
#   • "Überweisungsvorschlag" — committee referral boilerplate
#   • "Endgültiges Ergebnis Abgegebene" — vote tally header
# Deliberately excluded:
#   • "Erste/Zweite/Dritte Beratung" — used legitimately when members refer to a
#     bill's reading stage (~15 k false positives in earlier version).
#   • "Namentliche Abstimmung" — chairs routinely say "den Antrag auf namentliche
#     Abstimmung" inside their speeches; the phrase on its own is not distinctive
#     enough to flag reliably (~11 k false positives removed by this exclusion).
_RE_PROCEDURE_IN_BODY = re.compile(
    r"(?:^|\n)"
    r"(?:Überweisungsvorschlag"
    r"|Endgültiges\s+Ergebnis\s+Abgegebene"
    r")",
    re.IGNORECASE,
)

# Role prefixes that appear in Kolumnentitel running headers
_ROLE_PREFIXES: tuple[str, ...] = (
    "Alterspräsident ",    "Alterspräsidentin ",
    "Vizepräsident ",      "Vizepräsidentin ",
    "Präsident ",          "Präsidentin ",
    "Bundespräsident ",    "Bundespräsidentin ",
    "Bundeskanzler ",      "Bundeskanzlerin ",
    "Bundesminister ",     "Bundesministerin ",
    "Staatsminister ",     "Staatsministerin ",
    "Parl. Staatssekretär ", "Parl. Staatssekretärin ",
)


def _name_as_standalone_line(name: str, text: str) -> bool:
    """Return True if *name* appears as a standalone line inside *text*.

    Checks both bare name and common role-prefix + name combinations.
    Uses fast Python string containment (no regex) for the bulk check,
    keeping per-row cost to O(len(text)) without regex compilation.
    """
    if not name or not text:
        return False
    candidates = [name]
    for prefix in _ROLE_PREFIXES:
        candidates.append(prefix + name)

    for cand in candidates:
        # Mid-text: blank-line separator before, newline after.
        # We require the double-newline (paragraph boundary) form deliberately —
        # the single-newline form ("\n" + cand + "\n") generates massive false
        # positives because OCR-wrapped lines end at arbitrary column widths and
        # the speaker's name can appear at a line boundary inside a paragraph.
        if ("\n\n" + cand + "\n") in text:
            return True
        # Very start of text (paragraph 1 IS the running header)
        if text.startswith(cand + "\n"):
            return True
    return False


def _starts_with_name(name: str, text: str) -> bool:
    """Return True if *text* starts with the speaker's own name (± role prefix)."""
    if not name or not text:
        return False
    candidates = [name]
    for prefix in _ROLE_PREFIXES:
        candidates.append(prefix + name)
    stripped = text.lstrip()
    for cand in candidates:
        if stripped.startswith(cand + "\n") or stripped == cand:
            return True
    return False


def run_audit(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all checks; return df with boolean flag columns added."""
    text = df["text"].fillna("")
    name = df["name"].fillna("")

    # --- Vectorised checks (fast) ---
    print("  [2] Page-header leakage …",       end=" ", flush=True)
    df["flag_page_header"]       = text.str.contains(_RE_PAGE_HEADER,        na=False)
    print(f"{df['flag_page_header'].sum():,}")

    print("  [3] Unrejoined hyphen artifacts …", end=" ", flush=True)
    df["flag_hyphen_artifact"]   = text.str.contains(_RE_HYPHEN_ARTIFACT,     na=False)
    print(f"{df['flag_hyphen_artifact'].sum():,}")

    print("  [4] Attribution pattern in body …", end=" ", flush=True)
    df["flag_attribution_in_body"] = text.str.contains(_RE_ATTRIBUTION_IN_BODY, na=False)
    print(f"{df['flag_attribution_in_body'].sum():,}")

    print("  [5] CID artifacts …",              end=" ", flush=True)
    df["flag_cid"]               = text.str.contains(_RE_CID,                na=False)
    print(f"{df['flag_cid'].sum():,}")

    print("  [6] Overlength speeches …",         end=" ", flush=True)
    df["flag_overlength"]        = text.str.len() > _OVERLENGTH_CHARS
    print(f"{df['flag_overlength'].sum():,}")

    print("  [8] Procedure text in body …",      end=" ", flush=True)
    df["flag_procedure_in_body"] = text.str.contains(_RE_PROCEDURE_IN_BODY,  na=False)
    print(f"{df['flag_procedure_in_body'].sum():,}")

    # --- Row-wise checks (slower — ~1–2 min for 1 M rows) ---
    print("  [1] Running header in body (row-wise) …", flush=True)
    df["flag_running_header"] = [
        _name_as_standalone_line(n, t)
        for n, t in zip(name, text)
    ]
    print(f"      → {df['flag_running_header'].sum():,}")

    print("  [7] Starts with own name (row-wise) …", flush=True)
    df["flag_starts_with_name"] = [
        _starts_with_name(n, t)
        for n, t in zip(name, text)
    ]
    print(f"      → {df['flag_starts_with_name'].sum():,}")

    return df


_FLAG_COLS = [
    ("flag_running_header",       "1. Running header in body"),
    ("flag_page_header",          "2. Page-header leakage"),
    ("flag_hyphen_artifact",      "3. Unrejoined hyphen artifact"),
    ("flag_attribution_in_body",  "4. Attribution pattern in body"),
    ("flag_cid",                  "5. CID artifact"),
    ("flag_overlength",           "6. Overlength speech"),
    ("flag_starts_with_name",     "7. Starts with own name"),
    ("flag_procedure_in_body",    "8. Procedure text in body"),
]

_TEXT_PREVIEW = 200   # characters of text to show in examples


def _text_preview(text: str) -> str:
    t = str(text or "").strip().replace("\n", "↵")
    return t[:_TEXT_PREVIEW] + ("…" if len(t) > _TEXT_PREVIEW else "")


def print_report(df: pd.DataFrame) -> None:
    total = len(df)
    any_flag = [c for c, _ in _FLAG_COLS]
    flagged_any = df[any_flag].any(axis=1).sum()

    print()
    print("=" * 70)
    print("QUALITY AUDIT REPORT")
    print("=" * 70)
    print(f"Total speeches examined : {total:>10,}")
    print(f"Speeches with ≥1 flag   : {flagged_any:>10,}  ({flagged_any/total:.2%})")
    print()

    for col, label in _FLAG_COLS:
        subset = df[df[col]]
        n = len(subset)
        if n == 0:
            print(f"  {label}")
            print(f"    Count : 0  ✓ CLEAN")
            print()
            continue

        print(f"  {label}")
        print(f"    Count : {n:,}  ({n/total:.2%} of corpus)")

        # WP breakdown
        wp_counts = subset["wp"].value_counts().sort_index()
        wp_str = "  ".join(f"WP{int(wp):02d}:{c}" for wp, c in wp_counts.items())
        print(f"    By WP : {wp_str}")

        # Up to 5 examples
        print(f"    Examples (up to 5):")
        sample = subset.head(5)
        for _, row in sample.iterrows():
            sid   = row.get("speech_id", "?")
            spkr  = row.get("name", "?")
            prev  = _text_preview(row.get("text", ""))
            print(f"      [{sid}] {spkr}")
            print(f"        {prev}")
        print()


# CLI

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automated speech-text quality audit for bundestag_dataset_v1.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dataset-dir", required=True,
        help="Path to bundestag_dataset_v1/ (must contain speeches.parquet).",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory where quality_audit.csv will be written.",
    )
    parser.add_argument(
        "--wp", default=None,
        help="Optional: restrict audit to a single WP (e.g. --wp 18).",
    )
    parser.add_argument(
        "--sample", type=int, default=None,
        help="Optional: audit only a random sample of N speeches (for quick testing).",
    )
    args = parser.parse_args()

    dataset_dir = Path(args.dataset_dir)
    output_dir  = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    speeches_path = dataset_dir / "speeches.parquet"
    if not speeches_path.exists():
        print(f"ERROR: {speeches_path} not found.", file=sys.stderr)
        sys.exit(1)

    # Load — only columns needed for the audit
    print(f"Loading speeches from {speeches_path} …", flush=True)
    cols = ["speech_id", "wp", "session_id", "name", "role", "party", "text"]
    df = pd.read_parquet(speeches_path, columns=cols)
    print(f"  {len(df):,} speeches loaded.")

    if args.wp is not None:
        wp_int = int(args.wp)
        df = df[df["wp"] == wp_int].copy()
        print(f"  Filtered to WP{wp_int:02d}: {len(df):,} speeches.")

    if args.sample is not None:
        df = df.sample(n=min(args.sample, len(df)), random_state=42).copy()
        print(f"  Random sample: {len(df):,} speeches.")

    df = df.reset_index(drop=True)

    # Run checks
    print("\nRunning checks …", flush=True)
    df = run_audit(df)

    # Print report
    print_report(df)

    # Write flagged CSV
    flag_cols = [c for c, _ in _FLAG_COLS]
    flagged = df[df[flag_cols].any(axis=1)].copy()
    # Drop full text from CSV (too large); keep a preview column
    flagged["text_preview"] = flagged["text"].apply(
        lambda t: str(t or "").strip()[:300].replace("\n", "↵")
    )
    out_cols = ["speech_id", "wp", "session_id", "name", "role", "party",
                "text_preview"] + flag_cols
    out_path = output_dir / "quality_audit.csv"
    flagged[out_cols].to_csv(out_path, index=False, encoding="utf-8")
    print(f"Flagged rows written to: {out_path}")
    print(f"  {len(flagged):,} rows flagged across {len(flag_cols)} checks.")


if __name__ == "__main__":
    main()
