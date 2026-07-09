"""Annotate Bundestag corpus speakers with gender.

Resolution order: Stammdaten XML → Frau/Herr honorific → gender_guesser → unknown.
Output: <output_dir>/gender_lookup.csv  (name, gender, source, stammdaten_id, birth_date).
"""

# defaults — override via CLI flags
_DEFAULT_PREPROCESSED = (
    r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics"
    r"\bundestag-project\data\preprocessed"
)
_DEFAULT_STAMMDATEN = None  # auto-resolved from __file__ when None

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# gender_guesser is optional; degrade gracefully if not installed.
try:
    import gender_guesser.detector as _gg_detector
    _GUESSER = _gg_detector.Detector(case_sensitive=False)
except ImportError:
    _GUESSER = None



def _fold(s: str) -> str:
    """Lowercase + fold German umlauts for fuzzy comparison."""
    return (
        s.lower()
        .replace("ä", "a").replace("ö", "o").replace("ü", "u")
        .replace("Ä", "a").replace("Ö", "o").replace("Ü", "u")
        .replace("ß", "ss")
    )


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


# Title prefix regex — stripped before name matching.
# Covers: Frau/Herr, Prof., Dr., D., Dr. h. c., Dr. h. c. mult., Dr.-Ing. etc.
_RE_TITLE = re.compile(
    r"^(?:(?:Frau|Herr)\s+)?"
    r"(?:Prof\.\s+)?"
    r"(?:(?:Dr\.(?:\s*-[A-Za-z]+\.)?(?:\s+[Hh]\.\s+[Cc]\.(?:\s+mult\.)?)?\s+"
    r"|D\.\s+))*",
)

# Optional German nobility particle before surname.
_NOBLE = re.compile(
    r"^(?:von\s+der|van\s+der|von|van|de|zu|vom|zur)\s+", re.IGNORECASE
)


def _strip_title(name: str) -> str:
    """Remove title prefix from a transcript name string."""
    return _RE_TITLE.sub("", name).strip()


def _parse_name(name: str) -> tuple[str, str]:
    """
    Split a transcript name into (firstname_token, surname_token).

    Strategy:
    - Strip title prefix (Frau/Herr, Dr., Prof., etc.).
    - Strip leading noble particle ("van", "von", "de", "zu", …) — handles
      names like "van Scherpenberg" where the particle would otherwise be
      taken as the first-name token.
    - If more than 4 tokens remain, truncate to the first 2 — prevents
      name-leakage artefacts (e.g. "Andrea Lindholz Cansu Özdemir (Die Linke)")
      from using garbage text as the surname lookup key.
    - If multiple tokens remain: first token = given name, last = surname.
    - If one token: surname = that token, firstname = "".

    Returns lowercase-folded (firstname, surname) for comparison.
    """
    clean = _strip_title(name)
    if not clean:
        return "", ""

    # Strip leading noble particle (e.g. "van Scherpenberg" → "Scherpenberg").
    clean = _NOBLE.sub("", clean).strip()
    if not clean:
        return "", ""

    tokens = clean.split()
    if not tokens:
        return "", ""

    # Truncate garbled names with leaked text (>4 tokens → keep first 2 only).
    # Preserves "Firstname Surname" even when the rest of the string is garbage.
    if len(tokens) > 4:
        tokens = tokens[:2]

    if len(tokens) == 1:
        return "", _fold(tokens[0])

    # Last token = surname candidate; first token = given name candidate.
    surname_raw = tokens[-1]
    firstname_raw = tokens[0]

    return _fold(firstname_raw), _fold(surname_raw)



def parse_stammdaten(xml_path: Path) -> dict:
    """Parse Bundestag Stammdaten XML → {folded_surname: [records]} for gender lookup."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    lookup: dict[str, list] = defaultdict(list)

    for mdb in root.findall(".//MDB"):
        # MdB identifier (direct child of <MDB>)
        id_el  = mdb.find("ID")
        mdb_id = id_el.text.strip() if (id_el is not None and id_el.text) else None

        # Birth date (DD.MM.YYYY → YYYY-MM-DD ISO)
        birth_el   = mdb.find(".//GEBURTSDATUM")
        birth_date = None
        if birth_el is not None and birth_el.text:
            raw    = birth_el.text.strip()
            parts  = raw.split(".")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                birth_date = f"{parts[2]}-{parts[1]}-{parts[0]}"

        # Gender
        g_el = mdb.find(".//GESCHLECHT")
        if g_el is None or not g_el.text:
            continue
        g_text = g_el.text.strip().lower()
        if g_text in ("männlich", "maennlich", "m"):
            gender = "male"
        elif g_text in ("weiblich", "w"):
            gender = "female"
        else:
            continue

        # All name records for this MDB (a person may have changed name).
        for name_el in mdb.findall(".//NAME"):
            nachname = (name_el.findtext("NACHNAME") or "").strip()
            vorname  = (name_el.findtext("VORNAME")  or "").strip()
            if not nachname:
                continue
            nf = _fold(nachname)
            # First token of Vorname is used for disambiguation.
            vf = _fold(vorname.split()[0]) if vorname else ""
            lookup[nf].append({
                "nachname":       nachname,
                "vorname_folded": vf,
                "gender":         gender,
                "stammdaten_id":  mdb_id,
                "birth_date":     birth_date,
            })

    return dict(lookup)



def resolve_gender(
    name: str,
    stammdaten: dict,
    guesser,
) -> tuple[str, str, str | None, str | None]:
    """Return (gender, source, stammdaten_id, birth_date) for a speaker name.

    Order: Frau/Herr honorific → exact Stammdaten surname → Levenshtein-1 surname → gender_guesser → unknown.
    """
    # --- 0. Honorific prefix overrides Stammdaten and guesser ---
    _n = name.strip()
    if re.match(r'^Frau\s+', _n, re.IGNORECASE):
        return "female", "title_prefix", None, None
    if re.match(r'^Herr\s+', _n, re.IGNORECASE):
        return "male", "title_prefix", None, None

    firstname_folded, surname_folded = _parse_name(name)

    if not surname_folded:
        return "unknown", "unknown", None, None

    # --- 1. Exact match ---
    candidates = stammdaten.get(surname_folded)
    if candidates:
        rec = _disambiguate(candidates, firstname_folded)
        if rec:
            return rec["gender"], "stammdaten", rec.get("stammdaten_id"), rec.get("birth_date")
        # Exact match exists but is gender-ambiguous (same surname, both M+F).
        # Do NOT fall through to Levenshtein — a Lev-1 match would be a
        # *different* surname (one edit away), which is the wrong person.
        # Skip straight to gender_guesser.
    else:
        # --- 2. Levenshtein ≤ 1 on surname (only when no exact match) ---
        for key, cands in stammdaten.items():
            if key == surname_folded:
                continue          # skip exact key (already handled above)
            if abs(len(key) - len(surname_folded)) > 1:
                continue          # fast length gate
            if _levenshtein(key, surname_folded) <= 1:
                rec = _disambiguate(cands, firstname_folded)
                if rec:
                    return rec["gender"], "stammdaten", rec.get("stammdaten_id"), rec.get("birth_date")

    # --- 3. gender_guesser on first name ---
    if guesser and firstname_folded:
        # Use the original (un-folded) first token for guesser accuracy.
        clean_tokens = _strip_title(name).split()
        first_token = clean_tokens[0] if clean_tokens else ""
        if first_token and len(first_token) > 1:
            result = guesser.get_gender(first_token)
            if result in ("female", "mostly_female"):
                return "female", "inferred", None, None
            if result in ("male", "mostly_male"):
                return "male", "inferred", None, None

    return "unknown", "unknown", None, None


def _disambiguate(candidates: list, firstname_folded: str) -> dict | None:
    """Return matched record if gender is unambiguous; None if same surname maps to both genders."""
    if not candidates:
        return None

    # Try first-name exact match — fully identifies the person.
    if firstname_folded:
        for c in candidates:
            if c["vorname_folded"] == firstname_folded:
                return c

    # All same gender → gender is unambiguous, person identity is not.
    genders = {c["gender"] for c in candidates}
    if len(genders) == 1:
        return {"gender": genders.pop(), "stammdaten_id": None, "birth_date": None}

    return None   # ambiguous: same surname, different genders — cannot resolve



def collect_unique_names(preprocessed_dir: Path) -> set[str]:
    """
    Scan all *_speakers.json files and return the set of unique name strings.
    Names that are None / empty are excluded.
    """
    metadata_dir = preprocessed_dir / "metadata"
    json_files = sorted(
        p for p in preprocessed_dir.rglob("*_speakers.json")
        if metadata_dir not in p.parents
    )
    if not json_files:
        print(f"ERROR: No *_speakers.json files found under {preprocessed_dir}")
        sys.exit(1)

    print(f"Scanning {len(json_files):,} JSON files for unique names ...")
    names: set[str] = set()
    for idx, jf in enumerate(json_files, 1):
        if idx % 500 == 0 or idx == len(json_files):
            print(f"  {idx}/{len(json_files)} ...", end="\r", flush=True)
        try:
            record = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sp in record.get("speeches", []):
            n = sp.get("name")
            if n:
                names.add(n)

    print(f"\nFound {len(names):,} unique speaker names.")
    return names



def run(
    preprocessed_dir: str | None = None,
    stammdaten_path:  str | None = None,
    output_dir:       str | None = None,
) -> None:

    pre_dir = Path(preprocessed_dir or _DEFAULT_PREPROCESSED)
    if not pre_dir.exists():
        print(f"ERROR: Preprocessed folder not found: {pre_dir}")
        sys.exit(1)

    # Resolve Stammdaten path.
    if stammdaten_path:
        xml_path = Path(stammdaten_path)
    else:
        xml_path = Path(__file__).parent.parent / "docs" / "MdB-Stammdaten.xml"

    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = pre_dir.parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Preprocessed  : {pre_dir}")
    print(f"Stammdaten XML: {xml_path}")
    print(f"Output        : {out_dir}")
    print()

    # Load Stammdaten (or warn and continue with empty lookup).
    if xml_path.exists():
        print("Parsing Stammdaten XML ...")
        stammdaten = parse_stammdaten(xml_path)
        print(f"  Loaded {sum(len(v) for v in stammdaten.values()):,} "
              f"name records for {len(stammdaten):,} unique surnames.")
    else:
        print(f"WARNING: Stammdaten XML not found at {xml_path}")
        print("  Proceeding without Stammdaten — all results will be "
              "'inferred' or 'unknown'.")
        print("  Download from: https://www.bundestag.de/resource/blob/"
              "472878/e207ab4b38c93187af608ac8d5ef2b2c/MdB-Stammdaten-data.zip")
        stammdaten = {}

    if _GUESSER is None:
        print("NOTE: gender_guesser not installed; name-inference fallback disabled.")
        print("      pip install gender_guesser>=0.4.0")
    print()

    # Collect unique names from corpus.
    names = collect_unique_names(pre_dir)
    print()

    # Resolve gender (and Stammdaten identity) for each unique name.
    print("Resolving gender ...")
    results: list[tuple[str, str, str, str | None, str | None]] = []
    for name in sorted(names):
        gender, source, stammdaten_id, birth_date = resolve_gender(
            name, stammdaten, _GUESSER
        )
        results.append((name, gender, source, stammdaten_id, birth_date))

    # Write CSV.
    # Columns: name, gender, source, stammdaten_id, birth_date
    #   stammdaten_id — official Bundestag MdB ID (e.g. "11000001"); None for
    #                   non-MdBs and surname-only Stammdaten matches where the
    #                   specific person could not be identified.
    #   birth_date    — ISO 8601 "YYYY-MM-DD"; None for non-MdBs and ambiguous
    #                   same-surname matches.
    out_path = out_dir / "gender_lookup.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "gender", "source", "stammdaten_id", "birth_date"])
        for row in results:
            w.writerow(row)
    print(f"Saved: {out_path}")

    # Coverage summary.
    total = len(results)
    by_source:   dict[str, int] = {}
    by_gender:   dict[str, int] = {}
    n_with_id    = 0
    n_with_birth = 0
    for _, gender, source, stammdaten_id, birth_date in results:
        by_source[source] = by_source.get(source, 0) + 1
        by_gender[gender] = by_gender.get(gender, 0) + 1
        if stammdaten_id:
            n_with_id += 1
        if birth_date:
            n_with_birth += 1

    print()
    print("=== Coverage summary ===")
    for src in ("stammdaten", "title_prefix", "inferred", "unknown"):
        n = by_source.get(src, 0)
        print(f"  {src:12s}: {n:6,}  ({n/total*100:.1f}%)")
    print()
    print("=== Gender distribution ===")
    for g in ("female", "male", "unknown"):
        n = by_gender.get(g, 0)
        print(f"  {g:8s}: {n:6,}  ({n/total*100:.1f}%)")
    print()
    print("=== Stammdaten identity coverage ===")
    print(f"  stammdaten_id: {n_with_id:6,}  ({n_with_id/total*100:.1f}%)"
          "  — person definitively identified in Stammdaten")
    print(f"  birth_date:    {n_with_birth:6,}  ({n_with_birth/total*100:.1f}%)"
          "  — birth date available")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Annotate Bundestag corpus speakers with gender."
    )
    parser.add_argument(
        "--preprocessed-dir", default=None, metavar="PATH",
        help=f"Root preprocessed folder. Default: {_DEFAULT_PREPROCESSED}",
    )
    parser.add_argument(
        "--stammdaten", default=None, metavar="PATH",
        help="Path to MdB-Stammdaten.xml. "
             "Default: docs/MdB-Stammdaten.xml (relative to project root).",
    )
    parser.add_argument(
        "--output-dir", default=None, metavar="PATH",
        help="Output directory for gender_lookup.csv. "
             "Default: <preprocessed>/../outputs",
    )
    args = parser.parse_args()
    run(
        preprocessed_dir=args.preprocessed_dir,
        stammdaten_path=args.stammdaten,
        output_dir=args.output_dir,
    )
