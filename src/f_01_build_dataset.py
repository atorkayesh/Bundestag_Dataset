#!/usr/bin/env python3
"""Package the Bundestag corpus into a Zenodo-ready dataset.

Walks preprocessed/ → builds sessions.parquet, speeches.parquet, speakers.parquet,
joins gender annotations, and writes datapackage.json, CITATION.cff, README.md, LICENSE.
"""

import argparse
import json
import re
import shutil
import sys
import textwrap
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pandas as pd


# Role-title gender signals: checked against speaker_raw when Stammdaten + name-inference
# both returned unknown. Female patterns listed first to avoid \bMinister\b matching Ministerin.
_RE_FEMALE_ROLE = re.compile(
    r'\b(?:'
    r'Vizepräsidentin|Alterspräsidentin|Bundespräsidentin|Präsidentin|'
    r'Bundeskanzlerin|'
    r'Bundesministerin|Staatsministerin|Ministerpräsidentin|Ministerin|'
    r'Staatssekretärin|'
    r'Berichterstatterin|Senatorin|Bürgermeisterin'
    r')\b',
    re.IGNORECASE,
)

_RE_MALE_ROLE = re.compile(
    r'\b(?:'
    r'Vizepräsident|Alterspräsident|Bundespräsident|Präsident|'
    r'Bundeskanzler|'
    r'Bundesminister|Staatsminister|Ministerpräsident|Minister|'
    r'Staatssekretär|'
    r'Berichterstatter|Abgeordneter|Senator|Bürgermeister'
    r')\b',
    re.IGNORECASE,
)

# "Freiherr"/"Prinz" in the name field are exclusively male titles, never surnames.
_RE_NOBLE_MALE_NAME = re.compile(
    r'\b(?:Freiherr[n]?|Prinz\b)',
    re.IGNORECASE,
)

# Name cleaning  (name_cleaned column in speakers.parquet + speeches.parquet)
# Strips known prefix/suffix artefacts from the `name` field to produce a
# cleaner version suitable for person-level analysis.  The original `name`
# field is ALWAYS preserved; this is an additive derived column only.
#
# Eight artefact categories handled:
#   1. Procedure-text suffix  — "Überweisungsvorschlag", "Endgültiges Ergebnis" …
#   2. Two-speaker concat     — "Andrea Lindholz Cansu Özdemir (Die Linke)"
#                               → extract first speaker by stripping party-paren
#                                 block and trailing second-name tokens
#   3. Role/preamble prefix   — "Minister", "Ministerin", "Bitte", "Kollege",
#                               "AfD-Fraktion", "Herrn Abgeordneten" …
#   4. Honorific prefix       — "Frau", "Herr"  (rules 3+4 loop until stable,
#                               handling e.g. "Minister Frau Brunn")
#   5. Doubled first token    — "Elisabeth Elisabeth Scharfenberg"
#                               → "Elisabeth Scharfenberg"
#                               ("Dr. Dr." titles are intentionally kept intact)

# Text that leaks into names as suffixes: procedure keywords AND role words.
_RE_PROC_SUFFIX = re.compile(
    r'\s+(?:Überweisungsvorschlag|Endgültiges\s+Ergebnis.*|Berichterstattung|'
    r'Dritte\s+Beratung.*|Ein\s+Schlepplift.*|'
    r'Berichterstatter(?:in)?|Anfragender?).*$',
    re.IGNORECASE | re.DOTALL,
)

# Party abbreviations in parentheses that mark two-speaker concatenations.
_RE_PARTY_PAREN = re.compile(
    r'\s*\((?:Die\s+Linke|AfD|SPD|CDU/CSU|CDU|CSU|'
    r'BÜNDNIS[^)]*|(?:DIE\s+)?GRÜNEN?|FDP|Heidelberg|Köln)\)',
    re.IGNORECASE,
)

# Prefixes stripped in a loop until the name is stable.
# Ordering: more-specific alternatives BEFORE shorter/overlapping ones.
_RE_NAME_PREFIX = re.compile(
    r'^(?:'
    r'AfD-Fraktion\s+|'                        # party prefix (before bare AfD)
    r'AfD\s+|'                                 # bare party prefix
    r'Aktuelle\s+Stunde\s+|'                   # agenda preamble
    r'Bitte\s+|'                               # presiding-officer intro word
    r'Kolleg(?:e|in(?:nen)?|en)?\s+|'         # Kollege / Kollegin / Kollegen
    r'Herrn?\s+Abgeordneten?\s+|'             # "Herrn Abgeordneten X"
    r'Bundesminister(?:in)?\s+|'              # more specific before "Minister"
    r'Ministerpräsident(?:in)?\s+|'           # compound before "Minister"
    r'Staatsminister(?:in)?\s+|'
    r'Minister(?:in)?\s+|'
    r'Staatssekretär(?:in)?\s+|'
    r'Senator(?:in)?\s+|'
    r'Berichterstatter(?:in)?\s+|'
    r'Frau\s+|'                               # honorific — never a name in German
    r'Herr\s+'                                # honorific — never a name in German
    r')',
    re.IGNORECASE,
)


# Kolumnentitel merge fix
# pdftotext's horizontal-band layout sometimes places a presiding officer's
# page-header name (Kolumnentitel) on the same output line as the first speaker
# attribution, producing garbled names like "Petra Pau Wolfgang Wiehle (AfD)"
# or "Wolfgang Kubicki Überweisungsvorschlag".
#
# _fix_kolumnentitel_merges() detects these by checking whether a name starts
# with a known presiding officer's name followed by a space and more text.
# Two outcomes:
#   - Remainder is procedure text  → speech is dropped
#   - Remainder contains a party abbreviation in (…) → re-attribute to the
#     real speaker (name corrected, party backfilled if null)
#
# List is sorted longest-first so "Dr. h. c. Wolfgang Thierse" is checked
# before "Wolfgang Thierse", preventing partial matches.

_PRESIDING_OFFICERS: list[str] = sorted([
    # Bundestagspräsidenten
    "Erich Köhler",
    "Hermann Ehlers",
    "Eugen Gerstenmaier",
    "Kai-Uwe von Hassel",
    "Annemarie Renger",
    "Karl Carstens",
    "Richard Stücklen",
    "Philipp Jenninger",
    "Dr. Rita Süssmuth", "Rita Süssmuth",
    "Johannes Rau",
    "Dr. h. c. Wolfgang Thierse", "Wolfgang Thierse",
    "Dr. Norbert Lammert", "Norbert Lammert",
    "Wolfgang Schäuble",
    "Bärbel Bas",
    # Vizepräsidenten (all Wahlperioden)
    "Dr. Jaeger",
    "Dr. Mommer",
    "Funcke",
    "Schöttle",
    "Westphal",
    "Dr. Süssmuth",
    "Dieter-Julius Cronenberg",
    "Dr. Burkhard Hirsch",
    "Dr. Antje Vollmer", "Antje Vollmer",
    "Dr. h. c. Rudolf Seiters", "Rudolf Seiters",
    "Dr. h. c. Susanne Kastner", "Susanne Kastner",
    "Dr. Hermann Otto Solms", "Hermann Otto Solms",
    "Anke Fuchs",
    "Hans Klein",
    "Hans-Ulrich Klose",
    "Helmuth Becker",
    "Michaela Geiger",
    "Petra Bläss",
    "Renate Schmidt",
    "Eduard Oswald",
    "Gerda Hasselfeldt",
    "Peter Hintze",
    "Petra Pau",
    "Katrin Göring-Eckardt",
    "Thomas Oppermann",
    "Claudia Roth",
    "Wolfgang Kubicki",
    "Yvonne Magwas",
    "Aydan Özoğuz",
    "Andrea Lindholz",
    "Dagmar Ziegler",
    "Josephine Ortleb",
    "Julia Klöckner",
    # Bundesratspräsidenten who occasionally presided over Bundestag sessions
    "Bodo Ramelow",
    # Additional Vizepräsidenten missing from initial list
    "Dr. Wolfgang Schäuble",          # Bundestagspräsident WP19–20 (Dr. prefix in corpus)
    "Dr. Hans-Peter Friedrich",        # Vizepräsident WP19
    "Edelgard Bulmahn",                # Vizepräsidentin WP17–18
    "Dr. h. c. Edelgard Bulmahn",     # same person, honorary-doctorate variant
    "Johannes Singhammer",             # Vizepräsident WP18
    "Ulla Schmidt",                    # Vizepräsidentin WP18–19
    "Omid Nouripour",                  # substitute presiding officer (WP19–20)
    "Michaela Noll",                   # substitute presiding officer
    # OCR variants of already-listed officers
    "Katrin Göhring-Eckardt",          # OCR misread of Katrin Göring-Eckardt
    # ── Older-WP presiding officers (WP01–WP09) ──────────────────────────────
    # These appear with bare surname only (no first name) because early
    # Bundestag transcripts used surname-only attributions.
    "Renger",                           # Annemarie Renger — surname-only form
    "Stücklen",                         # Richard Stücklen — surname-only form
    "Carstens",                         # Karl Carstens — surname-only form
    "Cronenberg",                       # Dieter-Julius Cronenberg — surname-only
    "Wurbs",                            # Richard Wurbs (Vizepräsident WP08–09)
    "Leber",                            # Georg Leber (Vizepräsident WP08–10)
    "Windelen",                         # Heinrich Windelen (Vizepräsident WP09–11)
    "Dr. Schmitt-Vockenhausen",         # Hermann Schmitt-Vockenhausen (Vize WP06–08)
    "von Hassel",                       # Kai-Uwe von Hassel — surname-only form
    # ── "Frau X" honorific-prefixed forms (older WPs) ────────────────────────
    # Early transcripts sometimes include the "Frau" honorific in the
    # page-header Kolumnentitel, so the merge produces "Frau Renger …".
    "Frau Renger",                      # Frau + Annemarie Renger
    "Frau Funcke",                      # Frau + Liselotte Funcke (Vizepräsidentin)
    # (bare "Funcke" is already listed above with other surname-only Vizepräsidenten)
], key=len, reverse=True)

# Patterns that indicate the remainder is procedure/vote text — not a speaker.
_RE_KOLUMNENTITEL_PROC = re.compile(
    r'^(?:'
    r'Überweisungsvorschlag|'
    r'Berichterstattung|'
    r'Berichterstatter(?:in)?|'
    r'Endgültiges\s+Ergebnis|'
    r'Dritte\s+Beratung|'
    r'Zweite\s+Beratung|'
    r'Erste\s+Beratung|'
    r'Vizepräsident(?:in)?|'
    r'Präsident(?:in)?(?:\s+des)?|'
    r'Alterspräsident(?:in)?|'
    r'Tagesordnungspunkt|'
    r'Abstimmung'
    r')',
    re.IGNORECASE,
)

# Party abbreviation in parentheses — presence signals a real speaker attribution.
# Also used to strip the party from the remainder, leaving the clean name.
_RE_KOLUMNENTITEL_PARTY = re.compile(
    r'\s*\((?P<party>'
    r'AfD|SPD|CDU(?:/CSU)?|CSU|FDP|'
    r'Die\s+Linke|DIE\s+LINKE|'
    r'BÜNDNIS(?:\s*90)?/(?:DIE\s+)?GRÜNEN?|'
    r'(?:DIE\s+)?GRÜNEN?'
    r')\)',
    re.IGNORECASE,
)


def _fix_kolumnentitel_merges(speeches_df: pd.DataFrame,
                               verbose: bool = True) -> pd.DataFrame:
    """Fix presiding-officer Kolumnentitel + attribution merges.

    Detects rows where ``name`` starts with a known presiding officer's name
    followed by a space and additional content (a pdftotext horizontal-band
    artefact).  Two outcomes:

    - Remainder matches procedure text  → speech is dropped entirely.
    - Remainder contains a party abbreviation in parentheses → speech is
      re-attributed: ``name`` is updated to the real speaker's name (party
      paren stripped), ``party`` is backfilled if it was null.
    - Remainder matches neither pattern → speech is dropped (conservative).

    Returns the modified DataFrame (index reset).
    """
    drop_indices: list = []
    reattr_log: list[tuple[str, str]] = []  # (original_name, new_name)

    # Normalise whitespace once up front: collapse any run of whitespace
    # (including non-breaking spaces \xa0 common in older WP OCR text) to a
    # single ASCII space so prefix matching works reliably.
    name_normalised = speeches_df["name"].str.replace(r'\s+', ' ', regex=True).str.strip()

    for officer in _PRESIDING_OFFICERS:
        prefix = officer + " "
        # Rows where normalised name starts with "{officer} " and has more content
        mask = (
            name_normalised.str.startswith(prefix, na=False)
            & (name_normalised.str.len() > len(prefix))
        )
        if not mask.any():
            continue

        for idx in speeches_df.index[mask]:
            name = name_normalised.at[idx]          # use normalised form for splitting
            remainder = name[len(prefix):]

            # Procedure / vote text → drop
            if _RE_KOLUMNENTITEL_PROC.match(remainder):
                drop_indices.append(idx)
                continue

            # Real speaker (party abbreviation present) → re-attribute
            party_match = _RE_KOLUMNENTITEL_PARTY.search(remainder)
            if party_match:
                new_party = party_match.group("party")
                # Normalise mixed-case aliases to canonical form
                new_party = {"Die Linke": "DIE LINKE"}.get(new_party, new_party)
                # Strip party paren from remainder to get clean name;
                # keep location parens (e.g. "(Südpfalz)") intact.
                new_name = _RE_KOLUMNENTITEL_PARTY.sub("", remainder).strip()
                if new_name:
                    reattr_log.append((name, new_name))
                    speeches_df.at[idx, "name"] = new_name
                    # Backfill party only if not already set
                    cur = speeches_df.at[idx, "party"]
                    if pd.isna(cur) or str(cur).strip() == "":
                        speeches_df.at[idx, "party"] = new_party
                else:
                    drop_indices.append(idx)
            else:
                # Cannot determine speaker → conservative drop
                drop_indices.append(idx)

    speeches_df = speeches_df.drop(index=drop_indices).reset_index(drop=True)

    if verbose and (reattr_log or drop_indices):
        print(
            f"  Kolumnentitel fix: {len(reattr_log):,} speeches re-attributed "
            f"to correct speaker, {len(drop_indices):,} procedure-text entries "
            f"dropped.", flush=True
        )
        if reattr_log:
            print("  Re-attribution examples (up to 5):", flush=True)
            for orig, new in reattr_log[:5]:
                print(f"    '{orig}'  →  '{new}'", flush=True)
            if len(reattr_log) > 5:
                print(f"    … and {len(reattr_log) - 5} more.", flush=True)

    return speeches_df


def _clean_name(name: str) -> str:
    """Return a cleaned version of a speaker name string.

    Strips prefix/suffix artefacts introduced by b_02 parsing.  The original
    ``name`` field is preserved; this column is for person-level analysis.

    Rules applied in order:
    1. Strip party-in-parens suffix(es) — two-speaker concat marker.
       If ≥ 4 tokens remain, also drop the last 2 (the second speaker's name).
    2. Strip procedure-text suffix (Überweisungsvorschlag, Endgültiges Ergebnis …).
    3. Strip preamble prefix in a loop until stable
       (handles stacked prefixes like "Minister Frau X").
    4. Deduplicate repeated first token ("Elisabeth Elisabeth X" → "Elisabeth X").
       "Dr. Dr." titles are intentionally preserved.
    """
    if not isinstance(name, str) or not name.strip():
        return name

    cleaned = name

    # 1. Two-speaker concat
    n_parens = len(_RE_PARTY_PAREN.findall(cleaned))
    cleaned = _RE_PARTY_PAREN.sub("", cleaned).strip()
    if n_parens > 0:
        tokens = cleaned.split()
        if len(tokens) >= 4:
            cleaned = " ".join(tokens[:-2])

    # 2. Procedure-text suffix
    cleaned = _RE_PROC_SUFFIX.sub("", cleaned).strip()

    # 3. Preamble prefix — loop until no further stripping possible
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _RE_NAME_PREFIX.sub("", cleaned, count=1).strip()

    # 4. Deduplicate first token (preserve legitimate "Dr. Dr." titles)
    tokens = cleaned.split()
    if len(tokens) >= 3 and tokens[0] == tokens[1] and tokens[0].lower() != "dr.":
        cleaned = " ".join(tokens[1:])

    # Fallback: if stripping produced an empty string, keep the original
    return cleaned.strip() or name


# ── optional progress bar (tqdm already in requirements.txt) ──────────────────
try:
    from tqdm import tqdm as _tqdm
    def _progress(iterable, desc="", total=None):
        return _tqdm(iterable, desc=desc, total=total, unit="session")
except ImportError:
    def _progress(iterable, desc="", total=None):
        return iterable


# Helpers

def _make_speech_id(session_id: str, sequence: int) -> str:
    """Stable composite identifier: 'WP01_Session001_0001'.
    Built from the already-unique session_id so collisions are impossible
    even when metadata wp/session fields are malformed."""
    return f"{session_id}_{sequence:04d}"


def _modal(series: pd.Series):
    """Most frequent non-null value in a Series, or None if all null."""
    s = series.dropna()
    return s.mode().iloc[0] if not s.empty else None


def _modal_gender(series: pd.Series) -> str:
    """Most frequent non-unknown gender; 'unknown' if all unknown."""
    known = series[series != "unknown"]
    return str(known.mode().iloc[0]) if not known.empty else "unknown"


def _modal_gender_source(series: pd.Series) -> str:
    """Most authoritative gender source across speeches for one speaker.
    Priority: stammdaten > title_prefix > role_title > inferred > unknown.
    """
    _PRIORITY = {
        "stammdaten":   0,
        "title_prefix": 1,
        "role_title":   2,
        "manual":       3,
        "inferred":     4,
        "unknown":      5,
    }
    unique_sources = series.dropna().unique()
    if len(unique_sources) == 0:
        return "unknown"
    return min(unique_sources, key=lambda x: _PRIORITY.get(x, 99))


def _first_non_null(series: pd.Series):
    """First non-null value in a Series, or None if all null."""
    s = series.dropna()
    return s.iloc[0] if not s.empty else None


def _safe_int(value, default=0) -> int:
    """Convert string or number to int; return default on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


# Stammdaten context disambiguation
# Second-pass logic that uses speech-level context (WP, party, role) to
# identify specific MdBs even when the name-only e_01 lookup was ambiguous
# (same surname, same gender, first-name could not pinpoint the person).
#
# Example: "Renner" alone is ambiguous, but "Renner / KPD / WP1" maps to
# exactly one person in the Stammdaten (Heinz Renner, ID 11001825).
#
# Three context signals, applied in order:
#   1. WP membership  — filter to candidates who actually served in that term
#   2. Party          — if party is not null, filter by PARTEI_KURZ
#   3. Role           — fallback for null-party speakers (Bundesminister,
#                       Präsident, etc.): match role against per-WP INSTITUTIONEN

_RE_TITLE_CTX = re.compile(
    r"^(?:(?:Frau|Herr)\s+)?"
    r"(?:Prof\.\s+)?"
    r"(?:(?:Dr\.(?:\s*-[A-Za-z]+\.)?(?:\s+[Hh]\.\s+[Cc]\.(?:\s+mult\.)?)?\s+|D\.\s+))*"
)
_RE_NOBLE_CTX = re.compile(
    r"^(?:von\s+der|van\s+der|von|van|de|zu|vom|zur)\s+", re.IGNORECASE
)


def _fold_ctx(s: str) -> str:
    """Lowercase + fold German umlauts for Stammdaten key comparison."""
    return (s.lower()
            .replace("ä", "a").replace("ö", "o").replace("ü", "u")
            .replace("Ä", "a").replace("Ö", "o").replace("Ü", "u")
            .replace("ß", "ss"))


def _name_to_parts_ctx(name: str) -> tuple[str, str]:
    """Return (firstname_folded, surname_folded) from a corpus name string.

    Handles hyphenated compound surnames that OCR splits across tokens:
    "Schmitt- Vockenhausen" → surname "schmitt-vockenhausen" (not "vockenhausen").
    A token is treated as a hyphen-prefix fragment when it ends with '-' and is
    followed by a capitalised continuation token; the two are rejoined.
    """
    if not isinstance(name, str) or not name.strip():
        return "", ""
    clean = _RE_TITLE_CTX.sub("", name.strip()).strip()
    clean = _RE_NOBLE_CTX.sub("", clean).strip()
    tokens = clean.split()
    if not tokens:
        return "", ""

    # Rejoin split hyphenated compound tokens: "Schmitt-" + "Vockenhausen" → "Schmitt-Vockenhausen"
    rejoined: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.endswith("-") and i + 1 < len(tokens) and tokens[i + 1][0].isupper():
            rejoined.append(t + tokens[i + 1])
            i += 2
        else:
            rejoined.append(t)
            i += 1
    tokens = rejoined

    if len(tokens) > 4:
        tokens = tokens[:2]
    if len(tokens) == 1:
        return "", _fold_ctx(tokens[0])
    return _fold_ctx(tokens[0]), _fold_ctx(tokens[-1])


def _party_matches_ctx(corpus_party: str | None,
                       stammdaten_party: str | None) -> bool:
    """True if corpus party abbreviation is compatible with Stammdaten PARTEI_KURZ."""
    if not corpus_party or not stammdaten_party:
        return False
    cp = corpus_party.strip().lower()
    sp = stammdaten_party.strip().lower()
    if cp == sp:
        return True
    # CDU/CSU in corpus: the speaker could be a CDU or CSU member
    if cp == "cdu/csu" and sp in ("cdu", "csu"):
        return True
    # Greens: abbreviation changed across eras (GRÜNE → DIE GRÜNEN → BÜNDNIS 90/DIE GRÜNEN)
    _grn = frozenset({"grüne", "die grünen", "bündnis 90/die grünen",
                      "b90/gr", "b90/grüne"})
    _grn_simple = {g.replace(" ", "").replace("/", "") for g in _grn}
    if (cp.replace(" ", "").replace("/", "") in _grn_simple and
            sp.replace(" ", "").replace("/", "") in _grn_simple):
        return True
    # Linke / PDS: same party under different names across WPs
    _linke = frozenset({"die linke", "linke", "pds", "linke liste",
                        "pds/linke liste"})
    if cp in _linke and sp in _linke:
        return True
    return False


def _role_matches_ctx(corpus_role: str, stammdaten_roles: list) -> bool:
    """True if corpus role string corresponds to any Stammdaten INSTITUTIONEN entry.

    Uses substring containment with one edge-case guard:
    "Präsident" must not match "Vizepräsident" (the vize- prefix changes meaning).
    """
    if not corpus_role or not stammdaten_roles:
        return False
    cr = corpus_role.strip().lower()
    for sdt_role in stammdaten_roles:
        sr = sdt_role.strip().lower()
        # Corpus role is a substring of Stammdaten role
        if cr in sr:
            if cr == "präsident" and "vize" in sr:
                continue  # don't let "Präsident" match "Vizepräsident"
            return True
        # Stammdaten role is a substring of the (sometimes more verbose) corpus role
        if sr in cr:
            return True
    return False


def _contextual_disambiguate(
    surname_folded: str,
    firstname_folded: str,
    wp: int | None,
    party: str | None,
    role: str | None,
    context_lookup: dict,
) -> dict | None:
    """Attempt to identify a specific Stammdaten MdB record using speech context.

    Disambiguation pipeline:
    1. All Stammdaten candidates for the surname.
    2. Filter by WP membership (keep those who served in that term).
    3a. Filter by party if party is not null.
    3b. Filter by role if party is null but role is not null — checks each
        candidate's INSTITUTIONEN entries for that WP against the corpus role.
    4. First-name match within the survivors.
    5. Unique survivor → definitive match (returns full record with stammdaten_id).
    6. Multiple survivors, same gender → partial (gender correct, ID still null).
    7. Otherwise → None.
    """
    candidates = context_lookup.get(surname_folded)
    if not candidates:
        return None

    working = list(candidates)

    # Step 2 — WP filter
    if wp is not None:
        wp_filtered = [c for c in working if wp in c.get("wp_set", set())]
        if wp_filtered:          # only apply if at least one candidate remains
            working = wp_filtered

    # Step 3a — party filter
    if party:
        party_filtered = [
            c for c in working
            if _party_matches_ctx(party, c.get("partei_kurz"))
        ]
        if party_filtered:
            working = party_filtered

    # Step 3b — role filter (fallback when party is null)
    elif role:
        role_filtered = []
        for c in working:
            roles_by_wp = c.get("roles_by_wp", {})
            # Collect institution strings for this WP (and ±1 to handle edge cases
            # where a minister's tenure straddles an election boundary)
            role_strings: list[str] = []
            if wp is not None:
                for w in (wp - 1, wp, wp + 1):
                    role_strings.extend(roles_by_wp.get(w, []))
            else:
                for v in roles_by_wp.values():
                    role_strings.extend(v)
            if _role_matches_ctx(role, role_strings):
                role_filtered.append(c)
        if role_filtered:
            working = role_filtered

    # Step 4 — first-name tiebreaker
    if firstname_folded and len(working) > 1:
        fn_filtered = [c for c in working if c.get("vorname_folded") == firstname_folded]
        if fn_filtered:
            working = fn_filtered

    # Step 4b — deduplicate by stammdaten_id (XML creates one record per WAHLPERIODE
    # entry, so the same person can appear 2-3× after filtering; keep first occurrence)
    if len(working) > 1:
        seen_ids: set = set()
        deduped: list = []
        for c in working:
            sid = c.get("stammdaten_id")
            if sid is None or sid not in seen_ids:
                deduped.append(c)
                if sid is not None:
                    seen_ids.add(sid)
        working = deduped

    # Step 5 — unique candidate → definitive
    if len(working) == 1:
        return working[0]

    # Step 6 — multiple candidates, same gender → gender correct but ID uncertain
    genders = {c["gender"] for c in working}
    if len(genders) == 1:
        return {"gender": genders.pop(), "stammdaten_id": None, "birth_date": None}

    return None   # still ambiguous across genders


def _load_stammdaten_context(xml_path: Path) -> dict:
    """Parse the Bundestag Stammdaten XML for contextual (WP/party/role) disambiguation.

    Returns {folded_surname → list of records}.  Each record contains:
      stammdaten_id, vorname_folded, gender, birth_date,
      partei_kurz    — PARTEI_KURZ from BIOGRAFISCHE_ANGABEN
      wp_set         — set of WP ints the MdB served in
      roles_by_wp    — {wp_int: [lowercased INS_LANG strings]} per WP
    """
    tree  = ET.parse(xml_path)
    root  = tree.getroot()
    lookup: dict[str, list] = {}

    def _append(key: str, rec: dict) -> None:
        if key not in lookup:
            lookup[key] = []
        lookup[key].append(rec)

    def _parse_date(raw: str) -> str | None:
        parts = raw.strip().split(".")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        return None

    for mdb in root.findall(".//MDB"):
        id_el  = mdb.find("ID")
        mdb_id = id_el.text.strip() if (id_el is not None and id_el.text) else None

        birth_el   = mdb.find(".//GEBURTSDATUM")
        birth_date = _parse_date(birth_el.text) if (birth_el is not None and birth_el.text) else None

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

        p_el        = mdb.find(".//PARTEI_KURZ")
        partei_kurz = p_el.text.strip() if (p_el is not None and p_el.text) else None

        # Per-WP membership and institution/role strings
        wp_set: set[int]          = set()
        roles_by_wp: dict[int, list[str]] = {}
        for wp_el in mdb.findall(".//WAHLPERIODE"):
            wp_num_el = wp_el.find("WP")
            if wp_num_el is None or not wp_num_el.text:
                continue
            try:
                wp_num = int(wp_num_el.text.strip())
            except ValueError:
                continue
            wp_set.add(wp_num)
            roles: list[str] = []
            for inst_el in wp_el.findall(".//INSTITUTION"):
                ins_lang = inst_el.findtext("INS_LANG", "").strip()
                if ins_lang:
                    roles.append(ins_lang.lower())
            if roles:
                roles_by_wp[wp_num] = roles

        # One lookup entry per name variant (covers name changes / marriage)
        for name_el in mdb.findall(".//NAME"):
            nachname = (name_el.findtext("NACHNAME") or "").strip()
            vorname  = (name_el.findtext("VORNAME")  or "").strip()
            if not nachname:
                continue
            nf = _fold_ctx(nachname)
            vf = _fold_ctx(vorname.split()[0]) if vorname else ""
            _append(nf, {
                "stammdaten_id":  mdb_id,
                "vorname_folded": vf,
                "gender":         gender,
                "birth_date":     birth_date,
                "partei_kurz":    partei_kurz,
                "wp_set":         wp_set,
                "roles_by_wp":    roles_by_wp,
            })

    return lookup


# Corpus loader

def load_corpus(preprocessed_dir: Path, verbose: bool = True):
    """
    Single-pass walk of WP##/YYYY/ subdirectories.
    Loads every *_metadata.json + matching *_speakers.json pair.
    Returns (sessions_rows: list[dict], speeches_rows: list[dict]).
    """
    sessions_rows: list[dict] = []
    speeches_rows: list[dict] = []
    n_missing = 0

    # Collect all metadata files up front so tqdm can show a total
    meta_files = sorted(preprocessed_dir.rglob("*_metadata.json"))

    if verbose:
        print(f"Loading {len(meta_files):,} sessions from {preprocessed_dir} …",
              flush=True)

    for meta_path in _progress(meta_files, desc="Loading", total=len(meta_files)):
        stem    = meta_path.name.replace("_metadata.json", "")
        spk_path = meta_path.parent / f"{stem}_speakers.json"
        if not spk_path.exists():
            n_missing += 1
            continue

        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(spk_path, encoding="utf-8") as fh:
            spk  = json.load(fh)

        wp          = _safe_int(meta.get("wp",      0))
        session_num = _safe_int(meta.get("session", 0))
        year_str    = meta.get("year")
        year        = _safe_int(year_str) if year_str else None
        session_id  = meta.get("session_id")
        date_iso    = meta.get("date_iso")
        city        = meta.get("city")
        # Normalise historical West-Berlin venue name to canonical "Berlin"
        if city == "Berlin-Charlottenburg":
            city = "Berlin"
        # Manual metadata corrections for sessions where b_00 date extraction
        # failed (e.g. fully-CID-encoded PDFs whose date b_00 could not parse).
        # Date/city confirmed from decoded document text.
        _META_CORRECTIONS = {
            "WP14_Session169": {"date_iso": "2001-05-16", "city": "Berlin", "year": 2001},
        }
        if session_id in _META_CORRECTIONS and date_iso is None:
            _corr = _META_CORRECTIONS[session_id]
            date_iso = _corr.get("date_iso", date_iso)
            city     = _corr.get("city",     city)
            year     = _corr.get("year",     year)
        speeches    = spk.get("speeches", [])

        sessions_rows.append({
            "session_id":  session_id,
            "wp":          wp,
            "session_num": session_num,
            "date_iso":    date_iso,
            "city":        city,
            "year":        year,
            "source_pdf":  meta.get("source_pdf"),
            "md5":         meta.get("md5"),
            "engine":      meta.get("engine"),
            "n_speeches":  spk.get("speech_count", len(speeches)),
        })

        for speech in speeches:
            seq = _safe_int(speech.get("sequence", 0))
            speeches_rows.append({
                "speech_id":   _make_speech_id(session_id, seq),
                "session_id":  session_id,
                "sequence":    seq,
                "wp":          wp,
                "date_iso":    date_iso,
                "city":        city,
                "name":        speech.get("name"),
                "speaker_raw": speech.get("speaker_raw"),
                "role":        speech.get("role"),
                "party":       speech.get("party"),
                "location":    speech.get("location"),
                "text":        speech.get("text"),
                "char_offset": speech.get("char_offset"),
            })

    if n_missing and verbose:
        print(f"  WARNING: {n_missing} sessions had no _speakers.json — skipped.",
              flush=True)

    return sessions_rows, speeches_rows


# datapackage.json  (Frictionless Data)

def _make_datapackage(version: str) -> dict:
    return {
        "name":    "bundestag-plenarprotokolle",
        "title":   "Bundestag Plenarprotokolle Dataset",
        "version": version,
        "description": (
            "Structured, speaker-attributed speech corpus from 75 years of German "
            "Bundestag plenary sessions (WP01–WP21, 1949–present). Derived from "
            "official Plenarprotokolle published by the Bundestag under open data "
            "policy. CC-BY 4.0."
        ),
        "licenses": [
            {
                "name":  "CC-BY-4.0",
                "title": "Creative Commons Attribution 4.0 International",
                "path":  "https://creativecommons.org/licenses/by/4.0/"
            }
        ],
        "resources": [
            {
                "name":      "sessions",
                "path":      "sessions.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": "One row per plenary session.",
                "schema": {
                    "primaryKey": ["session_id"],
                    "fields": [
                        {"name": "session_id",  "type": "string",  "description": "Unique session key, e.g. 'WP01_Session001'."},
                        {"name": "wp",          "type": "integer", "description": "Wahlperiode (parliamentary term), 1–21."},
                        {"name": "session_num", "type": "integer", "description": "Session number within the Wahlperiode, 1-based."},
                        {"name": "date_iso",    "type": "date",    "description": "Session date in ISO 8601 format (YYYY-MM-DD)."},
                        {"name": "city",        "type": "string",  "description": "Session city: 'Bonn' (WP01–WP13) or 'Berlin' (WP14–WP21)."},
                        {"name": "year",        "type": "integer", "description": "Calendar year of the session."},
                        {"name": "source_pdf",  "type": "string",  "description": "Filename of the source PDF, e.g. '01001.pdf'."},
                        {"name": "md5",         "type": "string",  "description": "MD5 hash of the source PDF for integrity verification."},
                        {"name": "engine",      "type": "string",  "description": "PDF extraction engine used: 'pdftotext' (born-digital) or 'pdfminer' (OCR fallback). Use this to flag lower-quality sessions."},
                        {"name": "n_speeches",  "type": "integer", "description": "Number of attributed speeches in this session."},
                    ]
                }
            },
            {
                "name":      "speeches",
                "path":      "speeches.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": "One row per attributed speech, including full text.",
                "schema": {
                    "primaryKey": ["speech_id"],
                    "foreignKeys": [
                        {
                            "fields":    ["session_id"],
                            "reference": {"resource": "sessions", "fields": ["session_id"]}
                        }
                    ],
                    "fields": [
                        {"name": "speech_id",    "type": "string",  "description": "Unique speech key: '{session_id}_{sequence}', e.g. 'WP01_Session001_0001'."},
                        {"name": "session_id",   "type": "string",  "description": "Foreign key → sessions.session_id."},
                        {"name": "sequence",     "type": "integer", "description": "1-based order of this speech within the session."},
                        {"name": "wp",           "type": "integer", "description": "Wahlperiode (denormalized from sessions for convenience)."},
                        {"name": "date_iso",     "type": "date",    "description": "Session date (denormalized)."},
                        {"name": "year",         "type": "integer", "description": "Calendar year of the session, derived from date_iso."},
                        {"name": "city",         "type": "string",  "description": "Session city (denormalized)."},
                        {"name": "name",          "type": "string",  "description": "Normalized speaker name as extracted by b_02. May contain prefix/suffix artefacts for ~6.6% of unique names (see name_cleaned)."},
                        {"name": "name_cleaned",  "type": "string",  "description": "Cleaned speaker name with b_02 parsing artefacts removed: role/preamble prefixes (Minister, Kollege, Bitte …), procedure-text suffixes (Überweisungsvorschlag …), two-speaker concatenations, and doubled first tokens. Suitable for person-level grouping and cross-session analysis. Identical to name for 93.4% of rows."},
                        {"name": "speaker_raw",   "type": "string",  "description": "Raw speaker attribution string as extracted from the PDF, before normalization."},
                        {"name": "role",          "type": "string",  "description": "Speaker's parliamentary role (e.g. 'Bundeskanzler', 'Vizepräsident'). Null if not indicated in the attribution."},
                        {"name": "party",         "type": "string",  "description": "Party abbreviation (e.g. 'CDU', 'SPD'). Null means the party was not listed in the attribution — not that the speaker is independent."},
                        {"name": "location",      "type": "string",  "description": "Speaker's Bundesland. Null if not listed."},
                        {"name": "stammdaten_id", "type": "string",  "description": "Official Bundestag MdB identifier (e.g. '11000001') linking this speech to the speaker's biographical record in the Bundestag Stammdaten. Null for speakers not identified as Bundestag members (non-MdB ministers, Bundesrat representatives, etc.) and for Stammdaten matches where the first name was ambiguous."},
                        {"name": "gender",        "type": "string",  "description": "'male', 'female', or 'unknown'. Derived from official Bundestag Stammdaten XML; with role-title, honorific-prefix, name-inference, and manual-lookup fallbacks."},
                        {"name": "gender_source", "type": "string",  "description": "'stammdaten' (authoritative MdB biographical data), 'title_prefix' (Frau/Herr honorific in name), 'role_title' (gendered job title in speaker attribution, e.g. Vizepräsidentin), 'manual' (manually researched lookup for surname-only and OCR-variant attributions), 'inferred' (first-name heuristic), or 'unknown'."},
                        {"name": "text",          "type": "string",  "description": "Full speech text in German."},
                        {"name": "char_offset",   "type": "integer", "description": "Byte offset of the speech start in the cleaned session text file."},
                    ]
                }
            },
            {
                "name":      "speakers",
                "path":      "speakers.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": "One row per unique normalized speaker name with aggregate statistics.",
                "schema": {
                    "primaryKey": ["name"],
                    "fields": [
                        {"name": "name",              "type": "string",  "description": "Normalized speaker name (primary key). Maps to speeches.name. May contain b_02 artefacts for ~6.6% of entries — see name_cleaned."},
                        {"name": "name_cleaned",      "type": "string",  "description": "Cleaned speaker name with b_02 artefacts removed: role/preamble prefixes (Minister, Kollege, Bitte …), procedure-text suffixes (Überweisungsvorschlag …), two-speaker concatenations, and doubled first tokens. Identical to name for 93.4% of entries. Use this column for person-level grouping and cross-session analysis."},
                        {"name": "stammdaten_id",     "type": "string",  "description": "Official Bundestag MdB identifier linking this speaker to persons.parquet. Null for non-MdBs and ambiguous surname-only matches. Use this to join to persons.parquet for birth date, career span, and cross-session name variant tracking."},
                        {"name": "birth_date",        "type": "date",    "description": "ISO 8601 birth date 'YYYY-MM-DD' from the official Bundestag Stammdaten. Null for non-MdBs and ambiguous surname-only matches."},
                        {"name": "gender",            "type": "string",  "description": "'male', 'female', or 'unknown'."},
                        {"name": "gender_source",     "type": "string",  "description": "Source of gender annotation: 'stammdaten', 'title_prefix', 'role_title', 'manual', 'inferred', or 'unknown'. The most authoritative source across all speeches is reported."},
                        {"name": "n_speeches",        "type": "integer", "description": "Total speeches attributed to this name across all sessions."},
                        {"name": "most_common_party", "type": "string",  "description": "Modal party across all speeches. Null if party is never listed."},
                        {"name": "most_common_role",  "type": "string",  "description": "Modal role across all speeches. Null if role is never listed."},
                    ]
                }
            },
            {
                "name":      "persons",
                "path":      "persons.parquet",
                "mediatype": "application/vnd.apache.parquet",
                "description": (
                    "One row per Bundestag member (MdB) identified in the corpus via the "
                    "official Stammdaten biographical data. Provides cross-session person "
                    "identity: multiple name strings in the corpus that refer to the same "
                    "MdB are grouped under one stammdaten_id with all name variants listed. "
                    "Non-MdB speakers (ministers who were never members, Bundesrat "
                    "representatives, etc.) are absent — use speakers.parquet for those."
                ),
                "schema": {
                    "primaryKey": ["stammdaten_id"],
                    "fields": [
                        {"name": "stammdaten_id",  "type": "string",  "description": "Official Bundestag MdB identifier (primary key). Links to speeches.stammdaten_id and speakers.stammdaten_id."},
                        {"name": "canonical_name", "type": "string",  "description": "Cleaned name form with the most speeches for this MdB across the full corpus. Suitable for display and per-person analysis."},
                        {"name": "birth_date",     "type": "date",    "description": "ISO 8601 birth date 'YYYY-MM-DD' from the official Bundestag Stammdaten."},
                        {"name": "gender",         "type": "string",  "description": "'male' or 'female'. From Stammdaten — always authoritative for entries in this table."},
                        {"name": "gender_source",  "type": "string",  "description": "Gender source label — always 'stammdaten' for persons with a unique first-name match; may be 'stammdaten' with null id for same-surname matches. See speeches.gender_source."},
                        {"name": "n_speeches",     "type": "integer", "description": "Total speeches attributed to this MdB across all sessions and all name variants."},
                        {"name": "name_variants",  "type": "string",  "description": "Pipe-separated list of all distinct name strings (from speeches.name) that mapped to this stammdaten_id. Useful for understanding OCR and formatting variation across sessions."},
                    ]
                }
            },
            {
                "name":      "parties",
                "path":      "parties.csv",
                "mediatype": "text/csv",
                "description": "Reference table of German parliamentary parties, 1949–present.",
                "schema": {
                    "primaryKey": ["abbreviation"],
                    "fields": [
                        {"name": "party_name_de", "type": "string",  "description": "Full party name in German."},
                        {"name": "party_name_en", "type": "string",  "description": "Full party name in English."},
                        {"name": "abbreviation",  "type": "string",  "description": "Party abbreviation used in speeches.party."},
                        {"name": "year",          "type": "integer", "description": "Year the party was founded or first represented in the Bundestag."},
                        {"name": "status",        "type": "string",  "description": "'active' or 'inactive'."},
                    ]
                }
            }
        ]
    }


# Static file templates

_LICENSE_TEXT = """\
Creative Commons Attribution 4.0 International (CC BY 4.0)

You are free to:
  Share — copy and redistribute the material in any medium or format
  Adapt — remix, transform, and build upon the material for any purpose,
          even commercially.

Under the following terms:
  Attribution — You must give appropriate credit, provide a link to the
                license, and indicate if changes were made.

Full license text:
  https://creativecommons.org/licenses/by/4.0/legalcode

Note: The underlying Bundestag Plenarprotokolle are published as open data
by the Deutscher Bundestag. This derived dataset is released under CC BY 4.0.
"""


def _make_citation_cff(version: str) -> str:
    return textwrap.dedent(f"""\
        cff-version: 1.2.0
        message: "If you use this dataset, please cite it using the metadata below."
        type: dataset
        title: "Bundestag Plenarprotokolle Dataset"
        version: "{version}"
        date-released: "{date.today().isoformat()}"
        abstract: >
          Structured, speaker-attributed speech corpus from 75 years of German Bundestag
          plenary sessions (WP01–WP21, 1949–present). Covers 4,620 sessions and over
          1,040,000 speeches with speaker name, role, party, gender, and full text.
        license: CC-BY-4.0
        repository-code: "https://github.com/[REPOSITORY]"
        doi: "[ADD ZENODO DOI AFTER UPLOAD]"
        keywords:
          - Bundestag
          - parliamentary debates
          - German politics
          - natural language processing
          - political science
          - corpus linguistics
        authors:
          - family-names: "[YOUR LAST NAME]"
            given-names:  "[YOUR FIRST NAME]"
            affiliation:  "[YOUR INSTITUTION]"
        references:
          - type: article
            title: "[DATA PAPER TITLE — ADD WHEN PUBLISHED]"
            doi:   "[DATA PAPER DOI  — ADD WHEN PUBLISHED]"
        """)


def _make_readme(n_sessions: int, n_speeches: int, n_speakers: int,
                 n_persons: int, gender_pct: float, version: str) -> str:
    return textwrap.dedent(f"""\
        # Bundestag Plenarprotokolle Dataset {version}

        Structured, speaker-attributed speech corpus from 75 years of German Bundestag
        plenary sessions (WP01–WP21, 1949–present).

        | Statistic | Value |
        |---|---|
        | Sessions | {n_sessions:,} |
        | Speeches | {n_speeches:,} |
        | Unique speaker names | {n_speakers:,} |
        | MdBs (Stammdaten-linked persons) | {n_persons:,} |
        | Gender coverage (non-unknown) | {gender_pct:.1f}% |

        ## Which table to use

        | I want to … | Use this table |
        |---|---|
        | Analyse speeches (text, party, gender, date) | `speeches.parquet` |
        | Count speeches or compute statistics **per politician** | `persons.parquet` |
        | Look up career span, birth date, or name variants for an MdB | `persons.parquet` |
        | Understand how a name appeared in the raw transcripts | `speakers.parquet` |
        | Filter or aggregate by session (date, city, engine quality) | `sessions.parquet` |

        > **Note:** The same politician often appears under slightly different name strings
        > across sessions (e.g. "Schmidt", "Dr. Schmidt", "Frau Dr. Schmidt").
        > `persons.parquet` resolves these variants into one row per person via
        > `stammdaten_id`. Use it — not `speakers.parquet` — for any person-level analysis.

        ## Files

        | File | Rows | Description |
        |---|---|---|
        | `speeches.parquet` | {n_speeches:,} | One row per speech: name, role, party, gender, stammdaten_id, full text |
        | `persons.parquet` | {n_persons:,} | **Person-level table.** One row per MdB, aggregating all name variants via stammdaten_id. Use for career statistics and speaker-level analysis |
        | `sessions.parquet` | {n_sessions:,} | One row per plenary session |
        | `speakers.parquet` | {n_speakers:,} | Name-string index: one row per unique name string as extracted from transcripts. Useful for understanding extraction quality; **not** suitable for person-level aggregation |
        | `parties.csv` | 31 | Reference table of German parties, 1949–present |
        | `speeches.csv.gz` | {n_speeches:,} | CSV fallback for Stata / Excel (same columns as speeches.parquet) |
        | `datapackage.json` | — | Frictionless Data schema with column descriptions and FK constraints |
        | `CITATION.cff` | — | Machine-readable citation metadata |

        ## Quickstart (Python)

        ```python
        import pandas as pd

        speeches = pd.read_parquet("speeches.parquet")
        sessions = pd.read_parquet("sessions.parquet")
        speakers = pd.read_parquet("speakers.parquet")
        persons  = pd.read_parquet("persons.parquet")

        # Filter to a single Wahlperiode
        wp01 = speeches[speeches["wp"] == 1]

        # Female speeches per WP
        female_by_wp = (
            speeches[speeches["gender"] == "female"]
            .groupby("wp")
            .size()
            .rename("n_female_speeches")
        )

        # (career span analysis removed — first_wp / last_wp not in persons table)

        # Age at time of speech (requires stammdaten_id + birth_date)
        sp_with_birth = speeches.merge(
            persons[["stammdaten_id", "birth_date"]], on="stammdaten_id", how="left"
        )
        sp_with_birth["age"] = (
            pd.to_datetime(sp_with_birth["date_iso"]).dt.year
            - pd.to_datetime(sp_with_birth["birth_date"]).dt.year
        )

        # All speeches by a specific politician (cross-session, all name variants)
        # Step 1: find the stammdaten_id from persons.parquet
        helmut_schmidt = persons[persons["canonical_name"].str.contains("Helmut Schmidt")]
        schmidt_id = helmut_schmidt["stammdaten_id"].iloc[0]
        # Step 2: filter speeches by that ID — picks up all name variants automatically
        schmidt_speeches = speeches[speeches["stammdaten_id"] == schmidt_id]
        print(f"{{len(schmidt_speeches)}} speeches, {{schmidt_speeches['wp'].nunique()}} Wahlperioden")

        # Cross-session name variants for one MdB
        persons[persons["name_variants"].str.count("\\|") > 0].head(10)

        # Join session quality flag
        merged = speeches.merge(
            sessions[["session_id", "engine"]], on="session_id"
        )
        born_digital = merged[merged["engine"] == "pdftotext"]
        ```

        ## Quickstart (R)

        ```r
        library(arrow)
        speeches <- read_parquet("speeches.parquet")
        sessions <- read_parquet("sessions.parquet")

        # Female speeches per Wahlperiode
        library(dplyr)
        speeches |>
            filter(gender == "female") |>
            count(wp)
        ```

        ## Quickstart (Stata)

        ```stata
        * Use speeches.csv.gz (Stata 16+)
        import delimited "speeches.csv.gz", encoding("UTF-8")
        ```

        ## Data Notes

        **OCR quality (WP01–WP09)**
        Sessions extracted with `engine = 'pdfminer'` came from scanned two-column PDFs.
        Expect occasional Unicode replacement characters (U+FFFD) in the `text` field.
        Filter on `engine` or `wp <= 9` for quality-sensitive NLP work.

        **`party = null`**
        Means the speaker's party was *not listed* in the transcript attribution —
        normal for ministers, presiding officers, and Bundesrat representatives.
        It does not mean the speaker is politically independent.

        **Gender annotation**
        Resolved in priority order: (1) official Bundestag Stammdaten XML matched by
        surname+firstname (`stammdaten`); (2) Frau/Herr honorific prefix in the name
        field (`title_prefix`); (3) gendered German job title in the speaker attribution,
        e.g. *Vizepräsidentin* = female, *Bundesminister* = male (`role_title`);
        (4) manually researched lookup for surname-only and OCR-variant attributions
        (`manual`); (5) first-name heuristic via gender_guesser (`inferred`).
        `'unknown'` means none of the above resolved the gender.

        **Cross-session identity and name fragmentation**
        The same politician often appears under different name strings across sessions
        (e.g. "Schmidt", "Dr. Schmidt", "Frau Dr. Schmidt" — all the same person).
        `speakers.parquet` groups by raw name string and will therefore fragment a
        single politician across multiple rows. Use `persons.parquet` instead: it
        groups all name variants for the same MdB under one `stammdaten_id`, giving
        a single row with correctly aggregated speech counts, career span, and
        birth date. Coverage is ~98.5% of speeches; the remaining ~1.5% (ambiguous
        surname-only attributions and speakers not in the Bundestag Stammdaten) retain
        a null `stammdaten_id` and are only available at name-string level in
        `speakers.parquet`. Full biographical data is available at:
        https://www.bundestag.de/services/opendata

        ## License

        CC-BY 4.0. See the LICENSE file. The underlying Plenarprotokolle are official
        government documents of the Deutscher Bundestag and are public domain.

        ## Citation

        See CITATION.cff. If a data paper DOI is available, please also cite the paper.
        """)


# Main

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Package the Bundestag corpus into a Zenodo-ready dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--preprocessed-dir", required=True,
                    help="Path to data/preprocessed/ root")
    ap.add_argument("--gender-csv", required=True,
                    help="Path to outputs/gender_lookup.csv")
    ap.add_argument("--parties-csv", required=True,
                    help="Path to docs/political-parties-past-75-years.csv")
    ap.add_argument("--excluded-speakers-csv", default=None, metavar="PATH",
                    help="Path to docs/excluded_false_speakers.csv. "
                         "Default: auto-resolved from project root.")
    ap.add_argument("--manual-gender-csv", default=None, metavar="PATH",
                    help="Path to docs/manual_gender_overrides.csv. "
                         "Default: auto-resolved from project root.")
    ap.add_argument("--stammdaten", default=None, metavar="PATH",
                    help="Path to MdB-Stammdaten.xml for WP/party/role disambiguation. "
                         "Default: docs/MdB-Stammdaten.xml (relative to project root). "
                         "When present, enables a second-pass that resolves surname-only "
                         "attributions using speech WP, party, and role context.")
    ap.add_argument("--output-dir", required=True,
                    help="Destination folder for the packaged dataset")
    ap.add_argument("--version", default="v1",
                    help="Dataset version string written into metadata (default: v1)")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress progress output")
    args = ap.parse_args()

    verbose          = not args.quiet
    preprocessed_dir = Path(args.preprocessed_dir)
    gender_csv       = Path(args.gender_csv)
    parties_csv      = Path(args.parties_csv)
    output_dir       = Path(args.output_dir)

    # Resolve Stammdaten XML path (optional; used for second-pass disambiguation).
    if args.stammdaten:
        stammdaten_path = Path(args.stammdaten)
    else:
        stammdaten_path = Path(__file__).parent.parent / "docs" / "MdB-Stammdaten.xml"

    # Resolve excluded-speakers CSV (optional; silently skipped if absent).
    if args.excluded_speakers_csv:
        excluded_csv = Path(args.excluded_speakers_csv)
    else:
        excluded_csv = Path(__file__).parent.parent / "docs" / "excluded_false_speakers.csv"

    # Resolve manual gender overrides CSV (optional; silently skipped if absent).
    if args.manual_gender_csv:
        manual_gender_csv = Path(args.manual_gender_csv)
    else:
        manual_gender_csv = Path(__file__).parent.parent / "docs" / "manual_gender_overrides.csv"

    # ── input validation ───────────────────────────────────────────────────────
    for p, label in [
        (preprocessed_dir, "--preprocessed-dir"),
        (gender_csv,       "--gender-csv"),
        (parties_csv,      "--parties-csv"),
    ]:
        if not p.exists():
            sys.exit(f"ERROR: {label} not found: {p}")

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Load all sessions + speeches ───────────────────────────────────────
    sessions_rows, speeches_rows = load_corpus(preprocessed_dir, verbose=verbose)
    if verbose:
        print(f"  {len(sessions_rows):,} sessions, "
              f"{len(speeches_rows):,} speeches loaded.", flush=True)

    # ── 2. Build DataFrames ────────────────────────────────────────────────────
    if verbose:
        print("Building DataFrames …", flush=True)

    sessions_df = pd.DataFrame(sessions_rows)
    for col in ("wp", "session_num", "year", "n_speeches"):
        sessions_df[col] = sessions_df[col].astype("Int64")

    speeches_df = pd.DataFrame(speeches_rows)
    for col in ("wp", "sequence", "char_offset"):
        speeches_df[col] = speeches_df[col].astype("Int64")

    # ── 2b. Deduplicate sessions and speeches ──────────────────────────────────
    # Some sessions were processed from two different source PDFs and ended up
    # with the same session_id in the metadata (8 known cases, e.g. WP03_Session115
    # appears from both 03115.pdf and 07115.pdf).  Keep the first occurrence
    # (files are loaded in sorted directory order, so the lexicographically earlier
    # path wins — in practice this is always the "canonical" PDF for that session).
    n_dup_sessions = sessions_df.duplicated("session_id").sum()
    if n_dup_sessions:
        if verbose:
            dup_info = (sessions_df[sessions_df.duplicated("session_id", keep=False)]
                        [["session_id", "source_pdf"]]
                        .sort_values("session_id"))
            print(f"  NOTE: {n_dup_sessions} duplicate session_id entries found "
                  f"(same session processed from two PDFs). Keeping first.", flush=True)
            print(dup_info.to_string(index=False), flush=True)
        sessions_df = sessions_df.drop_duplicates("session_id", keep="first")

    n_dup_speeches = speeches_df.duplicated(["session_id", "sequence"]).sum()
    if n_dup_speeches:
        if verbose:
            print(f"  NOTE: {n_dup_speeches} duplicate (session_id, sequence) speech rows "
                  f"dropped (same sessions as above).", flush=True)
        speeches_df = speeches_df.drop_duplicates(["session_id", "sequence"], keep="first")

    # ── 2c. Exclude known false speakers ──────────────────────────────────────
    # Speeches attributed to non-person names (table headers, legislative terms,
    # military ranks, etc. that were parsed as speaker attributions by the
    # colon rule in b_02) are dropped here. The exclusion list lives in
    # docs/excluded_false_speakers.csv and can be extended as new cases are found.
    if excluded_csv.exists():
        excl_df = pd.read_csv(excluded_csv, dtype=str)
        excluded_names = set(excl_df["name"].dropna().str.strip())
        mask = speeches_df["name"].isin(excluded_names)
        n_excl = mask.sum()
        if n_excl and verbose:
            n_excl_names = speeches_df.loc[mask, "name"].nunique()
            print(f"  NOTE: Excluded {n_excl:,} speeches attributed to "
                  f"{n_excl_names} false-speaker names "
                  f"(see docs/excluded_false_speakers.csv).", flush=True)
        speeches_df = speeches_df[~mask]
    elif verbose:
        print(f"  NOTE: excluded_false_speakers.csv not found at {excluded_csv} "
              f"— skipping exclusion step.", flush=True)

    # ── 2d. Fix Kolumnentitel + speaker attribution merges ─────────────────────
    # Strips presiding-officer page-header names fused with the next speaker
    # attribution by pdftotext (e.g. "Petra Pau Wolfgang Wiehle (AfD)").
    # Re-attributes real-speaker cases; drops procedure-text cases.
    # Must run AFTER exclusion (2c) and BEFORE gender join (3) so corrected
    # names receive proper gender annotation.
    n_before_kolumn = len(speeches_df)
    speeches_df = _fix_kolumnentitel_merges(speeches_df, verbose=verbose)
    if verbose:
        n_reattr = len(speeches_df) - (n_before_kolumn - (n_before_kolumn - len(speeches_df)))
        print(f"  After Kolumnentitel fix: {len(speeches_df):,} speeches.", flush=True)

    # ── 3. Join gender ─────────────────────────────────────────────────────────
    if verbose:
        print("Joining gender lookup …", flush=True)

    gender_df = pd.read_csv(gender_csv, dtype=str)
    if "source" in gender_df.columns and "gender_source" not in gender_df.columns:
        gender_df = gender_df.rename(columns={"source": "gender_source"})
    # Deduplicate: one row per name (take first occurrence)
    gender_df = gender_df.drop_duplicates("name", keep="first")

    # Determine which columns are available (older gender_lookup.csv may lack
    # stammdaten_id / birth_date columns added by e_01 v2).
    _join_cols = ["name", "gender", "gender_source"]
    _has_stammdaten_cols = (
        "stammdaten_id" in gender_df.columns
        and "birth_date"    in gender_df.columns
    )
    if _has_stammdaten_cols:
        _join_cols += ["stammdaten_id", "birth_date"]
    elif verbose:
        print("  NOTE: gender_lookup.csv does not contain stammdaten_id / birth_date — "
              "re-run e_01_gender.py to generate the extended lookup.", flush=True)

    speeches_df = speeches_df.merge(gender_df[_join_cols], on="name", how="left")
    speeches_df["gender"]        = speeches_df["gender"].fillna("unknown")
    speeches_df["gender_source"] = speeches_df["gender_source"].fillna("unknown")
    # stammdaten_id and birth_date intentionally left as NaN/None for non-MdBs.

    # Load the Stammdaten XML context for second-pass (WP/party/role) disambiguation.
    # Only attempted when the extended gender_lookup.csv columns are present, since
    # that implies e_01 v2 has already run and stammdaten_id is available as a column.
    context_lookup: dict = {}
    if _has_stammdaten_cols:
        if stammdaten_path.exists():
            if verbose:
                print("Loading Stammdaten context for WP/party/role disambiguation …",
                      flush=True)
            context_lookup = _load_stammdaten_context(stammdaten_path)
            if verbose:
                n_ctx_recs = sum(len(v) for v in context_lookup.values())
                print(f"  {n_ctx_recs:,} records across "
                      f"{len(context_lookup):,} surnames.", flush=True)
        elif verbose:
            print(f"  NOTE: Stammdaten XML not found at {stammdaten_path} — "
                  f"contextual disambiguation disabled. Pass --stammdaten or "
                  f"place the XML at docs/MdB-Stammdaten.xml.", flush=True)

    # ── 3a. Role-title gender override ────────────────────────────────────────
    # For speeches where gender is still 'unknown' after the Stammdaten join,
    # check speaker_raw for gendered German job titles. These are authoritative:
    # "Vizepräsidentin" can only be a woman; "Vizepräsident" only a man.
    # Female titles are checked first so "-in" suffixes are never subsumed by
    # the male pattern.
    n_unknown_before = (speeches_df["gender"] == "unknown").sum()
    if n_unknown_before and verbose:
        print(f"  Applying role-title gender override to "
              f"{n_unknown_before:,} unknown-gender speeches …", flush=True)

    raw_series = speeches_df["speaker_raw"].fillna("")

    # Pass 1 — female titles
    unknown_mask  = speeches_df["gender"] == "unknown"
    female_hit    = unknown_mask & raw_series.str.contains(_RE_FEMALE_ROLE, na=False)
    speeches_df.loc[female_hit, "gender"]        = "female"
    speeches_df.loc[female_hit, "gender_source"] = "role_title"

    # Pass 2 — male titles in speaker_raw (only on remaining unknowns)
    unknown_mask  = speeches_df["gender"] == "unknown"
    male_hit      = unknown_mask & raw_series.str.contains(_RE_MALE_ROLE, na=False)
    speeches_df.loc[male_hit, "gender"]        = "male"
    speeches_df.loc[male_hit, "gender_source"] = "role_title"

    # Pass 3 — noble titles in name field (Freiherr, Prinz → male)
    # These titles never appear as German surnames; their presence is
    # unambiguous. Checking the name field catches cases where the title
    # was parsed into the name (e.g. "Freiherr von Gemmingen") rather than
    # the speaker_raw role string.
    unknown_mask = speeches_df["gender"] == "unknown"
    noble_hit    = unknown_mask & (
        speeches_df["name"].fillna("").str.contains(_RE_NOBLE_MALE_NAME, na=False)
    )
    speeches_df.loc[noble_hit, "gender"]        = "male"
    speeches_df.loc[noble_hit, "gender_source"] = "role_title"

    n_resolved = n_unknown_before - (speeches_df["gender"] == "unknown").sum()
    if verbose and n_unknown_before:
        pct = n_resolved / n_unknown_before * 100
        print(f"  Role-title override: resolved {n_resolved:,} / "
              f"{n_unknown_before:,} speeches ({pct:.1f}%).", flush=True)
        by_gender = speeches_df.loc[
            speeches_df["gender_source"] == "role_title", "gender"
        ].value_counts()
        for g, c in by_gender.items():
            print(f"    {g}: {c:,}", flush=True)

    # ── 3b. Manual gender override ────────────────────────────────────────────
    # Loads docs/manual_gender_overrides.csv — a manually researched lookup
    # table for speakers whose gender could not be resolved automatically
    # (surname-only attributions, OCR variants, noble titles in name field, etc.).
    # Only rows with gender = 'male' or 'female' are applied; other values
    # (not_a_person, mixed) are silently ignored.
    # Applied only to speeches still marked 'unknown' after all automatic passes.
    if manual_gender_csv.exists():
        man_df = pd.read_csv(manual_gender_csv, dtype=str)
        # Support both column naming conventions (name_original or name)
        if "name_original" in man_df.columns:
            man_df = man_df.rename(columns={"name_original": "name"})
        man_df = man_df[man_df["gender"].isin(["male", "female"])]
        man_df = man_df[["name", "gender"]].drop_duplicates("name", keep="first")

        n_unknown_before_manual = (speeches_df["gender"] == "unknown").sum()
        if verbose and n_unknown_before_manual:
            print(f"  Applying manual gender override ({len(man_df):,} entries) …",
                  flush=True)

        speeches_df = speeches_df.merge(
            man_df.rename(columns={"gender": "_mgender"}),
            on="name", how="left"
        )
        manual_hit = (speeches_df["gender"] == "unknown") & speeches_df["_mgender"].notna()
        speeches_df.loc[manual_hit, "gender"]        = speeches_df.loc[manual_hit, "_mgender"]
        speeches_df.loc[manual_hit, "gender_source"] = "manual"
        speeches_df = speeches_df.drop(columns=["_mgender"])

        n_resolved_manual = n_unknown_before_manual - (speeches_df["gender"] == "unknown").sum()
        if verbose and n_unknown_before_manual:
            pct = n_resolved_manual / n_unknown_before_manual * 100 if n_unknown_before_manual else 0
            print(f"  Manual override: resolved {n_resolved_manual:,} / "
                  f"{n_unknown_before_manual:,} speeches ({pct:.1f}%).", flush=True)
    elif verbose:
        print(f"  NOTE: manual_gender_overrides.csv not found at {manual_gender_csv} "
              f"— skipping manual override step.", flush=True)

    # ── 3c. Contextual stammdaten_id disambiguation (WP + party + role) ─────────
    # For speeches where the initial name-only Stammdaten lookup assigned a gender
    # but could not identify the specific person (stammdaten_id is null), attempt
    # a second pass using speech-level context:
    #   WP   → filter to candidates who actually served in that parliamentary term
    #   party → further filter by PARTEI_KURZ match (handles CDU/CSU, Greens, Linke)
    #   role  → fallback for null-party speakers: match against per-WP INSTITUTIONEN
    #           entries (e.g. "Bundeskanzler", "Bundesminister des Innern")
    # All unmatched speeches are eligible — _contextual_disambiguate() returns None
    # when the surname has no Stammdaten entry at all, so broadening the mask is safe.
    # This catches role_title / title_prefix / manual / inferred speakers whose surname
    # does appear in Stammdaten but whose gender came from a non-surname path (e.g.
    # "Präsident" → male), plus any stammdaten-sourced names still unresolved after e_01.
    if _has_stammdaten_cols and context_lookup:
        ctx_mask = speeches_df["stammdaten_id"].isna()
        n_ctx_cands = int(ctx_mask.sum())
        if verbose and n_ctx_cands:
            n_ctx_names = int(speeches_df.loc[ctx_mask, "name"].nunique())
            print(f"  Contextual disambiguation: {n_ctx_cands:,} speeches "
                  f"({n_ctx_names:,} unique names) eligible for "
                  f"WP/party/role refinement …", flush=True)

        if ctx_mask.any():
            # Build sentinel-filled merge keys so NaN party/role don't break the join.
            _tgt = speeches_df.loc[ctx_mask, ["name", "wp", "party", "role"]].copy()
            _tgt["_wp_i"] = _tgt["wp"].fillna(-1).astype(int)
            _tgt["_p"]    = _tgt["party"].fillna("")
            _tgt["_r"]    = _tgt["role"].fillna("")
            unique_combos = _tgt[["name", "_wp_i", "_p", "_r"]].drop_duplicates()

            # Disambiguate each unique (name, WP, party, role) combination
            result_rows: list[dict] = []
            for _, row in unique_combos.iterrows():
                name      = row["name"]
                wp_val    = int(row["_wp_i"]) if row["_wp_i"] != -1 else None
                party_val = row["_p"] or None
                role_val  = row["_r"] or None

                fn, sn = _name_to_parts_ctx(name)
                if not sn:
                    continue

                rec = _contextual_disambiguate(
                    sn, fn, wp_val, party_val, role_val, context_lookup
                )
                if rec and rec.get("stammdaten_id"):
                    result_rows.append({
                        "name":  name,
                        "_wp_i": row["_wp_i"],
                        "_p":    row["_p"],
                        "_r":    row["_r"],
                        "_cid":  rec["stammdaten_id"],
                        "_cbd":  rec.get("birth_date"),
                    })

            if result_rows:
                ctx_res = pd.DataFrame(result_rows)

                # Attach sentinel columns to speeches_df for the merge
                speeches_df["_wp_i"] = speeches_df["wp"].fillna(-1).astype(int)
                speeches_df["_p"]    = speeches_df["party"].fillna("")
                speeches_df["_r"]    = speeches_df["role"].fillna("")

                speeches_df = speeches_df.merge(
                    ctx_res,
                    on=["name", "_wp_i", "_p", "_r"],
                    how="left",
                )
                # Apply only to speeches that were in ctx_mask AND got a match
                hit = ctx_mask & speeches_df["_cid"].notna()
                speeches_df.loc[hit, "stammdaten_id"] = speeches_df.loc[hit, "_cid"]
                speeches_df.loc[hit, "birth_date"]    = speeches_df.loc[hit, "_cbd"]
                speeches_df = speeches_df.drop(
                    columns=["_wp_i", "_p", "_r", "_cid", "_cbd"]
                )
                if verbose:
                    n_resolved_names = int(speeches_df.loc[hit, "name"].nunique())
                    print(f"  Contextual disambiguation: resolved {int(hit.sum()):,} speeches "
                          f"({n_resolved_names:,} unique names) → stammdaten_id assigned.",
                          flush=True)
            elif verbose and n_ctx_cands:
                print("  Contextual disambiguation: 0 additional matches found.",
                      flush=True)

    # ── 4. Build speakers table ────────────────────────────────────────────────
    if verbose:
        print("Building speakers table …", flush=True)

    # Gender is aggregated from speeches_df (not re-joined from gender_df)
    # so that the role-title overrides from step 3a flow through to speakers.
    # _modal_gender picks the most frequent non-unknown gender per speaker.
    # _modal_gender_source picks the most authoritative source label.
    _spk_agg = dict(
        n_speeches        =("speech_id",     "count"),
        most_common_party =("party",         _modal),
        most_common_role  =("role",          _modal),
        gender            =("gender",        _modal_gender),
        gender_source     =("gender_source", _modal_gender_source),
    )
    if _has_stammdaten_cols:
        _spk_agg["stammdaten_id"] = ("stammdaten_id", _first_non_null)
        _spk_agg["birth_date"]    = ("birth_date",    _first_non_null)

    speakers_df = speeches_df.groupby("name", as_index=False).agg(**_spk_agg)

    # Add name_cleaned: strip role/preamble prefixes, procedure-text suffixes,
    # two-speaker concatenations, and doubled first tokens.
    speakers_df["name_cleaned"] = speakers_df["name"].map(_clean_name)

    _spk_cols = ["name", "name_cleaned", "gender", "gender_source",
                 "n_speeches", "most_common_party", "most_common_role"]
    if _has_stammdaten_cols:
        # Insert after name_cleaned so identity columns cluster together
        _spk_cols = (
            ["name", "name_cleaned", "stammdaten_id", "birth_date",
             "gender", "gender_source",
             "n_speeches", "most_common_party", "most_common_role"]
        )
    speakers_df = speakers_df[_spk_cols]

    # ── 4a. Build persons table (MdBs only) ───────────────────────────────────
    # persons.parquet provides cross-session identity: multiple name strings
    # in the corpus that refer to the same Bundestag member are linked by their
    # stammdaten_id.  Non-MdBs (speakers with stammdaten_id = null) are omitted
    # because cross-session linkage is not possible without a biographical record.
    #
    # canonical_name: the name_cleaned variant with the most speeches for that
    # stammdaten_id (most stable representation of the person's name).
    # name_variants: pipe-separated list of every distinct name string (raw,
    # not cleaned) that mapped to this stammdaten_id — useful for understanding
    # how OCR/formatting variation affected name extraction.
    persons_df = pd.DataFrame()
    if _has_stammdaten_cols:
        if verbose:
            print("Building persons table …", flush=True)

        mdb_speakers = speakers_df[speakers_df["stammdaten_id"].notna()].copy()
        persons_rows: list[dict] = []
        for stammdaten_id_val, grp in mdb_speakers.groupby("stammdaten_id"):
            # canonical_name = name_cleaned of the variant with the most speeches
            best = grp.sort_values("n_speeches", ascending=False).iloc[0]
            name_variants_str = "|".join(sorted(grp["name"].tolist()))
            persons_rows.append({
                "stammdaten_id":  stammdaten_id_val,
                "canonical_name": best["name_cleaned"],
                "birth_date":     (best["birth_date"]
                                  if "birth_date" in best.index and pd.notna(best["birth_date"])
                                  else None),
                "gender":         best["gender"],
                "gender_source":  best["gender_source"],
                "n_speeches":     int(grp["n_speeches"].sum()),
                "name_variants":  name_variants_str,
            })

        if persons_rows:
            persons_df = pd.DataFrame(persons_rows)
            persons_df["n_speeches"] = persons_df["n_speeches"].astype("Int64")

        if verbose:
            n_multi = sum(
                1 for _, g in mdb_speakers.groupby("stammdaten_id")
                if len(g) > 1
            )
            print(f"  {len(persons_df):,} MdBs matched — "
                  f"{n_multi:,} with multiple name variants in corpus.", flush=True)

    # ── 4c. Back-propagate speaker-level gender to remaining unknown speeches ──
    # speakers_df.gender uses _modal_gender (non-unknown mode across all speeches
    # for that speaker).  A speaker can have some speeches resolved (role_title
    # matched their 2nd appearance) and other speeches still 'unknown' (their
    # 1st appearance had no role-title signal).  Without this step, speeches.parquet
    # would still have 'unknown' rows for those earlier appearances even though
    # speakers.parquet correctly shows them as 'female'/'male'.
    still_unknown = speeches_df["gender"] == "unknown"
    if still_unknown.any():
        spk_gender = speakers_df[["name", "gender", "gender_source"]].rename(
            columns={"gender": "_spk_gender", "gender_source": "_spk_gender_source"}
        )
        speeches_df = speeches_df.merge(spk_gender, on="name", how="left")
        prop_mask = still_unknown & (speeches_df["_spk_gender"] != "unknown")
        speeches_df.loc[prop_mask, "gender"]        = speeches_df.loc[prop_mask, "_spk_gender"]
        speeches_df.loc[prop_mask, "gender_source"] = speeches_df.loc[prop_mask, "_spk_gender_source"]
        speeches_df = speeches_df.drop(columns=["_spk_gender", "_spk_gender_source"])
        n_propagated = prop_mask.sum()
        if verbose and n_propagated:
            print(f"  Back-propagated speaker-level gender to {n_propagated:,} 'unknown' "
                  f"speech rows.", flush=True)

    # ── 5. Reorder speeches columns ────────────────────────────────────────────
    # Carry name_cleaned from speakers into speeches for inline access
    # (avoids a join on every speech-level query).
    speeches_df = speeches_df.merge(
        speakers_df[["name", "name_cleaned"]], on="name", how="left"
    )
    speeches_df["year"] = pd.to_datetime(
        speeches_df["date_iso"], errors="coerce"
    ).dt.year.astype("Int64")

    _sp_cols = [
        "speech_id", "session_id", "sequence",
        "wp", "date_iso", "year", "city",
        "name", "name_cleaned", "speaker_raw", "role", "party", "location",
        "gender", "gender_source",
        "text", "char_offset",
    ]
    if _has_stammdaten_cols and "stammdaten_id" in speeches_df.columns:
        # Insert stammdaten_id between location and gender
        idx = _sp_cols.index("gender")
        _sp_cols.insert(idx, "stammdaten_id")
    speeches_df = speeches_df[_sp_cols]

    # ── 6. Integrity checks ────────────────────────────────────────────────────
    if not speeches_df["speech_id"].is_unique:
        dupes = speeches_df[speeches_df.duplicated("speech_id", keep=False)]
        print(f"\nFATAL: {dupes['speech_id'].nunique():,} speech_id values appear more than once "
              f"({len(dupes):,} rows total).\n")
        # Are any session_ids themselves duplicated?
        dup_sessions = speeches_df[speeches_df.duplicated(["session_id", "sequence"], keep=False)]
        print("Sample duplicate (session_id, sequence) pairs:")
        print(dup_sessions[["speech_id", "session_id", "sequence", "name"]]
              .sort_values("speech_id").head(20).to_string(index=False))
        # Are there duplicate session_ids in the sessions table?
        dup_sess_ids = sessions_df[sessions_df.duplicated("session_id", keep=False)]
        if len(dup_sess_ids):
            print(f"\nAlso: {len(dup_sess_ids):,} duplicate session_id rows in sessions table:")
            print(dup_sess_ids[["session_id", "wp", "session_num", "source_pdf"]].to_string(index=False))
        sys.exit("\nFATAL: speech_id is not unique — see diagnostics above.")
    if not speeches_df["session_id"].isin(sessions_df["session_id"]).all():
        n_bad = (~speeches_df["session_id"].isin(sessions_df["session_id"])).sum()
        sys.exit(f"FATAL: {n_bad:,} speeches have session_ids not found in sessions table.")

    # ── 7. Write output files ──────────────────────────────────────────────────
    if verbose:
        print("Writing output files …", flush=True)

    def _write(path, label):
        size_mb = path.stat().st_size / 1e6
        if verbose:
            print(f"  {label:<25} {size_mb:6.1f} MB", flush=True)

    sessions_df.to_parquet(output_dir / "sessions.parquet", index=False)
    _write(output_dir / "sessions.parquet", "sessions.parquet")

    speeches_df.to_parquet(output_dir / "speeches.parquet", index=False)
    _write(output_dir / "speeches.parquet", "speeches.parquet")

    speakers_df.to_parquet(output_dir / "speakers.parquet", index=False)
    _write(output_dir / "speakers.parquet", "speakers.parquet")

    if not persons_df.empty:
        persons_df.to_parquet(output_dir / "persons.parquet", index=False)
        _write(output_dir / "persons.parquet", "persons.parquet")
    elif verbose:
        print(f"  {'persons.parquet':<25} (skipped — no stammdaten_id data)", flush=True)

    if verbose:
        print("  speeches.csv.gz           (writing — may take a minute) …",
              flush=True)
    speeches_df.to_csv(output_dir / "speeches.csv.gz", index=False, compression="gzip")
    _write(output_dir / "speeches.csv.gz", "speeches.csv.gz")

    shutil.copy2(parties_csv, output_dir / "parties.csv")
    if verbose:
        print(f"  {'parties.csv':<25} (copied)", flush=True)

    with open(output_dir / "datapackage.json", "w", encoding="utf-8") as fh:
        json.dump(_make_datapackage(args.version), fh, indent=2, ensure_ascii=False)
    if verbose:
        print(f"  {'datapackage.json':<25} (written)", flush=True)

    with open(output_dir / "CITATION.cff", "w", encoding="utf-8") as fh:
        fh.write(_make_citation_cff(args.version))
    if verbose:
        print(f"  {'CITATION.cff':<25} (template — fill in author/DOI)", flush=True)

    with open(output_dir / "LICENSE", "w", encoding="utf-8") as fh:
        fh.write(_LICENSE_TEXT)
    if verbose:
        print(f"  {'LICENSE':<25} (CC-BY 4.0)", flush=True)

    n_sessions = len(sessions_df)
    n_speeches = len(speeches_df)
    n_speakers = len(speakers_df)
    n_persons  = len(persons_df) if not persons_df.empty else 0
    gender_pct = (speeches_df["gender"] != "unknown").mean() * 100

    with open(output_dir / "README.md", "w", encoding="utf-8") as fh:
        fh.write(_make_readme(n_sessions, n_speeches, n_speakers, n_persons, gender_pct, args.version))
    if verbose:
        print(f"  {'README.md':<25} (written)", flush=True)

    # ── 8. Summary ─────────────────────────────────────────────────────────────
    if verbose:
        pct_female = (speeches_df["gender"] == "female").mean() * 100
        pct_male   = (speeches_df["gender"] == "male").mean()   * 100
        pct_unk    = (speeches_df["gender"] == "unknown").mean() * 100

        print()
        print("=" * 60)
        print("DATASET SUMMARY")
        print("=" * 60)
        print(f"  Sessions:            {n_sessions:>10,}")
        print(f"  Speeches:            {n_speeches:>10,}")
        print(f"  Unique speaker names:{n_speakers:>10,}")
        if n_persons:
            n_mdb_matched = (speakers_df["stammdaten_id"].notna()).sum() if _has_stammdaten_cols else 0
            pct_mdb = n_mdb_matched / n_speakers * 100 if n_speakers else 0
            print(f"  MdBs (Stammdaten):   {n_persons:>10,}  unique persons")
            print(f"  Stammdaten-linked:   {n_mdb_matched:>10,}  speaker names  ({pct_mdb:.1f}%)")
        print()
        print(f"  Gender — female:     {pct_female:>9.1f}%")
        print(f"  Gender — male:       {pct_male:>9.1f}%")
        print(f"  Gender — unknown:    {pct_unk:>9.1f}%")
        print()
        print(f"  Null party:          {speeches_df['party'].isna().mean() * 100:>9.1f}%")
        print(f"  Null role:           {speeches_df['role'].isna().mean()  * 100:>9.1f}%")
        n_names_cleaned = (speakers_df["name"] != speakers_df["name_cleaned"]).sum()
        pct_cleaned = n_names_cleaned / len(speakers_df) * 100
        print(f"  Names cleaned:       {n_names_cleaned:>10,}  ({pct_cleaned:.1f}% of unique names)")
        print()
        print("  Engine breakdown (sessions):")
        for eng, cnt in sessions_df["engine"].value_counts().items():
            print(f"    {str(eng):<15} {cnt:>6,} sessions")
        print()
        print(f"  Output directory: {output_dir.resolve()}")
        print("=" * 60)
        print("All integrity checks passed.")


if __name__ == "__main__":
    main()
