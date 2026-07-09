"""Extract raw text from Bundestag Plenarprotokoll PDFs.

Outputs per session: WP{wp}_Session{s}.json (full), _metadata.json (no text), .txt (plain text).
Catalogue: preprocessed/metadata/metadata_catalogue.csv  — regenerated every run.
PDF filename convention: WWSSS.pdf → WW = Wahlperiode, SSS = session (e.g. 01061.pdf → WP01/S061).
"""

# defaults — override via --input-dir / --output-dir
_DEFAULT_INPUT  = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\raw"
_DEFAULT_OUTPUT = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"
# ============================================================

import argparse
import csv
import json
import hashlib
import re
import subprocess
from pathlib import Path

from utils import append_log


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

# Expected session-year range (inclusive) for each Wahlperiode.
# Used to reject OCR-corrupted or historically-referenced years that happen
# to match the date regex before the actual session date in the page header.
#
# Ranges are the EXACT calendar years in which each parliament held sessions
# (no buffer).  _try_correct_ocr_year() handles single-digit OCR errors such
# as "1958" → "1957" for a WP02 session, so a buffer is no longer needed.
#
# Adjacent parliaments legitimately share a year (e.g. WP02 and WP03 both
# have sessions in 1957 — WP02 through October, WP03 from November).  Year
# subfolders with the same name in consecutive WPs are expected and correct.
#
# Sources: official Bundestag Wahlperiode dates.
_WP_YEAR_RANGE: dict[int, tuple[int, int]] = {
    1:  (1949, 1953),  # 07 Sep 1949 – 06 Oct 1953
    2:  (1953, 1957),  # 06 Oct 1953 – 15 Oct 1957
    3:  (1957, 1961),  # 15 Oct 1957 – 17 Oct 1961
    4:  (1961, 1965),  # 17 Oct 1961 – 19 Oct 1965
    5:  (1965, 1969),  # 19 Oct 1965 – 20 Oct 1969
    6:  (1969, 1972),  # 20 Oct 1969 – 13 Dec 1972
    7:  (1972, 1976),  # 13 Dec 1972 – 14 Dec 1976
    8:  (1976, 1980),  # 14 Dec 1976 – 04 Nov 1980
    9:  (1980, 1983),  # 04 Nov 1980 – 29 Mar 1983
    10: (1983, 1987),  # 29 Mar 1983 – 18 Jan 1987
    11: (1987, 1990),  # 18 Feb 1987 – 20 Dec 1990
    12: (1991, 1994),  # 17 Jan 1991 – 10 Nov 1994  (first all-Germany parliament)
    13: (1994, 1998),  # 10 Nov 1994 – 26 Sep 1998
    14: (1998, 2002),  # 26 Oct 1998 – 17 Sep 2002
    15: (2002, 2005),  # 17 Oct 2002 – 18 Sep 2005
    16: (2005, 2009),  # 18 Oct 2005 – 27 Sep 2009
    17: (2009, 2013),  # 27 Oct 2009 – 22 Sep 2013
    18: (2013, 2017),  # 22 Oct 2013 – 24 Sep 2017
    19: (2017, 2021),  # 24 Oct 2017 – 26 Oct 2021
    20: (2021, 2025),  # 26 Nov 2021 – 25 Mar 2025
    21: (2025, 2030),  # 25 Apr 2025 – present (upper bound generous)
}


CSV_COLUMNS = [
    "session_id",
    "wp_filename",
    "session_filename",
    "wp_text",
    "session_text",
    "wp",
    "session",
    "date",
    "date_iso",
    "city",
    "year",
    "source_pdf",
    "md5",
    "engine",
    "meta_warnings_count",
    "meta_warnings_detail",
]

# Compiled regexes for metadata extraction

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
#   or  "Bonn , Donnerstag, den 4. Mai 1950"       (OCR: space before comma)
#   or  "Berlin-Charlottenburg, Mittwoch, den 19. Oktober 1955"
#         (Bundestag occasionally met in West Berlin; city suffix after hyphen)
#   or  "Bonn, den 24. und 25. November 1949"
#         (two-day session header — no weekday, "und 25." skipped, first day used)
#
# Design notes:
#   Berlin(?:-\w+)?    — matches "Berlin", "Berlin-Charlottenburg", etc.
#   \s*[,.]?\s+        — handles OCR space before comma ("Bonn ,")
#   \w+[,.]?\s+        — day-of-week token or "den" when no weekday is given
#   (?:den\s+)?        — optional "den" before the day number
#   (?:und\s+\d{1,2}\.\s+)?  — skips "und 25." in two-day ranges (first day kept)
RE_DATE = re.compile(
    r"(Bonn|Berlin(?:-\w+)?)\s*[,.]?\s+\w+[,.]?\s+(?:den\s+)?(\d{1,2})\.\s+"
    r"(?:und\s+\d{1,2}\.\s+)?([A-Za-zÄÖÜäöüß]+)[,\s]+(\d{4})",
    re.IGNORECASE,
)

# Helpers

def _try_correct_ocr_year(bad_year_str: str, lo: int, hi: int) -> str | None:
    """Single-digit substitution to fix OCR year errors (e.g. "1940" → "1949").

    Returns the closest in-range correction, preferring rightmost position on ties.
    """
    if len(bad_year_str) != 4 or not bad_year_str.isdigit():
        return None
    bad_int = int(bad_year_str)
    best: str | None = None
    best_diff = 10_000
    best_pos  = -1
    for pos in range(4):
        orig = bad_year_str[pos]
        for d in "0123456789":
            if d == orig:
                continue
            candidate = bad_year_str[:pos] + d + bad_year_str[pos + 1:]
            try:
                y = int(candidate)
            except ValueError:
                continue
            if lo <= y <= hi:
                diff = abs(y - bad_int)
                # Prefer smallest numeric difference; break ties with rightmost position.
                if diff < best_diff or (diff == best_diff and pos > best_pos):
                    best      = candidate
                    best_diff = diff
                    best_pos  = pos
    return best


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


def _wp_from_path(pdf_path: Path) -> int | None:
    """
    Infer the Wahlperiode number from a PDF path.

    Tries the filename first (Bundestag convention: WWSSS.pdf, e.g.
    19210.pdf → WP 19), then walks the parent directories looking for
    a component like "WP19" or "19".  Returns None if undetermined.
    """
    m = re.match(r"^(\d{2})\d{3}\.pdf$", pdf_path.name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    for part in reversed(pdf_path.parts):
        m2 = re.match(r"(?i:WP)?0*(\d{1,2})$", part)
        if m2:
            wp = int(m2.group(1))
            if 1 <= wp <= 25:
                return wp
    return None


def extract_text_pdftotext(pdf_path: Path) -> str:
    """Extract text via pdftotext (poppler). Returns raw text with pages separated by \f.

    WP01–09 (scanned): uses -raw (content-stream order matches column layout of OCR output).
    WP10+ (born-digital): no -raw flag; pdftotext layout analysis handles two-column correctly.
    """
    wp = _wp_from_path(pdf_path)
    use_raw = (wp is None or wp <= 9)

    cmd = ["pdftotext", "-enc", "UTF-8"]
    if use_raw:
        cmd.append("-raw")
    cmd += [str(pdf_path), "-"]

    result = subprocess.run(
        cmd,
        capture_output=True,
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
    """Fallback extraction engine using pdfminer.six (pure Python, no binary deps)."""
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
    """Try pdftotext, fall back to pdfminer. Returns (text, engine_name)."""
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


def parse_metadata_from_text(text: str, wp: int | None = None) -> dict:
    """Extract wp, session, date, city from document text.

    WP/session from first 3 KB (always in page-1 header); date from full text
    (OCR sessions sometimes have the date header on page 2 or 3).
    When wp is given, date year is validated against _WP_YEAR_RANGE to reject
    OCR-corrupted years and historical references.
    """
    # WP/session identifiers are always in the first page header — 3 KB is enough.
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

    # 4. Date and city — search the FULL document text.
    # In early OCR sessions (WP01–WP09) the date header sometimes appears on
    # page 2 or 3 (after a table of contents or preliminary pages), so limiting
    # to 3 KB caused those sessions to fall into the 'unclassified' subfolder.
    # Scanning the full text is safe: RE_DATE is a linear scan (~5 ms per PDF).
    date = None
    date_iso = None
    city = None
    year = None
    year_out_of_range = False

    lo, hi = _WP_YEAR_RANGE.get(wp, (1, 9999)) if wp is not None else (1, 9999)
    all_matches = list(RE_DATE.finditer(text))
    chosen = None
    if all_matches:
        for candidate in all_matches:
            try:
                y = int(candidate.group(4))
            except ValueError:
                continue
            if lo <= y <= hi:
                chosen = candidate
                break
        if chosen is None:
            # Date matches were found but none has a year in the expected range.
            # Last resort: try a single-digit OCR correction on the first match.
            # This handles corrupted session headers like "1940" → "1949" or
            # "1052" → "1952" where the year digit was misread by the scanner.
            bad_year = all_matches[0].group(4)
            corrected = _try_correct_ocr_year(bad_year, lo, hi)
            if corrected is not None:
                # Re-parse the first match with the corrected year.
                m0 = all_matches[0]
                city_corr = m0.group(1)
                day_corr  = m0.group(2).zfill(2)
                month_name_corr = m0.group(3).lower()
                month_corr = MONTHS.get(month_name_corr)
                if month_corr:
                    date_corr    = f"{day_corr}-{month_corr}-{corrected}"
                    date_iso_corr = f"{corrected}-{month_corr}-{day_corr}"
                    return {
                        "wp_text":           wp_text,
                        "session_text":      session_text,
                        "date":              date_corr,
                        "date_iso":          date_iso_corr,
                        "city":              city_corr,
                        "year":              corrected,
                        "year_out_of_range": False,
                        "year_ocr_corrected": True,
                        "year_raw":          bad_year,
                    }
            # No single-digit correction worked — fall through to 'unclassified'.
            year_out_of_range = True
            return {
                "wp_text":           wp_text,
                "session_text":      session_text,
                "date":              None,
                "date_iso":          None,
                "city":              None,
                "year":              None,
                "year_out_of_range": True,
                "year_raw":          bad_year,
            }

    if chosen is not None:
        city = chosen.group(1)
        day = chosen.group(2).zfill(2)
        month_name = chosen.group(3).lower()
        year = chosen.group(4)
        month = MONTHS.get(month_name)
        if month:
            date = f"{day}-{month}-{year}"
            date_iso = f"{year}-{month}-{day}"

    return {
        "wp_text":           wp_text,
        "session_text":      session_text,
        "date":              date,
        "date_iso":          date_iso,
        "city":              city,
        "year":              year,
        "year_out_of_range": year_out_of_range,
    }


def build_record(pdf_path: Path, in_root: Path) -> dict:
    """Extract text and metadata from one PDF; return the full JSON payload dict."""
    text, engine = extract_text(pdf_path)

    if not text.strip():
        raise RuntimeError("Extraction produced empty text")

    stem = pdf_path.stem
    wp_fn, session_fn = parse_wp_session_from_filename(stem)

    wp_int = int(wp_fn) if wp_fn else _wp_from_path(pdf_path)
    meta = parse_metadata_from_text(text, wp=wp_int)

    warnings = []

    if meta.get("year_ocr_corrected"):
        bad  = meta.get("year_raw", "unknown")
        good = meta.get("year", "unknown")
        warnings.append(
            f"Date year OCR-corrected: {bad!r} → {good!r} "
            f"(single-digit substitution within WP{wp_int} expected range)"
        )

    if meta.get("year_out_of_range"):
        bad = meta.get("year_raw") or meta.get("year") or "unknown"
        yr_range = _WP_YEAR_RANGE.get(wp_int) if wp_int else None
        range_str = f"{yr_range[0]}–{yr_range[1]}" if yr_range else "unknown"
        warnings.append(
            f"Date year {bad!r} is outside expected range "
            f"{range_str} for WP{wp_int}; "
            f"session placed in 'unclassified' subfolder"
        )

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

    wp = meta["wp_text"] or wp_fn
    session = meta["session_text"] or session_fn

    session_id = f"WP{wp or 'unknown'}_Session{session or 'unknown'}"

    try:
        source_pdf = str(pdf_path.relative_to(in_root))
    except ValueError:
        source_pdf = pdf_path.name

    return {
        "session_id":       session_id,
        "wp_filename":    wp_fn,
        "session_filename": session_fn,
        "wp_text":        meta["wp_text"],
        "session_text":   meta["session_text"],
        "wp":             wp,
        "session":        session,
        "date":           meta["date"],
        "date_iso":       meta["date_iso"],
        "city":           meta["city"],
        "year":           meta["year"],
        "source_pdf":     source_pdf,
        "md5":            md5_of_file(pdf_path),
        "engine":         engine,
        "meta_warnings":  warnings,
        "text":           text,
    }


# Output path helpers

def output_paths_for(record: dict, out_root: Path) -> tuple[Path, Path, Path]:
    """Return (out_full.json, out_metadata.json, out.txt) for a session record."""
    wp      = record.get("wp")      or "WPunknown"
    session = record.get("session") or "unknownSession"
    year    = record.get("year")    or "unclassified"

    folder    = out_root / f"WP{wp}" / year
    base_name = f"WP{wp}_Session{session}"

    return (
        folder / f"{base_name}.json",
        folder / f"{base_name}_metadata.json",
        folder / f"{base_name}.txt",
    )


# Write helpers

def write_outputs(record: dict, out_full: Path, out_meta: Path, out_txt: Path) -> None:
    """Write full JSON, metadata-only JSON, and plain-text output for one session."""
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


def record_to_csv_row(record: dict) -> dict:
    """Flatten a metadata record to a CSV row dict; splits meta_warnings into count + JSON detail."""
    warnings = record.get("meta_warnings", [])
    return {
        "session_id":           record.get("session_id")          or "",
        "wp_filename":          record.get("wp_filename")      or "",
        "session_filename":     record.get("session_filename") or "",
        "wp_text":              record.get("wp_text")          or "",
        "session_text":         record.get("session_text")     or "",
        "wp":                   record.get("wp")               or "",
        "session":              record.get("session")          or "",
        "date":                 record.get("date")             or "",
        "date_iso":             record.get("date_iso")         or "",
        "city":                 record.get("city")             or "",
        "year":                 record.get("year")             or "",
        "source_pdf":           record.get("source_pdf")       or "",
        "md5":                  record.get("md5")              or "",
        "engine":               record.get("engine")           or "",
        "meta_warnings_count":  len(warnings),
        "meta_warnings_detail": json.dumps(warnings, ensure_ascii=False),
    }


def rebuild_catalogue(out_root: Path) -> None:
    """Regenerate metadata_catalogue.csv from all *_metadata.json files under out_root."""
    catalogue_dir = out_root / "metadata"
    catalogue_dir.mkdir(parents=True, exist_ok=True)
    catalogue_path = catalogue_dir / "metadata_catalogue.csv"

    # Collect all metadata JSON files, excluding the metadata/ folder itself,
    # sorted for a stable row order (alphabetical ≈ WP then session order).
    meta_files = sorted(
        p for p in out_root.rglob("*_metadata.json")
        if catalogue_dir not in p.parents
    )

    rows = []
    for meta_file in meta_files:
        try:
            record = json.loads(meta_file.read_text(encoding="utf-8"))
            rows.append(record_to_csv_row(record))
        except Exception as e:
            print(f"  [WARN] Could not read {meta_file.name} for catalogue: {e}")

    with open(catalogue_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCatalogue written: {catalogue_path}  ({len(rows)} session(s))")


# Main runner

def run(force: bool = False, wp_filter: set[str] | None = None,
        in_root=None, out_root=None):
    in_root  = Path(in_root  or _DEFAULT_INPUT)
    out_root = Path(out_root or _DEFAULT_OUTPUT)
    out_root.mkdir(parents=True, exist_ok=True)

    # The log tracks the main JSON path only (one line per successfully
    # processed session). A session is skipped when its main JSON path
    # appears in the log OR all three output files already exist on disk.
    log_path = out_root / "processed.log"
    done: set[str] = set()
    if log_path.exists() and not force:
        done = set(log_path.read_text(encoding="utf-8").splitlines())

    pdfs = sorted(in_root.rglob("*.pdf"))

    # Apply WP filter if given
    if wp_filter:
        pdfs = [p for p in pdfs if p.stem[:2] in wp_filter]

    print(f"Found {len(pdfs)} PDF(s) under {in_root}"
          + (f"  [WP filter: {sorted(wp_filter)}]" if wp_filter else "")
          + ("  [--force]" if force else ""))

    ok_count = skip_count = error_count = 0

    for pdf in pdfs:
        # --- Quick preview for skip check (filename only, no text parsing) ---
        preview = {
            "wp":      pdf.stem[:2]  if len(pdf.stem) >= 5 and pdf.stem[:5].isdigit() else None,
            "session": pdf.stem[2:5] if len(pdf.stem) >= 5 and pdf.stem[:5].isdigit() else None,
            "year":    None,
        }
        out_full_preview, out_meta_preview, out_txt_preview = output_paths_for(preview, out_root)

        # Skip if main JSON is already logged OR all three files exist
        # (unless --force is active, in which case always re-process)
        already_logged = str(out_full_preview) in done
        all_exist = (
            out_full_preview.exists()
            and out_meta_preview.exists()
            and out_txt_preview.exists()
        )
        if not force and (already_logged or all_exist):
            print(f"[SKIP] {pdf.name}")
            skip_count += 1
            continue

        print(f"[PROC] {pdf.name} ...", end=" ", flush=True)

        try:
            record = build_record(pdf, in_root)
        except Exception as e:
            print(f"ERROR\n  -> {e}")
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

            # Log only the main JSON path (flock-safe for concurrent SLURM jobs)
            append_log(log_path, str(out_full))

            print(f"OK -> {out_full.relative_to(out_root)}")
            ok_count += 1

        except Exception as e:
            print(f"WRITE ERROR\n  -> {e}")
            error_count += 1

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")

    # Always regenerate the catalogue at the end of every run, even if no
    # new PDFs were processed, so the CSV reflects the full current corpus.
    rebuild_catalogue(out_root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bundestag PDF -> JSON extractor"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process PDFs even if output files already exist.",
    )
    parser.add_argument(
        "--wp",
        metavar="NN[,NN,...]",
        help=(
            "Comma-separated list of Wahlperiode numbers to process "
            "(zero-padded or plain, e.g. '01,02,03' or '1,2,3'). "
            "All others are skipped."
        ),
    )
    parser.add_argument(
        "--input-dir",
        default=None,
        metavar="PATH",
        help=f"Directory containing source PDFs. Default: {_DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help=f"Root directory for preprocessed output. Default: {_DEFAULT_OUTPUT}",
    )
    args = parser.parse_args()

    wp_filter: set[str] | None = None
    if args.wp:
        wp_filter = {w.strip().zfill(2) for w in args.wp.split(",")}

    in_dir  = args.input_dir  or _DEFAULT_INPUT
    out_dir = args.output_dir or _DEFAULT_OUTPUT

    print("Bundestag PDF -> JSON extractor")
    print(f"Input:  {in_dir}")
    print(f"Output: {out_dir}")
    print()
    run(force=args.force, wp_filter=wp_filter, in_root=in_dir, out_root=out_dir)