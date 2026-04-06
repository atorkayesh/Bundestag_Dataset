"""
bundestag_extractor.py
======================
Extracts raw text from Bundestag Plenarprotokoll PDFs and writes THREE files
per session, all sharing the same folder structure and base name:

  WP{wp}/{year}/WP{wp}_Session{session}.json          ← full record (metadata + text)
  WP{wp}/{year}/WP{wp}_Session{session}_metadata.json ← metadata only (no text field)
  WP{wp}/{year}/WP{wp}_Session{session}.txt           ← plain text only

Design principles
-----------------
- Faithful extraction: no dehyphenation, no reflow, no NLP at this stage.
- Page boundaries preserved in the JSON text field using the form-feed character
  (\f).  In the .txt output, \f is rendered as a human-readable separator
  (\n\n--- Page Break ---\n\n) so the file looks clean in any text editor.
- Metadata extracted from BOTH filename AND document text, cross-validated.
- Warnings emitted (never silent failures) when something looks wrong.
- Resume-safe: a PDF is skipped only when ALL THREE output files already exist
  (or the main JSON path appears in processed.log).
- The processing log records only the main JSON path (one line per session).

Filename convention (Bundestag standard)
-----------------------------------------
  WWSSS.pdf  →  WW = zero-padded Wahlperiode, SSS = zero-padded session
  e.g.  01061.pdf  → WP 01, Session 061
        20146.pdf  → WP 20, Session 146

Output JSON fields (full record)
---------------------------------
  wp_filename     : str        WP extracted from filename (e.g. "01")
  session_filename: str        Session extracted from filename (e.g. "061")
  wp_text         : str|null   WP extracted from document text
  session_text    : str|null   Session number extracted from document text
  wp              : str        Final WP used (text wins if available, else filename)
  session         : str        Final session used (text wins if available, else filename)
  date            : str|null   "DD-MM-YYYY"
  date_iso        : str|null   "YYYY-MM-DD"
  city            : str|null   "Bonn" or "Berlin"
  year            : str|null   "1950", "1988", etc.
  source_pdf      : str        relative path from INPUT_PDF_FOLDER
  md5             : str        hex digest
  engine          : str        "pdftotext" (primary) or "pdfminer" (fallback)
  meta_warnings   : list       any cross-validation issues found
  text            : str        full extracted text, pages separated by \f

The metadata-only JSON contains all fields above EXCEPT "text".
"""

# ============================================================
# USER SETTINGS
# ============================================================
INPUT_PDF_FOLDER = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\raw"
OUTPUT_FOLDER    = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"
# ============================================================

import re
import json
import hashlib
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Month lookup (German → zero-padded month number)
# ---------------------------------------------------------------------------
MONTHS = {
    "januar": "01", "february": "02", "februar": "02",
    "märz": "03", "maerz": "03", "april": "04",
    "mai": "05", "juni": "06", "juli": "07",
    "august": "08", "september": "09", "oktober": "10",
    "november": "11", "dezember": "12",
}

# Page separator used in the plain-text output file.
# The raw \f from pdftotext is preserved as-is in the JSON;
# this string replaces it only in the .txt file.
TXT_PAGE_SEP = "\n\n--- Page Break ---\n\n"

# ---------------------------------------------------------------------------
# Compiled regexes for metadata extraction
# ---------------------------------------------------------------------------

# Modern format (WP11+): "Plenarprotokoll 19/236"
RE_SLUG = re.compile(
    r"Plenarprotokoll\s+(\d+)/(\d+)",
    re.IGNORECASE,
)

# Modern header (WP11+): "11. Wahlperiode"
# (may have spaces from OCR, e.g. "Wahlpe ri ode")
RE_WP = re.compile(
    r"(\d{1,2})\.\s*Wahlpe(?:ri ode|riode|riode)",
    re.IGNORECASE,
)

# Session number from body text
RE_SESSION = re.compile(
    r"(\d+)\.\s*Sitzung",
    re.IGNORECASE,
)

# Date: "Bonn, Donnerstag, den 4. Mai 1950"
#   or  "Berlin, Mittwoch, den 17. Januar 2024"
RE_DATE = re.compile(
    r"(Bonn|Berlin)[,.]?\s+\w+,?\s+(?:den\s+)?(\d{1,2})\.\s+([A-Za-zÄÖÜäöüß]+)[,\s]+(\d{4})",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def md5_of_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_wp_session_from_filename(stem: str):
    """
    '01061' → ('01', '061')
    '20146' → ('20', '146')
    Returns (None, None) if the stem doesn't look right.
    """
    s = stem.strip()
    if len(s) >= 5 and s[:5].isdigit():
        return s[:2], s[2:5]
    return None, None


def extract_text_pdftotext(pdf_path: Path) -> str:
    """
    Primary extraction engine.
    Uses system pdftotext (poppler) which handles the column layout of
    Bundestag PDFs better than pure-Python alternatives.
    Returns the raw text string (pages separated by \f).
    Raises RuntimeError on failure.
    """
    result = subprocess.run(
        ["pdftotext", "-raw", "-enc", "UTF-8", str(pdf_path), "-"],
        capture_output=True,   # capture raw bytes, NOT text=True
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pdftotext failed (rc={result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace')[:200]}"
        )
    # Decode explicitly as UTF-8.
    # Never use text=True here: on Windows the default locale is often
    # cp1252, which would silently corrupt ä/ö/ü/ß into "Ã¤" etc.
    return result.stdout.decode("utf-8")


def extract_text_pdfminer(pdf_path: Path) -> str:
    """
    Fallback extraction engine (pure Python, no binary deps).
    Used when pdftotext is unavailable or crashes.
    """
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        raise RuntimeError(
            "pdfminer.six not installed. Run: pip install pdfminer.six"
        )
    text = extract_text(str(pdf_path))
    if not text:
        raise RuntimeError("pdfminer returned empty text")
    return text


def extract_text(pdf_path: Path) -> tuple[str, str]:
    """
    Try pdftotext first, fall back to pdfminer.
    Returns (text, engine_name).
    """
    try:
        return extract_text_pdftotext(pdf_path), "pdftotext"
    except Exception as e:
        print(f"  [WARN] pdftotext failed on {pdf_path.name}: {e}. Trying pdfminer...")
        try:
            return extract_text_pdfminer(pdf_path), "pdfminer"
        except Exception as e2:
            raise RuntimeError(
                f"Both engines failed on {pdf_path.name}.\n"
                f"  pdftotext: {e}\n  pdfminer:  {e2}"
            )


def parse_metadata_from_text(text: str) -> dict:
    """
    Scan the first ~3 KB of text (covers page 1 and part of page 2,
    which is where all headers appear) for:
      - wp_text, session_text  from Plenarprotokoll XX/YYY slug or Wahlperiode
      - date (DD-MM-YYYY), city, year
    Returns a dict; absent fields are None.
    """
    head = text[:3000]

    wp_text = None
    session_text = None

    # 1. Try slug: "Plenarprotokoll 19/236" — most reliable, WP11+
    m = RE_SLUG.search(head)
    if m:
        wp_text = m.group(1).zfill(2)
        session_text = m.group(2).zfill(3)

    # 2. If no slug, try "19. Wahlperiode" (also covers OCR variant "Wahlpe ri ode")
    if wp_text is None:
        m = RE_WP.search(head)
        if m:
            wp_text = m.group(1).zfill(2)

    # 3. Session from "N. Sitzung" if not yet found from slug
    if session_text is None:
        m = RE_SESSION.search(head)
        if m:
            session_text = m.group(1).zfill(3)

    # 4. Date and city
    date = None
    date_iso = None
    city = None
    year = None

    m = RE_DATE.search(head)
    if m:
        city = m.group(1)  # "Bonn" or "Berlin"
        day = m.group(2).zfill(2)
        month_name = m.group(3).lower()
        year = m.group(4)
        month = MONTHS.get(month_name)
        if month:
            date = f"{day}-{month}-{year}"
            date_iso = f"{year}-{month}-{day}"

    return {
        "wp_text": wp_text,
        "session_text": session_text,
        "date": date,
        "date_iso": date_iso,
        "city": city,
        "year": year,
    }


def build_record(pdf_path: Path, in_root: Path) -> dict:
    """
    Full pipeline for one PDF:
      1. Extract text
      2. Parse metadata from text
      3. Parse WP/session from filename
      4. Cross-validate; build warnings list
      5. Return the full JSON payload dict (including "text")
    """
    # --- Extraction ---
    text, engine = extract_text(pdf_path)

    if not text.strip():
        raise RuntimeError("Extraction produced empty text")

    # --- Metadata from text ---
    meta = parse_metadata_from_text(text)

    # --- Metadata from filename ---
    stem = pdf_path.stem
    wp_fn, session_fn = parse_wp_session_from_filename(stem)

    # --- Cross-validate ---
    warnings = []

    if wp_fn and meta["wp_text"]:
        if wp_fn.lstrip("0") != meta["wp_text"].lstrip("0"):
            warnings.append(
                f"WP mismatch: filename={wp_fn}, text={meta['wp_text']}"
            )

    if session_fn and meta["session_text"]:
        if session_fn.lstrip("0") != meta["session_text"].lstrip("0"):
            warnings.append(
                f"Session mismatch: filename={session_fn}, text={meta['session_text']}"
            )

    if wp_fn is None and meta["wp_text"] is None:
        warnings.append("WP could not be determined from filename or text")

    if session_fn is None and meta["session_text"] is None:
        warnings.append("Session could not be determined from filename or text")

    # --- Choose best values (text wins when available) ---
    wp = meta["wp_text"] or wp_fn
    session = meta["session_text"] or session_fn

    # --- Build relative source path ---
    try:
        source_pdf = str(pdf_path.relative_to(in_root))
    except ValueError:
        source_pdf = pdf_path.name

    return {
        "wp_filename": wp_fn,
        "session_filename": session_fn,
        "wp_text": meta["wp_text"],
        "session_text": meta["session_text"],
        "wp": wp,
        "session": session,
        "date": meta["date"],
        "date_iso": meta["date_iso"],
        "city": meta["city"],
        "year": meta["year"],
        "source_pdf": source_pdf,
        "md5": md5_of_file(pdf_path),
        "engine": engine,
        "meta_warnings": warnings,
        "text": text,
    }


def output_paths_for(record: dict, out_root: Path) -> tuple[Path, Path, Path]:
    """
    Derive all three output paths from a record (or a minimal preview dict).

    Returns:
        out_full     – WP{wp}/{year}/WP{wp}_Session{session}.json
        out_meta     – WP{wp}/{year}/WP{wp}_Session{session}_metadata.json
        out_txt      – WP{wp}/{year}/WP{wp}_Session{session}.txt
    """
    wp      = record.get("wp")      or "WPunknown"
    session = record.get("session") or "unknownSession"
    year    = record.get("year")    or "unclassified"

    folder    = out_root / f"WP{wp}" / year
    base_name = f"WP{wp}_Session{session}"

    out_full = folder / f"{base_name}.json"
    out_meta = folder / f"{base_name}_metadata.json"
    out_txt  = folder / f"{base_name}.txt"

    return out_full, out_meta, out_txt


def write_outputs(record: dict, out_full: Path, out_meta: Path, out_txt: Path) -> None:
    """
    Write all three output files for a single session record.

    out_full  – complete record including the "text" field
    out_meta  – same record with the "text" field removed
    out_txt   – raw text with \f replaced by a human-readable page separator
    """
    # 1. Full JSON (metadata + text)
    out_full.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2. Metadata-only JSON (drop the "text" key)
    meta_record = {k: v for k, v in record.items() if k != "text"}
    out_meta.write_text(
        json.dumps(meta_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. Plain-text file (\f → human-readable page separator)
    plain_text = record["text"].replace("\f", TXT_PAGE_SEP)
    out_txt.write_text(plain_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run():
    in_root  = Path(INPUT_PDF_FOLDER)
    out_root = Path(OUTPUT_FOLDER)
    out_root.mkdir(parents=True, exist_ok=True)

    # The log tracks the main JSON path only (one line per successfully
    # processed session).  A session is skipped when its main JSON path
    # appears in the log OR all three output files already exist on disk.
    log_path = out_root / "processed.log"
    done: set[str] = set()
    if log_path.exists():
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    pdfs = sorted(in_root.rglob("*.pdf"))
    print(f"Found {len(pdfs)} PDF(s) under {in_root}")

    ok_count = skip_count = error_count = 0

    for pdf in pdfs:
        # --- Quick preview for skip check (filename-only, no text parsing) ---
        preview = {
            "wp":      pdf.stem[:2]  if len(pdf.stem) >= 5 and pdf.stem[:5].isdigit() else None,
            "session": pdf.stem[2:5] if len(pdf.stem) >= 5 and pdf.stem[:5].isdigit() else None,
            "year":    None,
        }
        out_full_preview, out_meta_preview, out_txt_preview = output_paths_for(preview, out_root)

        # Skip if the main JSON is already logged OR all three files exist
        already_logged = str(out_full_preview) in done
        all_exist      = (
            out_full_preview.exists()
            and out_meta_preview.exists()
            and out_txt_preview.exists()
        )
        if already_logged or all_exist:
            print(f"[SKIP] {pdf.name}")
            skip_count += 1
            continue

        print(f"[PROC] {pdf.name} ...", end=" ", flush=True)

        try:
            record = build_record(pdf, in_root)
        except Exception as e:
            print(f"ERROR\n  → {e}")
            error_count += 1
            continue

        # Recompute final output paths now that we have full metadata
        out_full, out_meta, out_txt = output_paths_for(record, out_root)
        out_full.parent.mkdir(parents=True, exist_ok=True)

        # Report any metadata warnings
        for w in record["meta_warnings"]:
            print(f"\n  [WARN] {w}", end="")

        try:
            write_outputs(record, out_full, out_meta, out_txt)

            # Log only the main JSON path
            with open(log_path, "a", encoding="utf-8") as lg:
                lg.write(str(out_full) + "\n")

            print(f"OK → {out_full.relative_to(out_root)}")
            ok_count += 1

        except Exception as e:
            print(f"WRITE ERROR\n  → {e}")
            error_count += 1

    print(
        f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}"
    )


if __name__ == "__main__":
    print("Bundestag PDF → JSON extractor")
    print(f"Input:  {INPUT_PDF_FOLDER}")
    print(f"Output: {OUTPUT_FOLDER}")
    print()
    run()