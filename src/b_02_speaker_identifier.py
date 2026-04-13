"""
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

Key rule: the terminal colon on a standalone line is the definitive marker
that what follows is the speaker's own text. Inline interjections use square
brackets [Party] and are enclosed in ( ); they are never matched as speakers.

"""

# preprocessed folder path
PREPROCESSED_FOLDER = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\preprocessed"


import json
import re
import sys
from pathlib import Path

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

# Main speaker-attribution regex.
# Matches a complete line that is a speaker attribution ending with ":".
#
# Pattern A — chair roles (Alterspräsident, Präsident, Vizepräsident)
# Pattern B — member name + one or more parenthesised qualifiers (location / party)
# Pattern C — name + comma + official role (minister, chancellor, state secretary …)
#
# The line must NOT start with "(" (that would be an interjection block).
RE_SPEAKER_LINE = re.compile(
    rf"^(?:"
    # Pattern A: chair / presiding officer
    rf"(?P<chair>(?:Alterspräsident(?:in)?|Vizepräsident(?:in)?|Präsident(?:in)?)"
    rf"\s+{_FULL_NAME})"
    rf"|"
    # Pattern B: member with at least one parenthesised qualifier
    rf"(?P<member>{_FULL_NAME}(?:\s+\([^)\n]+\))+)"
    rf"|"
    # Pattern C: name + comma + official role + optional description
    rf"(?P<official>{_FULL_NAME},\s*(?:{_OFFICIAL_ROLES})[^\n:]{{0,120}})"
    rf")"
    rf"\s*:\s*$",
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

# A parenthesised qualifier, e.g. "(SPD)" or "(Minden)".
RE_PAREN_QUALIFIER = re.compile(r"\(([^)]+)\)")

# Session ID from filename: "WP20_Session010_cleaned.txt" → "WP20_Session010".
RE_SESSION_ID = re.compile(r"(WP\d+_Session\d+)")


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

    # Pattern C: name + comma + official role
    m = RE_OFFICIAL_SPLIT.match(text)
    if m:
        name_part = m.group(1).strip()
        role_part = (m.group(2) + m.group(3)).strip()
        # A "(State)" qualifier may follow the role keyword.
        location = None
        state_m = RE_PAREN_QUALIFIER.search(m.group(3))
        if state_m:
            location = state_m.group(1)
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
        # Convention: last qualifier is the party; second-to-last (if present) is location.
        party = qualifiers[-1] if qualifiers else None
        location = qualifiers[-2] if len(qualifiers) >= 2 else None
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
# Speech splitter
# ---------------------------------------------------------------------------

def split_speeches(text: str) -> list:
    """
    Find all speaker attribution lines in *text* and split the document into
    speech segments.

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
    matches = list(RE_SPEAKER_LINE.finditer(text))

    speeches = []
    for i, match in enumerate(matches):
        speaker_raw = match.group(0).rstrip().rstrip(":").strip()

        # Speech body: from end of the attribution line to start of the next one.
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        speech_text = text[body_start:body_end].strip()

        parsed = parse_speaker(speaker_raw)

        speeches.append({
            "sequence": i + 1,
            "speaker_raw": speaker_raw,
            "name": parsed["name"],
            "role": parsed["role"],
            "party": parsed["party"],
            "location": parsed["location"],
            "text": speech_text,
            "char_offset": match.start(),
        })

    return speeches


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
