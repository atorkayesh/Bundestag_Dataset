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


def _surname_suffixes(name: str) -> list[tuple[str, str]]:
    """(firstname, surname) candidates, longest surname first, folded.

    `_parse_name` takes the LAST token as the surname, which can never find a
    multi-token one: "Meyer zu Bentrup" is sought under "bentrup", "De Ridder"
    under "ridder" and "Stokar von Neuforn" under "neuforn" — keys the
    Stammdaten does not hold, because it spells such surnames out in full. 26
    names over 1,437 speeches were left with no identity for that reason,
    among them sitting members: Silke Stokar von Neuforn, Fabio De Masi,
    Amira Mohamed Ali, Reem Alabali Radovan.

    Every suffix of the token run is offered so the caller can take the
    longest the register actually knows. That is a MORE specific key than the
    last token, not a looser one: matching "meyer zu bentrup" in full is
    stronger evidence than matching "bentrup".

    The nobiliary particle is deliberately NOT stripped here, unlike in
    _parse_name: in a multi-token surname the particle belongs to the name.
    """
    clean = _strip_title(name)
    if not clean:
        return []
    tokens = clean.split()
    # Beyond five tokens the string is leaked text rather than a name, and
    # _parse_name's own truncation is the right treatment.
    if not 1 < len(tokens) <= 5:
        return []
    out: list[tuple[str, str]] = []
    for i in range(len(tokens) - 1):
        sn = _fold(" ".join(tokens[i:]))
        fn = _fold(tokens[0]) if i > 0 else ""
        if " " in sn:                     # only genuinely multi-token keys
            out.append((fn, sn))
    return out


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

        # Terms served. Needed so a surname-only match can be rejected when
        # the speaker and the candidate never sat at the same time.
        wps = frozenset(
            int(w.text.strip()) for w in mdb.iter("WP")
            if w is not None and w.text and w.text.strip().isdigit()
        )

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
                "wps":            wps,
            })

    return dict(lookup)



def resolve_gender(
    name: str,
    stammdaten: dict,
    guesser,
    wps: set | None = None,
) -> tuple[str, str, str | None, str | None]:
    """Return (gender, source, stammdaten_id, birth_date) for a speaker name.

    Order: Frau/Herr honorific → exact Stammdaten surname → Levenshtein-1
    surname → gender_guesser → unknown.

    `wps` is the set of Wahlperioden this name was actually seen in. It is
    used only to REJECT a surname-only match whose candidates never served in
    any of them; it never creates a match. Passing nothing keeps the old
    behaviour.
    """
    # --- 0. Honorific prefix overrides Stammdaten and guesser ---
    _n = name.strip()
    if re.match(r'^Frau\s+', _n, re.IGNORECASE):
        return "female", "title_prefix", None, None
    if re.match(r'^Herr\s+', _n, re.IGNORECASE):
        return "male", "title_prefix", None, None

    firstname_folded, surname_folded = _parse_name(name)
    forenames = _known_forenames(stammdaten)

    # --- 0.5. Multi-token surname, longest first ---
    # Tried before the single-token lookup because it is the more specific
    # key. Purely additive: a name whose surname is one token never enters
    # here, so the established path is untouched.
    for _fn_c, _sn_c in _surname_suffixes(name):
        _cands = stammdaten.get(_sn_c)
        if not _cands:
            continue
        _rec = _disambiguate(_cands, _fn_c, wps, forenames)
        if _rec:
            return (_rec["gender"], "stammdaten", _rec.get("stammdaten_id"),
                    _rec.get("birth_date"))

    if not surname_folded:
        return "unknown", "unknown", None, None

    # --- 1. Exact match ---
    candidates = stammdaten.get(surname_folded)
    if candidates:
        rec = _disambiguate(candidates, firstname_folded, wps, forenames)
        if rec:
            return rec["gender"], "stammdaten", rec.get("stammdaten_id"), rec.get("birth_date")
        # Exact match exists but is gender-ambiguous (same surname, both M+F).
        # Do NOT fall through to Levenshtein — a Lev-1 match would be a
        # *different* surname (one edit away), which is the wrong person.
        # Skip straight to gender_guesser.
    else:
        # --- 2. Levenshtein ≤ 1 on surname (only when no exact match) ---
        # One edit is a wide net on a short surname, and this loop used to
        # return the FIRST key it happened to reach, so the answer depended on
        # dict order rather than on evidence:
        #
        #   "Dr. Erkel", Staatssekretär im BMJ, sat one edit from BOTH "merkel"
        #   (Angela and Petra, female) and "erdel" (Rainer, male). "merkel" was
        #   reached first, so 106 rows of a male state secretary were marked
        #   female — and a rehash could silently have flipped them.
        #
        #   "Marion Caspers Merk" parses to the surname "merk", one edit from
        #   "merz" (Friedrich) and "mark" (Lothar), and 14 rows of a woman were
        #   marked male.
        #
        # Every candidate key is now collected rather than the first taken, and
        # a near-miss surname is only trusted when the forename corroborates
        # it, which is what distinguishes an OCR variant of a real member from
        # a coincidental neighbour of somebody absent from the Stammdaten.
        near = []
        for key, cands in stammdaten.items():
            if key == surname_folded:
                continue          # skip exact key (already handled above)
            if abs(len(key) - len(surname_folded)) > 1:
                continue          # fast length gate
            if _levenshtein(key, surname_folded) <= 1:
                near.append((key, cands))
        if near:
            flat = [c for _k, cands in near for c in cands]
            # an exact forename match across the near misses identifies the
            # person, and an OCR'd surname is exactly where that happens
            hits = [c for c in flat
                    if firstname_folded
                    and c["vorname_folded"] == firstname_folded]
            if hits and len({c["gender"] for c in hits}) == 1:
                rec = hits[0]
                return (rec["gender"], "stammdaten", rec.get("stammdaten_id"),
                        rec.get("birth_date"))
            # otherwise the surname alone decided it, so demand that every
            # near miss agrees on gender, that a real forename does not
            # contradict them, and that the terms overlap
            contradicted = (
                firstname_folded
                and _is_known_forename(firstname_folded, forenames)
                and not any(_forename_compatible(firstname_folded,
                                                 c["vorname_folded"])
                            for c in flat)
            )
            genders = {c["gender"] for c in flat}
            overlaps = (not wps) or any(set(c.get("wps") or ()) & wps
                                        for c in flat)
            if len(genders) == 1 and not contradicted and overlaps:
                return (genders.pop(), "stammdaten", None, None)

    # --- 2.5. the forename itself, from the Stammdaten's own name records ---
    # Reached when the surname match was refused above, which is the normal
    # case for ministers, state secretaries and state politicians who never
    # sat in the Bundestag. Labelled `inferred`, not `stammdaten`: it says
    # what the name implies, not who the person is, and carries no identity.
    fg = _forename_gender(firstname_folded, stammdaten)
    if fg:
        return fg, "inferred", None, None

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


_FORENAME_CACHE: dict = {}


def _known_forenames(stammdaten: dict) -> frozenset:
    """Every Vorname the Stammdaten holds, folded. Cached per lookup dict."""
    key = id(stammdaten)
    hit = _FORENAME_CACHE.get(key)
    if hit is None:
        hit = frozenset(
            c["vorname_folded"] for cands in stammdaten.values() for c in cands
            if c.get("vorname_folded")
        )
        _FORENAME_CACHE.clear()      # one entry is enough; keeps this bounded
        _FORENAME_CACHE[key] = hit
    return hit


_FORENAME_GENDER_CACHE: dict = {}


def _forename_gender(tok: str, stammdaten: dict) -> str | None:
    """Gender of a forename, where every Stammdaten holder of it agrees.

    This is not a guess from name endings: it is the observed gender of real
    politicians who carry the name, 4,900 records of them. It is what rescues
    the people who are NOT members — "Christian Wulff", "Alexander
    Schweitzer", "Anke Rehlinger" — whose surname exists in the Stammdaten
    attached to somebody else entirely. The old matcher answered those from
    whichever gender that other person happened to be, which was right by luck
    for 653 rows and wrong for 592. The forename answers them from evidence.

    Only a unanimous forename counts; "kersten" and the like, carried by both
    men and women, return None.
    """
    if not tok:
        return None
    key = id(stammdaten)
    table = _FORENAME_GENDER_CACHE.get(key)
    if table is None:
        seen: dict[str, set] = {}
        for cands in stammdaten.values():
            for c in cands:
                vf = c.get("vorname_folded")
                if vf:
                    seen.setdefault(vf, set()).add(c["gender"])
        table = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
        _FORENAME_GENDER_CACHE.clear()
        _FORENAME_GENDER_CACHE[key] = table
    if tok in table:
        return table[tok]
    for part in re.split(r"[-\s.]+", tok):
        if len(part) >= 3 and part in table:
            return table[part]
    return None


def _is_known_forename(tok: str, forenames) -> bool:
    """Is this token a given name at all, as opposed to debris from the name field?"""
    if not forenames:
        return False
    if tok in forenames:
        return True
    # a double name counts if either part is a known given name
    parts = [p for p in re.split(r"[-\s.]+", tok) if len(p) >= 3]
    return any(p in forenames for p in parts)


def _forename_compatible(a: str, b: str) -> bool:
    """Could these two folded forenames belong to the same person?

    Deliberately generous, because the transcript abbreviates and the
    Stammdaten does not: "hans" against "hans-peter" must still match, and so
    must a middle name dropped in print. What it must NOT accept is two
    unrelated names — "michael" against "kersten" — because that is the signal
    that the speaker is somebody the Stammdaten does not contain at all.
    """
    if not a or not b:
        return True                      # no evidence either way
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if len(short) >= 3 and long.startswith(short):
        return True                      # "hans" vs "hanspeter", "theo" vs "theodor"
    at = set(re.split(r"[-\s.]+", a)) - {""}
    bt = set(re.split(r"[-\s.]+", b)) - {""}
    return bool(at & bt)                 # a shared part of a double name


def _disambiguate(candidates: list, firstname_folded: str,
                  wps: set | None = None,
                  forenames: frozenset | None = None) -> dict | None:
    """Return matched record if gender is unambiguous, else None.

    The group-gender fallback below is the delicate part. Its premise is "we
    cannot tell WHICH person of this surname, but all of them share a gender,
    so the gender is safe regardless". That premise only holds while we have no
    evidence pointing away from the group — and twice it did not:

      "Dr. Michael Naumann", Staatsminister beim Bundeskanzler, was marked
      female across 74 rows because the only Naumann in the Stammdaten is
      Kersten Naumann. The printed forename said Michael; the fallback ignored
      it. A forename that matches nobody in the group means the speaker is
      probably not in the group at all, which is routine for ministers and
      state secretaries who never sat in the Bundestag.

      "Dr. Jochimsen", Staatssekretär in WP7-8, was marked female across 19
      rows from Lukrezia Jochimsen, who entered in WP16 — the terms do not
      overlap by a single year. He is Reimut Jochimsen.

    So both contradictions now veto the fallback, and the speaker is left
    unknown for the later, better-evidenced passes in f_01 to resolve.
    """
    if not candidates:
        return None

    # An exact forename match identifies the person outright.
    if firstname_folded:
        for c in candidates:
            if c["vorname_folded"] == firstname_folded:
                return c

    # Below here the person is NOT identified, only their surname is, so every
    # contradiction counts against inheriting the group's gender.
    #
    # The veto fires only on a token that really is a given name. The name
    # field carries plenty of debris that lands in the forename slot —
    # "AfD Markus Koob" parses to forename "afd", "AfD-Fraktion Marc Bernhard"
    # to "afd-fraktion" — and treating that debris as a contradiction threw
    # away 1,320 correct matches when this was first written. A token absent
    # from every Vorname in the Stammdaten carries no evidence either way, so
    # the group-gender fallback still applies to it.
    if (firstname_folded and _is_known_forename(firstname_folded, forenames)
            and not any(_forename_compatible(firstname_folded, c["vorname_folded"])
                        for c in candidates)):
        return None

    if wps and not any(set(c.get("wps") or ()) & wps for c in candidates):
        return None

    # All same gender → gender is unambiguous, person identity is not.
    genders = {c["gender"] for c in candidates}
    if len(genders) == 1:
        return {"gender": genders.pop(), "stammdaten_id": None, "birth_date": None}

    return None   # ambiguous: same surname, different genders — cannot resolve



def collect_unique_names(preprocessed_dir: Path) -> dict:
    """
    Scan all *_speakers.json files and return {name: frozenset of Wahlperioden}.
    Names that are None / empty are excluded.

    The terms are collected so a surname-only Stammdaten match can be rejected
    when the candidate never served while the speaker was speaking. "Dr.
    Jochimsen" spoke in WP7-8 and was being matched to Lukrezia Jochimsen of
    WP16-17, who is a different person by about thirty years.
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
    names: dict[str, set] = {}
    no_wp = 0
    for idx, jf in enumerate(json_files, 1):
        if idx % 500 == 0 or idx == len(json_files):
            print(f"  {idx}/{len(json_files)} ...", end="\r", flush=True)
        try:
            record = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        m = re.match(r"WP(\d{1,2})", jf.name)
        wp = int(m.group(1)) if m else None
        if wp is None:
            no_wp += 1
        for sp in record.get("speeches", []):
            n = sp.get("name")
            if n:
                bucket = names.setdefault(n, set())
                if wp is not None:
                    bucket.add(wp)

    print(f"\nFound {len(names):,} unique speaker names.")
    if no_wp:
        # a name with no term attached is never rejected on term grounds, so an
        # unparsed filename degrades to the old behaviour rather than silently
        # dropping matches
        print(f"  NOTE: {no_wp} file(s) had no Wahlperiode in the filename; "
              f"names seen only there keep an empty term set and are never "
              f"rejected on term grounds.")
    return {n: frozenset(w) for n, w in names.items()}



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
            name, stammdaten, _GUESSER, names[name]
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
