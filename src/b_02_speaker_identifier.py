"""
b_02_speaker_identifier.py
==========================
Identifies speakers in cleaned Bundestag Plenarprotokoll text files and
splits each session into individual speech segments.

Input  : preprocessed/WP{wp}/{year}/WP{wp}_Session{session}_cleaned.txt
Output : preprocessed/WP{wp}/{year}/WP{wp}_Session{session}_speakers.json

Each output JSON has the following structure:
    {
        "session_id": "WP20_Session010",
        "speech_count": 42,
        "speeches": [
            {
                "sequence": 1,
                "speaker_raw": "Präsidentin Bärbel Bas",
                "name": "Bärbel Bas",
                "role": "Präsidentin",
                "party": null,
                "location": null,
                "text": "Guten Tag, liebe Kolleginnen...",
                "char_offset": 1234
            },
            ...
        ]
    }

Speaker attribution patterns (from docs/reqs.txt):
---------------------------------------------------
A.  Chair roles       — Alterspräsident(in)/Präsident(in)/Vizepräsident(in) + name
B.  Member with party — [title] name (location)? (party) [more qualifiers]*
C.  Official role     — name , Bundeskanzler(in)/Bundesminister(in)/
                         Ministerpräsident(in)/Parl. Staatssekretär(in)/
                         Staatsminister(in) [description]

Qualifier parsing rules (docs/reqs.txt):
-----------------------------------------
- Drucksache/Frage references in parentheses are ignored (not party/location).
- German Bundesländer in parentheses are always locations, never parties.
- A single location-only qualifier means party = null (not "fraktionslos").
- Ministerpräsident(in)/Regierender Bürgermeister as a name prefix → role field.
- Inline "Name (Loc) (Party): text..." on same line → split as new speech (Zwischenfrage).

Key rule: the terminal colon on a standalone line is the definitive marker
that what follows is the speaker's own text. Inline interjections use square
brackets [Party] and are enclosed in ( ); they are never matched as speakers.

Design notes
------------
- One pure function per concern; easy to test and extend.
- Regexes compiled once at module level.
- Resume-safe: existing _speakers.json files are skipped unless --force is passed.
- A speakers.log records each successfully processed file.
"""

# ============================================================
# USER SETTINGS
# ============================================================
PREPROCESSED_FOLDER = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"
# ============================================================

import csv
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Party and location reference data
# ---------------------------------------------------------------------------

def _load_known_parties() -> frozenset:
    """Load party abbreviations from the CSV, plus known era-specific variants."""
    csv_path = Path(__file__).parent.parent / "docs" / "political-parties-past-75-years.csv"
    abbrevs = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                abbr = row.get("abbreviation", "").strip()
                if abbr:
                    abbrevs.add(abbr)
    # Era-specific variants not in the CSV
    abbrevs.update({
        "F.D.P.",          # WP10-12 spelling of FDP
        "CDU",             # standalone (without /CSU)
        "CSU",             # standalone
        "GRÜNE",           # early Green party
        "DIE GRÜNEN",      # mid-era Green party
        "GRÜNEN",
        "PDS/Linke Liste", # WP12 variant
        "fraktionslos",    # lowercase used in some eras
        "Fraktionslos",
    })
    return frozenset(abbrevs)


_KNOWN_PARTIES: frozenset = _load_known_parties()

_BUNDESLAENDER: frozenset = frozenset({
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
})

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Optional title prefix: "Frau", "Herr", "Dr.", "Prof. Dr." in any combination.
_TITLE = r"(?:(?:Frau|Herr)\s+)?(?:(?:Prof\.\s+)?Dr\.\s+)?"

# A single name component starting with an uppercase letter.
_NAME_PART = r"[A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-]+"

# Full name: title + at least one name component.
# Single tokens are accepted to cover "Brandt, Bundeskanzler:" style.
_FULL_NAME = rf"{_TITLE}{_NAME_PART}(?:\s+{_NAME_PART})*"

# Ministerial/official role keywords (used in Pattern C).
_OFFICIAL_ROLES = (
    r"Bundes(?:minister(?:in)?|kanzler(?:in)?)"
    r"|Parl\.\s+Staatssekretär(?:in)?"
    r"|Staatssekretär(?:in)?"
    r"|Staatsminister(?:in)?"
    r"|Ministerpräsident(?:in)?"
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
    # Pattern A: chair / presiding officer
    rf"(?P<chair>(?:Alterspräsident(?:in)?|Vizepräsident(?:in)?|Präsident(?:in)?)"
    rf"\s+{_FULL_NAME})"
    rf"|"
    # Pattern B: member with at least one parenthesised qualifier
    rf"(?P<member>{_FULL_NAME}(?:\s+\([^)\n]+\))+)"
    rf"|"
    # Pattern C: name + comma + official role + optional description
    rf"(?P<official>{_FULL_NAME},\s*(?:{_OFFICIAL_ROLES})[^\n:]{{0,120}})"
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
    # Pattern A inline: chair / presiding officer
    rf"(?P<chair_inline>(?:Alterspräsident(?:in)?|Vizepräsident(?:in)?|Präsident(?:in)?)"
    rf"\s+{_FULL_NAME})"
    rf"|"
    # Pattern B inline: member name + one or more parenthesised qualifiers
    rf"(?P<member_inline>{_FULL_NAME}(?:\s+\([^)\n]+\))+)"
    rf"|"
    # Pattern C inline: name + comma + official role
    rf"(?P<official_inline>{_FULL_NAME},\s*(?:{_OFFICIAL_ROLES})[^\n:]{{0,120}})"
    rf"|"
    # Pattern D inline: name + qualifier(s) + comma + parliamentary procedural role
    rf"(?P<member_role_inline>{_FULL_NAME}(?:\s+\([^)\n]+\))+,\s*(?:{_PARLIAMENTARY_ROLES})[^\n:]{{0,80}})"
    rf")"
    rf"\s*:\s*(?=\S)",
    re.MULTILINE,
)

# Used inside parse_speaker to break a chair line into role + name.
RE_CHAIR_PREFIX = re.compile(
    r"^(Alterspräsident(?:in)?|Vizepräsident(?:in)?|Präsident(?:in)?)\s+(.+)$",
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

# A parenthesised qualifier, e.g. "(SPD)" or "(Minden)".
RE_PAREN_QUALIFIER = re.compile(r"\(([^)]+)\)")

# Drucksache / parliamentary reference — always ignore as qualifier.
RE_DRUCKSACHE = re.compile(r"^(?:Drucksache|Frage[n]?\s)", re.IGNORECASE)

# Session ID from filename: "WP20_Session010_cleaned.txt" → "WP20_Session010".
RE_SESSION_ID = re.compile(r"(WP\d+_Session\d+)")


# ---------------------------------------------------------------------------
# Qualifier classifier
# ---------------------------------------------------------------------------

def _classify_qualifiers(qualifiers: list) -> tuple:
    """
    Classify parenthetical qualifier strings into (party, location).

    Rules (in order):
    1. Skip Drucksache / Frage reference strings.
    2. Known Bundesland → location.
    3. Known party abbreviation → party (slash-spacing is normalised before lookup,
       so "CDU / CSU" matches the same as "CDU/CSU").
    4. First remaining ambiguous qualifier → location (city / constituency).
    5. Second remaining ambiguous qualifier → party.

    Returns (party, location) — either may be None.
    """
    party = None
    location = None
    for q in qualifiers:
        q = q.strip()
        if RE_DRUCKSACHE.match(q):
            continue  # parliamentary reference — ignore
        if q in _BUNDESLAENDER:
            location = q
        else:
            # Normalise spaces around "/" so "CDU / CSU" == "CDU/CSU"
            q_norm = re.sub(r'\s*/\s*', '/', q)
            if q_norm in _KNOWN_PARTIES:
                party = q_norm
            elif location is None:
                location = q  # first ambiguous → location (e.g. city)
            else:
                party = q     # second ambiguous → party
    return party, location


# ---------------------------------------------------------------------------
# Speaker line parser
# ---------------------------------------------------------------------------

def parse_speaker(raw: str) -> dict:
    """
    Break a raw speaker attribution string (colon already stripped) into
    structured fields: name, role, party, location.

    Returns a dict with keys: name, role, party, location.
    All values may be None if not determinable.
    """
    text = raw.strip().rstrip(":")

    # Pattern A: chair role prefix
    m = RE_CHAIR_PREFIX.match(text)
    if m:
        return {
            "name": m.group(2).strip(),
            "role": m.group(1),
            "party": None,
            "location": None,
        }

    # Pattern D: name + qualifier(s) + comma + parliamentary procedural role
    # e.g. "Dr. Kather (GB/BHE), Anfragender"
    m = RE_MEMBER_ROLE_SPLIT.match(text)
    if m:
        name_part = m.group(1).strip()
        qualifiers = RE_PAREN_QUALIFIER.findall(m.group(2))
        role_part = m.group(3).strip()
        party, location = _classify_qualifiers(qualifiers)
        return {
            "name": name_part,
            "role": role_part,
            "party": party,
            "location": location,
        }

    # Pattern C: name + comma + official role
    m = RE_OFFICIAL_SPLIT.match(text)
    if m:
        name_part = m.group(1).strip()
        role_part = (m.group(2) + m.group(3)).strip()
        # A "(State)" qualifier may follow the role keyword.
        location = None
        state_m = RE_PAREN_QUALIFIER.search(m.group(3))
        if state_m:
            location = state_m.group(1).strip()
        return {
            "name": name_part,
            "role": role_part,
            "party": None,
            "location": location,
        }

    # Pattern B: name + parenthesised qualifiers.
    # Split at the first "(" to separate base name from qualifiers.
    paren_pos = text.find("(")
    if paren_pos != -1:
        base_name = text[:paren_pos].strip()
        qualifiers = RE_PAREN_QUALIFIER.findall(text[paren_pos:])
        party, location = _classify_qualifiers(qualifiers)

        # Check if base_name starts with a state-level role prefix
        # e.g. "Ministerpräsident Oskar Lafontaine" or "Regierender Bürgermeister Diepgen"
        role_m = RE_STATE_ROLE_PREFIX.match(base_name)
        if role_m:
            return {
                "name": role_m.group(2).strip(),
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
        "name": text,
        "role": None,
        "party": None,
        "location": None,
    }


# ---------------------------------------------------------------------------
# Inline match helper
# ---------------------------------------------------------------------------

def _inline_speaker_raw(m: re.Match) -> str:
    """Extract the speaker attribution string from an RE_INLINE_SPEAKER match."""
    return (
        m.group("chair_inline")
        or m.group("member_inline")
        or m.group("official_inline")
        or m.group("member_role_inline")
        or ""
    ).strip()


# ---------------------------------------------------------------------------
# Speech splitter
# ---------------------------------------------------------------------------

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
        speaker_raw = match.group(0).strip().rstrip(":").strip()

        # Speech body: from end of the attribution line to start of the next one.
        body_start = match.end()
        body_end = primary_matches[i + 1].start() if i + 1 < len(primary_matches) else len(text)
        speech_text = text[body_start:body_end].strip()

        parsed = parse_speaker(speaker_raw)

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
            inline_raw = _inline_speaker_raw(im)
            text_start = im.end()
            text_end = pre_inline[i + 1].start() if i + 1 < len(pre_inline) else len(pre_text)
            inline_body = pre_text[text_start:text_end].strip()
            inline_parsed = parse_speaker(inline_raw)
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
            inline_raw = _inline_speaker_raw(im)
            text_start = im.end()
            text_end = inline_matches[i + 1].start() if i + 1 < len(inline_matches) else len(text)
            inline_body = text[text_start:text_end].strip()
            inline_parsed = parse_speaker(inline_raw)
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
            inline_raw = _inline_speaker_raw(im)
            # Text of this inline speech: from after the colon to the next inline match.
            text_start = im.end()
            text_end = inline_matches[j + 1].start() if j + 1 < len(inline_matches) else len(body)
            inline_body = body[text_start:text_end].strip()

            inline_parsed = parse_speaker(inline_raw)
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
            "text":       sp["text"],
            "char_offset": sp["char_offset"],
        })

    return ordered


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(force: bool = False) -> None:
    """
    Walk the preprocessed folder, process every *_cleaned.txt file, and
    write a sibling *_speakers.json with the identified speeches.

    Parameters
    ----------
    force : bool
        If True, overwrite existing _speakers.json files.
        If False (default), skip files whose _speakers.json already exists.
    """
    in_root = Path(PREPROCESSED_FOLDER)

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
        out_path = speakers_path_for(cleaned_path)

        if not force and (str(out_path) in done or out_path.exists()):
            print(f"[SKIP] {cleaned_path.name}")
            skip_count += 1
            continue

        print(f"[PROC] {cleaned_path.name} ...", end=" ", flush=True)

        try:
            text = cleaned_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"READ ERROR\n  → {e}")
            error_count += 1
            continue

        try:
            speeches = split_speeches(text)
        except Exception as e:
            print(f"PARSE ERROR\n  → {e}")
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
            with open(log_path, "a", encoding="utf-8") as lg:
                lg.write(str(out_path) + "\n")
            print(f"OK  → {len(speeches)} speeches → {out_path.relative_to(in_root)}")
            ok_count += 1
        except Exception as e:
            print(f"WRITE ERROR\n  → {e}")
            error_count += 1

    print(f"\nDone.  OK={ok_count}  Skipped={skip_count}  Errors={error_count}")


if __name__ == "__main__":
    force = "--force" in sys.argv
    if force:
        print("Force mode: existing _speakers.json files will be overwritten.\n")
    print("Bundestag speaker identifier")
    print(f"Input/Output: {PREPROCESSED_FOLDER}")
    print()
    run(force=force)
