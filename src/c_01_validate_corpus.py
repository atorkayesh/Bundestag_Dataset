"""Quality validation for the full Bundestag Plenarprotokolle corpus.

Reads all *_speakers.json files and runs 13 checks (null names, whitespace, header leaks,
sequence gaps, etc.). Saves validation_report.txt and validation_per_wp.csv.
"""

# defaults — override via --preprocessed-dir
_DEFAULT_PREPROCESSED = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# German Bundesländer — a party field containing one of these is misclassified
_BUNDESLAENDER = frozenset({
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
})

RE_MULTI_SPACE  = re.compile(r"  +")          # two or more consecutive spaces
RE_HEADER_LEAK  = re.compile(
    r"Deutscher\s+Bundestag\s*[–—-].*Sitzung\.",
    re.IGNORECASE,
)
RE_CID_LEAK     = re.compile(r"\(cid:\d+\)")



def validate_session(record: dict) -> dict:
    """
    Run all checks on one session record.
    Returns a dict of issue counts and sets for this session.
    """
    session_id   = record.get("session_id", "unknown")
    stated_count = record.get("speech_count", 0)
    speeches     = record.get("speeches", [])

    issues = {
        "session_id":          session_id,
        "speech_count":        len(speeches),
        "null_names":          0,
        "whitespace_fields":   0,
        "multi_space_fields":  0,
        "party_is_land":       0,
        "empty_body":          0,
        "short_body":          0,
        "header_leak":         0,
        "cid_leak":            0,
        "count_mismatch":      0,
        "sequence_gaps":       0,
        "consecutive_same":    0,
    }

    # Check 9: speech_count field vs actual list length
    if stated_count != len(speeches):
        issues["count_mismatch"] = 1

    # Check 10: sequence contiguity
    seqs = [sp.get("sequence") for sp in speeches]
    expected = list(range(1, len(speeches) + 1))
    if seqs != expected:
        issues["sequence_gaps"] = 1

    prev_name = None
    for sp in speeches:
        name     = sp.get("name") or ""
        role     = sp.get("role") or ""
        party    = sp.get("party") or ""
        location = sp.get("location") or ""
        text     = sp.get("text") or ""

        # Check 1: null/empty name
        if not name.strip():
            issues["null_names"] += 1

        # Check 2: leading/trailing whitespace in any field
        for field_val in (name, role, party, location):
            if field_val and field_val != field_val.strip():
                issues["whitespace_fields"] += 1
                break

        # Check 3: multiple consecutive spaces inside fields
        for field_val in (name, role, party):
            if field_val and RE_MULTI_SPACE.search(field_val):
                issues["multi_space_fields"] += 1
                break

        # Check 4: party value is a Bundesland
        if party and party in _BUNDESLAENDER:
            issues["party_is_land"] += 1

        # Check 5: empty body
        if not text.strip():
            issues["empty_body"] += 1

        # Check 6: short body (< 20 chars, non-empty)
        elif len(text.strip()) < 20:
            issues["short_body"] += 1

        # Check 7: header leak
        if RE_HEADER_LEAK.search(text):
            issues["header_leak"] += 1

        # Check 8: CID artefact leak
        if RE_CID_LEAK.search(text):
            issues["cid_leak"] += 1

        # Check 11: consecutive same speaker
        if name and name == prev_name:
            issues["consecutive_same"] += 1
        prev_name = name

    return issues



def _wp_from_session_id(session_id: str) -> str:
    """Extract WP number from 'WP01_Session001' -> '01'."""
    m = re.match(r"WP(\d+)", session_id)
    return m.group(1).zfill(2) if m else "??"


def aggregate_by_wp(all_issues: list) -> dict:
    """Group session-level results by WP."""
    by_wp = defaultdict(lambda: {
        "sessions": 0, "speeches": 0,
        "null_names": 0, "whitespace_fields": 0, "multi_space_fields": 0,
        "party_is_land": 0, "empty_body": 0, "short_body": 0,
        "header_leak": 0, "cid_leak": 0,
        "count_mismatch": 0, "sequence_gaps": 0, "consecutive_same": 0,
    })
    for iss in all_issues:
        wp = _wp_from_session_id(iss["session_id"])
        by_wp[wp]["sessions"]          += 1
        by_wp[wp]["speeches"]          += iss["speech_count"]
        by_wp[wp]["null_names"]        += iss["null_names"]
        by_wp[wp]["whitespace_fields"] += iss["whitespace_fields"]
        by_wp[wp]["multi_space_fields"]+= iss["multi_space_fields"]
        by_wp[wp]["party_is_land"]     += iss["party_is_land"]
        by_wp[wp]["empty_body"]        += iss["empty_body"]
        by_wp[wp]["short_body"]        += iss["short_body"]
        by_wp[wp]["header_leak"]       += iss["header_leak"]
        by_wp[wp]["cid_leak"]          += iss["cid_leak"]
        by_wp[wp]["count_mismatch"]    += iss["count_mismatch"]
        by_wp[wp]["sequence_gaps"]     += iss["sequence_gaps"]
        by_wp[wp]["consecutive_same"]  += iss["consecutive_same"]
    return by_wp



def _col(value, width=8) -> str:
    """Right-align a value in a fixed-width column."""
    return str(value).rjust(width)


def format_per_wp_table(by_wp: dict) -> str:
    header = (
        f"{'WP':>4}  {'Sessions':>8}  {'Speeches':>9}  "
        f"{'NullName':>8}  {'WSpace':>6}  {'MSpace':>6}  "
        f"{'PartyLand':>9}  {'EmptyBody':>9}  {'ShortBody':>9}  "
        f"{'HdrLeak':>7}  {'CIDLeak':>7}  {'SeqGap':>6}  {'ConsSame':>8}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    totals = defaultdict(int)
    for wp in sorted(by_wp.keys()):
        d = by_wp[wp]
        lines.append(
            f"{wp:>4}  {d['sessions']:>8}  {d['speeches']:>9}  "
            f"{d['null_names']:>8}  {d['whitespace_fields']:>6}  {d['multi_space_fields']:>6}  "
            f"{d['party_is_land']:>9}  {d['empty_body']:>9}  {d['short_body']:>9}  "
            f"{d['header_leak']:>7}  {d['cid_leak']:>7}  {d['sequence_gaps']:>6}  {d['consecutive_same']:>8}"
        )
        for k in d:
            totals[k] += d[k]
    lines.append(sep)
    lines.append(
        f"{'ALL':>4}  {totals['sessions']:>8}  {totals['speeches']:>9}  "
        f"{totals['null_names']:>8}  {totals['whitespace_fields']:>6}  {totals['multi_space_fields']:>6}  "
        f"{totals['party_is_land']:>9}  {totals['empty_body']:>9}  {totals['short_body']:>9}  "
        f"{totals['header_leak']:>7}  {totals['cid_leak']:>7}  {totals['sequence_gaps']:>6}  {totals['consecutive_same']:>8}"
    )
    return "\n".join(lines)


def format_summary_table(all_issues: list, unique_roles: set, unique_parties: set) -> str:
    total_speeches      = sum(i["speech_count"]        for i in all_issues)
    total_sessions      = len(all_issues)
    null_names          = sum(i["null_names"]           for i in all_issues)
    whitespace_fields   = sum(i["whitespace_fields"]    for i in all_issues)
    multi_space         = sum(i["multi_space_fields"]   for i in all_issues)
    party_is_land       = sum(i["party_is_land"]        for i in all_issues)
    empty_body          = sum(i["empty_body"]           for i in all_issues)
    short_body          = sum(i["short_body"]           for i in all_issues)
    header_leak         = sum(i["header_leak"]          for i in all_issues)
    cid_leak            = sum(i["cid_leak"]             for i in all_issues)
    count_mismatch      = sum(i["count_mismatch"]       for i in all_issues)
    sequence_gaps       = sum(i["sequence_gaps"]        for i in all_issues)
    consecutive_same    = sum(i["consecutive_same"]     for i in all_issues)

    rows = [
        ("Total sessions",                          total_sessions,    ""),
        ("Total speeches",                          total_speeches,    ""),
        ("",                                        "",                ""),
        ("1.  Null or empty speaker names",         null_names,        "PASS" if null_names == 0 else "FAIL"),
        ("2.  Leading/trailing whitespace in fields", whitespace_fields, "PASS" if whitespace_fields == 0 else "WARN"),
        ("3.  Multiple spaces inside fields",       multi_space,       "PASS" if multi_space == 0 else "WARN"),
        ("4.  Party field is a Bundesland",         party_is_land,     "PASS" if party_is_land == 0 else "FAIL"),
        ("5.  Empty speech body",                   empty_body,        "INFO"),
        ("6.  Short speech body (< 20 chars)",      short_body,        "INFO"),
        ("7.  Page header in speech body",          header_leak,       "PASS" if header_leak == 0 else "WARN"),
        ("8.  CID artefact in speech body",         cid_leak,          "PASS" if cid_leak == 0 else "WARN"),
        ("9.  speech_count field mismatch",         count_mismatch,    "PASS" if count_mismatch == 0 else "FAIL"),
        ("10. Sequence gaps within sessions",       sequence_gaps,     "PASS" if sequence_gaps == 0 else "FAIL"),
        ("11. Consecutive same-speaker sequences",  consecutive_same,  "INFO"),
        ("",                                        "",                ""),
        ("12. Unique role values",                  len(unique_roles), "INFO"),
        ("13. Unique party values",                 len(unique_parties), "INFO"),
    ]

    width = max(len(r[0]) for r in rows if r[0]) + 2
    lines = []
    lines.append(f"{'Check':<{width}}  {'Count':>8}  {'Status':>6}")
    lines.append("-" * (width + 18))
    for label, count, status in rows:
        if not label:
            lines.append("")
        else:
            lines.append(f"{label:<{width}}  {str(count):>8}  {status:>6}")
    return "\n".join(lines)



def run(preprocessed_dir=None) -> None:
    in_root = Path(preprocessed_dir or _DEFAULT_PREPROCESSED)

    if not in_root.exists():
        print(f"ERROR: Preprocessed folder not found: {in_root}")
        sys.exit(1)

    metadata_dir = in_root / "metadata"
    json_files = sorted(
        p for p in in_root.rglob("*_speakers.json")
        if metadata_dir not in p.parents
    )

    if not json_files:
        print(f"ERROR: No *_speakers.json files found under {in_root}")
        sys.exit(1)

    print(f"Found {len(json_files)} speaker JSON file(s). Validating ...")

    all_issues   = []
    unique_roles   = set()
    unique_parties = set()
    error_files  = []

    for i, jf in enumerate(json_files, 1):
        if i % 100 == 0 or i == len(json_files):
            print(f"  {i}/{len(json_files)} ...", end="\r", flush=True)
        try:
            record = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            error_files.append((jf.name, str(e)))
            continue

        # Collect unique roles and parties across the corpus
        for sp in record.get("speeches", []):
            role  = sp.get("role")
            party = sp.get("party")
            if role:
                unique_roles.add(role)
            if party:
                unique_parties.add(party)

        all_issues.append(validate_session(record))

    print(f"\nValidation complete. {len(all_issues)} sessions processed.")
    if error_files:
        print(f"  WARNING: {len(error_files)} file(s) could not be read:")
        for fname, err in error_files:
            print(f"    {fname}: {err}")

    # Build report text
    by_wp = aggregate_by_wp(all_issues)

    summary = format_summary_table(all_issues, unique_roles, unique_parties)
    per_wp  = format_per_wp_table(by_wp)

    role_list  = "\n".join(f"  {r}" for r in sorted(unique_roles))
    party_list = "\n".join(f"  {p}" for p in sorted(unique_parties))

    report_lines = [
        "=" * 70,
        "BUNDESTAG CORPUS VALIDATION REPORT",
        f"Preprocessed folder: {in_root}",
        "=" * 70,
        "",
        "SUMMARY",
        "-" * 70,
        summary,
        "",
        "PER-WAHLPERIODE BREAKDOWN",
        "-" * 70,
        per_wp,
        "",
        "UNIQUE ROLE VALUES",
        "-" * 70,
        role_list,
        "",
        "UNIQUE PARTY VALUES",
        "-" * 70,
        party_list,
        "",
        "=" * 70,
    ]
    report_text = "\n".join(report_lines)

    # Print to console
    print()
    print(report_text)

    # Save report
    report_path = in_root / "validation_report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\nReport saved to: {report_path}")

    # Save per-WP CSV
    csv_path = in_root / "validation_per_wp.csv"
    csv_cols = [
        "wp", "sessions", "speeches", "null_names", "whitespace_fields",
        "multi_space_fields", "party_is_land", "empty_body", "short_body",
        "header_leak", "cid_leak", "count_mismatch", "sequence_gaps",
        "consecutive_same",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_cols)
        writer.writeheader()
        for wp in sorted(by_wp.keys()):
            row = {"wp": wp}
            row.update(by_wp[wp])
            writer.writerow(row)
    print(f"Per-WP CSV saved to: {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate the full Bundestag corpus speaker JSON files."
    )
    parser.add_argument(
        "--preprocessed-dir",
        default=None,
        metavar="PATH",
        help=f"Root preprocessed folder. Default: {_DEFAULT_PREPROCESSED}",
    )
    args = parser.parse_args()
    run(preprocessed_dir=args.preprocessed_dir)
