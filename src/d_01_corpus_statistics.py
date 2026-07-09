"""
d_01_corpus_statistics.py
=========================
Compute corpus statistics and generate publication-ready figures for the
Bundestag Plenarprotokolle data paper (Data Records section).

Reads all *_speakers.json files produced by b_02_speaker_identifier.py and
generates seven CSV tables and three figures:

  <output_dir>/
    table_01_corpus_overview.csv     — per-WP sessions / speeches / mean length
    table_02_speaker_types.csv       — speaker-type counts per WP
    table_03_party_speeches.csv      — normalised party speech counts per WP
    table_04_notable_speakers.csv    — Bundeskanzler / presidents per WP
    table_05_party_stats.csv         — speeches + unique speakers per party per WP
    table_06_role_stats.csv          — speeches + unique speakers per role category per WP
    table_07_role_raw_stats.csv      — speeches + unique speakers per raw role string per WP

    fig_01_speeches_per_wp.png/pdf   — corpus size over time
    fig_02_speaker_composition.png/pdf — speaker type breakdown (2 panels)
    fig_03_party_evolution.png/pdf   — party speech share evolution

Requirements (beyond stdlib):
    pip install matplotlib

Python: 3.10+ (uses X | Y type-union syntax)

Usage:
    python src/d_01_corpus_statistics.py
    python src/d_01_corpus_statistics.py \\
        --preprocessed-dir $HPCWORK/bundestag-project/data/preprocessed \\
        --output-dir       $HPCWORK/bundestag-project/outputs
"""

# matplotlib backend must be set before pyplot is imported.
# "Agg" renders to files without requiring a display (works on HPC).
import matplotlib
matplotlib.use("Agg")

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

# =============================================================================
# USER SETTINGS  (override via CLI flags)
# =============================================================================
_DEFAULT_PREPROCESSED = (
    r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics"
    r"\bundestag-project\data\preprocessed"
)
# =============================================================================


# -----------------------------------------------------------------------------
# Wahlperiode metadata
# -----------------------------------------------------------------------------

WP_META: dict[str, dict] = {
    "01": {"years": "1949–53", "city": "Bonn",   "start": 1949},
    "02": {"years": "1953–57", "city": "Bonn",   "start": 1953},
    "03": {"years": "1957–61", "city": "Bonn",   "start": 1957},
    "04": {"years": "1961–65", "city": "Bonn",   "start": 1961},
    "05": {"years": "1965–69", "city": "Bonn",   "start": 1965},
    "06": {"years": "1969–72", "city": "Bonn",   "start": 1969},
    "07": {"years": "1972–76", "city": "Bonn",   "start": 1972},
    "08": {"years": "1976–80", "city": "Bonn",   "start": 1976},
    "09": {"years": "1980–83", "city": "Bonn",   "start": 1980},
    "10": {"years": "1983–87", "city": "Bonn",   "start": 1983},
    "11": {"years": "1987–90", "city": "Bonn",   "start": 1987},
    "12": {"years": "1990–94", "city": "Bonn",   "start": 1990},
    "13": {"years": "1994–98", "city": "Bonn",   "start": 1994},
    "14": {"years": "1998–02", "city": "Berlin", "start": 1998},
    "15": {"years": "2002–05", "city": "Berlin", "start": 2002},
    "16": {"years": "2005–09", "city": "Berlin", "start": 2005},
    "17": {"years": "2009–13", "city": "Berlin", "start": 2009},
    "18": {"years": "2013–17", "city": "Berlin", "start": 2013},
    "19": {"years": "2017–21", "city": "Berlin", "start": 2017},
    "20": {"years": "2021–25", "city": "Berlin", "start": 2021},
    "21": {"years": "2025–  ", "city": "Berlin", "start": 2025},
}

WPS: list[str] = [f"{i:02d}" for i in range(1, 22)]


# -----------------------------------------------------------------------------
# Speaker type classification
# -----------------------------------------------------------------------------

# Each entry: (tuple_of_prefixes, canonical_label).
# Rules are evaluated in order; first match wins.
_ROLE_RULES: list[tuple] = [
    (("Alterspräsident", "Vizepräsident", "Präsident"), "Präsidium"),
    (("Bundeskanzlerin", "Bundeskanzler"),               "Bundeskanzler/in"),
    (("Bundesministerin", "Bundesminister"),             "Bundesminister/in"),
    (("Parl. Staatssekretärin", "Parl. Staatssekretär"), "Parl. Staatssekr."),
    (("Staatssekretärin", "Staatssekretär"),             "Staatssekretär/in"),
    (("Staatsministerin", "Staatsminister"),             "Staatsminister/in"),
    (("Ministerpräsidentin", "Ministerpräsident",
      "Regierender Bürgermeister"),                      "Ministerpräsident/in"),
]

def classify_role(role: str | None) -> str:
    """Map a raw role string to a canonical speaker-type label."""
    if not role:
        return "Abgeordnete/r"
    for prefixes, label in _ROLE_RULES:
        if any(role.startswith(p) for p in prefixes):
            return label
    return "Sonstige"


_NOTABLE_ROLE_MAP: list[tuple[str, str]] = [
    # Checked in order; first match wins.  More-specific prefixes come first.
    ("Alterspräsident",          "alterspräsident"),
    ("Vizepräsident",            "vizepräsidenten"),
    ("Bundeskanzler",            "bundeskanzler"),
    ("Präsident des Bundesrat",  "bundesratspraesident"),  # "Bundesrats" or "Bundesrates"
    # Bundestagspräsident: role is exactly "Präsident" or "Präsidentin"
    # (the prefix "Präsident" would also fire for Alterspräsident/Vize/BundesratsPräsident,
    # so those are handled by earlier rules above — this entry is the fallback.)
    ("Präsident",                "bundestagspraesident"),
]


def _notable_role_type(role: str | None) -> str | None:
    """Return the notable-speaker category for *role*, or None if not notable."""
    if not role:
        return None
    for prefix, category in _NOTABLE_ROLE_MAP:
        if role.startswith(prefix):
            return category
    return None


# Ordered list used in tables and stacked bars.
ALL_TYPES: list[str] = [
    "Abgeordnete/r",
    "Präsidium",
    "Bundeskanzler/in",
    "Bundesminister/in",
    "Parl. Staatssekr.",
    "Staatssekretär/in",
    "Staatsminister/in",
    "Ministerpräsident/in",
    "Sonstige",
]


# -----------------------------------------------------------------------------
# Party normalisation
# -----------------------------------------------------------------------------

_PARTY_MAP: dict[str, str] = {}

def _register(canonical: str, variants: set[str]) -> None:
    for v in variants:
        _PARTY_MAP[v] = canonical

_register("CDU/CSU", {
    "CDU/CSU", "CDU", "CSU", "CDUCSU", "CDU CSU", "CDU, CSU", "CDU. CSU",
    "CDU: CSU", "CDU; CSU", "CDU %CSU", "CDU 'CSU", "CDU ' CSU",
    "CDU /CUS", "CDU/CDU", "CDU/SCSU", "CDT T/CSU", "CSU/ CSU",
    "CSU/CDU", "CSU/CSU", "alle CDU/CSU", "beide CDU/CSU",
})
_register("SPD", {"SPD", "SDP", "SPP", "SPD-Gast"})
_register("FDP", {
    "FDP", "F.D.P.", "F.D.P", "F.D:P.", "FPD", "F VP", "Gast FDP",
})
_register("GRÜNEN/B90", {
    "GRÜNE", "DIE GRÜNEN", "GRÜNEN", "GRÜNE/Bündnis 90",
    "BÜNDNIS 90/DIE GRÜNEN", "BÜNDNIS 90/GRÜNE",
    "BÜNDNIS 90/D1E GRÜ-NEN", "BÜNDNIS 90/DIE GRÜ-NEN",
    "BÜNDNISS 90/DIE GRÜ-NEN", "BÜNDNS 90/DIE GRÜ-NEN",
    "BÜND-NIS 90/DIE GRÜNEN",
    "Bündnis 90/DIE GRÜNEN", "Bündnis 90/Die Grünen",
    "Bündnis 90/GRÜNE", "Bündnis90/Die Grünen", "GRUÑE",
})
_register("PDS/Linke", {
    "PDS", "PDS/LL", "PDS/Linke Liste", "Gruppe der PDS",
    "DIE LINKE", "Die Linke", "DIE LIN-KE",
})
_register("AfD",    {"AfD"})
_register("KPD",    {"KPD"})
_register("Sonst.", {
    "GB/BHE", "DP", "DP [FVP]", "DP[FVP]", "FVP",
    "WAV", "Z", "DA", "DRP", "SRP", "BP", "FU", "SSW", "NPD", "DKP",
})

# Values meaning "independent / no faction" — valid but excluded from
# the party evolution figure.
_FRAKTIONSLOS = frozenset({
    "fraktionslos", "Fraktionslos", "fraktionlos", "parteilos",
})

# Sub-strings that flag a party value as garbage that slipped through cleaning.
_GARBAGE_SUBS = (
    "begrüßt", "Drucksache", "Tagesordnungspunkt",
    "Sitzung", "Button", "Aufschrift", "Nr. ",
)

def normalize_party(raw: str | None) -> str | None:
    """
    Map a raw party string to a canonical group name.
    Returns None for unknown, garbage, or fraktionslos values
    (these are excluded from party statistics).
    """
    if not raw:
        return None
    p = raw.strip().lstrip("(").strip()
    if not p or len(p) > 80:
        return None
    for sub in _GARBAGE_SUBS:
        if sub in p:
            return None
    if p in _FRAKTIONSLOS:
        return None
    return _PARTY_MAP.get(p, None)   # unknown variants → None


# Ordered list used in tables and party evolution figure.
ALL_PARTIES: list[str] = [
    "CDU/CSU", "SPD", "FDP", "GRÜNEN/B90", "PDS/Linke", "AfD",
    "KPD", "Sonst.",
]


# -----------------------------------------------------------------------------
# Data loading
# -----------------------------------------------------------------------------

def load_corpus(preprocessed_dir: Path) -> dict:
    """
    Read all *_speakers.json files and aggregate statistics.

    Returns a dict with:
        sessions_per_wp      dict[wp, int]
        speeches_per_wp      dict[wp, int]
        type_counts          dict[wp, dict[type, int]]
        type_wc_sum          dict[wp, dict[type, int]]   — total word count per type
        party_counts         dict[wp, dict[party, int]]  — collapsed 8-group counts
        wc_all               dict[wp, list[int]]          — word counts, every speech
        notable_speakers     dict[wp, dict[role_type, set[name]]]
        party_speech_counts  dict[wp, dict[party_str, int]]   — full granularity
        party_speaker_sets   dict[wp, dict[party_str, set[name]]]
        role_cat_speaker_sets dict[wp, dict[role_cat, set[name]]]
        role_raw_speech_counts dict[wp, dict[role_str, int]]
        role_raw_speaker_sets  dict[wp, dict[role_str, set[name]]]
    """
    metadata_dir = preprocessed_dir / "metadata"
    json_files = sorted(
        p for p in preprocessed_dir.rglob("*_speakers.json")
        if metadata_dir not in p.parents
    )

    if not json_files:
        print(f"ERROR: No *_speakers.json files found under {preprocessed_dir}")
        sys.exit(1)

    print(f"Found {len(json_files)} JSON files. Processing ...")

    sessions_per_wp      = defaultdict(int)
    speeches_per_wp      = defaultdict(int)
    type_counts          = defaultdict(lambda: defaultdict(int))
    type_wc_sum          = defaultdict(lambda: defaultdict(int))
    party_counts         = defaultdict(lambda: defaultdict(int))
    wc_all               = defaultdict(list)
    # New: full-granularity party stats (not collapsed to 8 groups)
    party_speech_counts  = defaultdict(lambda: defaultdict(int))
    party_speaker_sets   = defaultdict(lambda: defaultdict(set))
    # New: role category speakers (unique names per canonical type)
    role_cat_speaker_sets = defaultdict(lambda: defaultdict(set))
    # New: raw role string stats
    role_raw_speech_counts = defaultdict(lambda: defaultdict(int))
    role_raw_speaker_sets  = defaultdict(lambda: defaultdict(set))
    # New: notable officeholders per WP
    notable_speakers     = defaultdict(lambda: defaultdict(set))

    _re_wp = re.compile(r"WP(\d+)")

    for idx, jf in enumerate(json_files, 1):
        if idx % 500 == 0 or idx == len(json_files):
            print(f"  {idx}/{len(json_files)} ...", end="\r", flush=True)

        # Extract WP number from the directory path components
        wp = None
        for part in jf.parts:
            m = _re_wp.match(part)
            if m:
                wp = m.group(1).zfill(2)
                break
        if wp is None:
            continue

        try:
            record = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue

        speeches = record.get("speeches", [])
        sessions_per_wp[wp] += 1
        speeches_per_wp[wp] += len(speeches)

        for sp in speeches:
            text      = sp.get("text") or ""
            wc        = len(text.split())
            name      = sp.get("name") or ""
            role_raw  = sp.get("role") or ""
            party_raw = sp.get("party") or ""   # empty string when null
            stype     = classify_role(role_raw)
            pnorm     = normalize_party(party_raw)

            wc_all[wp].append(wc)
            type_counts[wp][stype] += 1
            type_wc_sum[wp][stype] += wc
            if pnorm:
                party_counts[wp][pnorm] += 1

            # Full-granularity party stats (table_05)
            party_speech_counts[wp][party_raw] += 1
            if name:
                party_speaker_sets[wp][party_raw].add(name)

            # Role category unique speakers (table_06)
            if name:
                role_cat_speaker_sets[wp][stype].add(name)

            # Raw role string stats (table_07)
            role_raw_speech_counts[wp][role_raw] += 1
            if name:
                role_raw_speaker_sets[wp][role_raw].add(name)

            # Notable officeholders (table_04)
            notable_type = _notable_role_type(role_raw)
            if notable_type and name:
                notable_speakers[wp][notable_type].add(name)

    n_sess = sum(sessions_per_wp.values())
    n_sp   = sum(speeches_per_wp.values())
    print(f"\nLoaded: {n_sess:,} sessions, {n_sp:,} speeches.")

    return {
        "sessions_per_wp": dict(sessions_per_wp),
        "speeches_per_wp": dict(speeches_per_wp),
        "type_counts":     {k: dict(v) for k, v in type_counts.items()},
        "type_wc_sum":     {k: dict(v) for k, v in type_wc_sum.items()},
        "party_counts":    {k: dict(v) for k, v in party_counts.items()},
        "wc_all":          dict(wc_all),
        # Full-granularity tables — sets kept as sets (not serialised)
        "notable_speakers":        {k: dict(v) for k, v in notable_speakers.items()},
        "party_speech_counts":     {k: dict(v) for k, v in party_speech_counts.items()},
        "party_speaker_sets":      {k: dict(v) for k, v in party_speaker_sets.items()},
        "role_cat_speaker_sets":   {k: dict(v) for k, v in role_cat_speaker_sets.items()},
        "role_raw_speech_counts":  {k: dict(v) for k, v in role_raw_speech_counts.items()},
        "role_raw_speaker_sets":   {k: dict(v) for k, v in role_raw_speaker_sets.items()},
    }


# -----------------------------------------------------------------------------
# CSV writers
# -----------------------------------------------------------------------------

def write_table_01(data: dict, out_dir: Path) -> None:
    """Table 1: corpus overview per WP."""
    path = out_dir / "table_01_corpus_overview.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp", "years", "city", "sessions", "speeches",
                    "mean_words_per_speech", "total_words"])
        for wp in WPS:
            n_sess  = data["sessions_per_wp"].get(wp, 0)
            n_sp    = data["speeches_per_wp"].get(wp, 0)
            wcs     = data["wc_all"].get(wp, [])
            mean_wc = round(sum(wcs) / len(wcs), 1) if wcs else 0
            total_w = sum(wcs)
            meta    = WP_META.get(wp, {})
            w.writerow([wp, meta.get("years", ""), meta.get("city", ""),
                        n_sess, n_sp, mean_wc, total_w])
    print(f"Saved: {path}")


def write_table_02(data: dict, out_dir: Path) -> None:
    """Table 2: speaker-type speech counts per WP."""
    path = out_dir / "table_02_speaker_types.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp"] + ALL_TYPES)
        for wp in WPS:
            counts = data["type_counts"].get(wp, {})
            w.writerow([wp] + [counts.get(t, 0) for t in ALL_TYPES])
    print(f"Saved: {path}")


def write_table_03(data: dict, out_dir: Path) -> None:
    """Table 3: normalised party speech counts per WP."""
    path = out_dir / "table_03_party_speeches.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp"] + ALL_PARTIES)
        for wp in WPS:
            counts = data["party_counts"].get(wp, {})
            w.writerow([wp] + [counts.get(p, 0) for p in ALL_PARTIES])
    print(f"Saved: {path}")


def write_table_04(data: dict, out_dir: Path) -> None:
    """Table 4: notable officeholders per WP (chancellors, presidents)."""
    path = out_dir / "table_04_notable_speakers.csv"
    notable_cols = [
        "bundeskanzler",
        "bundestagspraesident",
        "alterspräsident",
        "vizepräsidenten",
        "bundesratspraesident",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp"] + notable_cols)
        for wp in WPS:
            ns = data["notable_speakers"].get(wp, {})
            row = [wp]
            for col in notable_cols:
                names = sorted(ns.get(col, set()))
                row.append("; ".join(names))
            w.writerow(row)
    print(f"Saved: {path}")


def write_table_05(data: dict, out_dir: Path) -> None:
    """Table 5: full-granularity party statistics (speeches + unique speakers) per WP.

    Rows: one per (WP, party_string) combination, plus an "ALL" aggregate row
    per party (aggregated across all WPs).  Party strings are the normalised
    values from the speakers.json files — not collapsed to the 8 canonical
    groups used in table_03.  An empty string in the ``party`` column means
    the transcript contained no party qualifier (typical for ministers and
    presiding officers).
    """
    path = out_dir / "table_05_party_stats.csv"

    sc = data["party_speech_counts"]   # dict[wp][party] → int
    ss = data["party_speaker_sets"]    # dict[wp][party] → set[name]
    grand_total = sum(data["speeches_per_wp"].values()) or 1

    # Aggregate across all WPs
    total_sc: dict[str, int] = {}
    total_ss: dict[str, set] = {}
    for wp_dict in sc.values():
        for party, cnt in wp_dict.items():
            total_sc[party] = total_sc.get(party, 0) + cnt
    for wp_dict in ss.values():
        for party, names in wp_dict.items():
            if party not in total_ss:
                total_ss[party] = set()
            total_ss[party] |= names

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp", "party", "n_speeches", "n_unique_speakers", "share_speeches"])

        for wp in WPS:
            wp_sc = sc.get(wp, {})
            wp_ss = ss.get(wp, {})
            total_wp = data["speeches_per_wp"].get(wp, 0) or 1
            for party in sorted(wp_sc.keys()):
                n_sp     = wp_sc[party]
                n_unique = len(wp_ss.get(party, set()))
                share    = round(n_sp / total_wp, 6)
                w.writerow([wp, party, n_sp, n_unique, share])

        # ALL aggregate rows
        for party in sorted(total_sc.keys()):
            n_sp     = total_sc[party]
            n_unique = len(total_ss.get(party, set()))
            share    = round(n_sp / grand_total, 6)
            w.writerow(["ALL", party, n_sp, n_unique, share])

    print(f"Saved: {path}")


def write_table_06(data: dict, out_dir: Path) -> None:
    """Table 6: role-category statistics (speeches + unique speakers) per WP.

    Uses the same 9 canonical categories as table_02 (via classify_role()).
    Rows are ordered by ALL_TYPES within each WP so comparisons across WPs
    are column-aligned when pivoting.  An "ALL" aggregate row is appended
    for each category.
    """
    path = out_dir / "table_06_role_stats.csv"

    tc  = data["type_counts"]           # dict[wp][role_cat] → int (speeches)
    rcs = data["role_cat_speaker_sets"] # dict[wp][role_cat] → set[name]
    grand_total = sum(data["speeches_per_wp"].values()) or 1

    # Aggregate across all WPs
    total_tc:  dict[str, int] = {}
    total_rcs: dict[str, set] = {}
    for wp_dict in tc.values():
        for rc, cnt in wp_dict.items():
            total_tc[rc] = total_tc.get(rc, 0) + cnt
    for wp_dict in rcs.values():
        for rc, names in wp_dict.items():
            if rc not in total_rcs:
                total_rcs[rc] = set()
            total_rcs[rc] |= names

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp", "role_category", "n_speeches",
                    "n_unique_speakers", "share_speeches"])

        for wp in WPS:
            wp_tc  = tc.get(wp, {})
            wp_rcs = rcs.get(wp, {})
            total_wp = data["speeches_per_wp"].get(wp, 0) or 1
            for rc in ALL_TYPES:
                n_sp     = wp_tc.get(rc, 0)
                n_unique = len(wp_rcs.get(rc, set()))
                share    = round(n_sp / total_wp, 6)
                w.writerow([wp, rc, n_sp, n_unique, share])

        # ALL aggregate rows
        for rc in ALL_TYPES:
            n_sp     = total_tc.get(rc, 0)
            n_unique = len(total_rcs.get(rc, set()))
            share    = round(n_sp / grand_total, 6)
            w.writerow(["ALL", rc, n_sp, n_unique, share])

    print(f"Saved: {path}")


def write_table_07(data: dict, out_dir: Path) -> None:
    """Table 7: raw role-string statistics (speeches + unique speakers) per WP.

    Every distinct normalised role string (post-b_02 normalization) is listed
    with its speech and unique-speaker counts.  Within each WP the rows are
    sorted by n_speeches descending.  An "ALL" aggregate block follows, also
    sorted by total n_speeches descending.

    This table is useful for validating that role normalization worked (few
    near-duplicate strings should remain) and for users who want the full
    role granularity.  An empty string in ``role_raw`` means the transcript
    contained no role qualifier (regular MdBs).
    """
    path = out_dir / "table_07_role_raw_stats.csv"

    rc  = data["role_raw_speech_counts"]  # dict[wp][role_str] → int
    rs  = data["role_raw_speaker_sets"]   # dict[wp][role_str] → set[name]

    # Aggregate across all WPs
    total_rc: dict[str, int] = {}
    total_rs: dict[str, set] = {}
    for wp_dict in rc.values():
        for role, cnt in wp_dict.items():
            total_rc[role] = total_rc.get(role, 0) + cnt
    for wp_dict in rs.values():
        for role, names in wp_dict.items():
            if role not in total_rs:
                total_rs[role] = set()
            total_rs[role] |= names

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp", "role_raw", "n_speeches", "n_unique_speakers"])

        for wp in WPS:
            wp_rc = rc.get(wp, {})
            wp_rs = rs.get(wp, {})
            for role in sorted(wp_rc.keys(), key=lambda r: (-wp_rc[r], r)):
                w.writerow([wp, role, wp_rc[role],
                            len(wp_rs.get(role, set()))])

        # ALL aggregate
        for role in sorted(total_rc.keys(), key=lambda r: (-total_rc[r], r)):
            w.writerow(["ALL", role, total_rc[role],
                        len(total_rs.get(role, set()))])

    print(f"Saved: {path}")


# -----------------------------------------------------------------------------
# Plot style
# -----------------------------------------------------------------------------

plt.rcParams.update({
    "font.family":       "sans-serif",
    "font.size":         10,
    "axes.titlesize":    11,
    "axes.labelsize":    10,
    "xtick.labelsize":   8,
    "ytick.labelsize":   9,
    "legend.fontsize":   8.5,
    "figure.dpi":        150,
    "axes.spines.top":   False,
    "axes.spines.right": False,
})

_BONN_COLOR   = "#4C72B0"   # blue  — Bonn era shading / bars
_BERLIN_COLOR = "#DD8452"   # amber — Berlin era shading / bars

_PARTY_COLORS: dict[str, str] = {
    "CDU/CSU":    "#2E2E2E",   # near-black
    "SPD":        "#E3000F",   # red
    "FDP":        "#C9A800",   # dark amber (party yellow is illegible on white)
    "GRÜNEN/B90": "#1AA037",   # green
    "PDS/Linke":  "#BE3075",   # reddish-purple
    "AfD":        "#009EE0",   # light blue
    "KPD":        "#8B0000",   # dark red
    "Sonst.":     "#999999",   # grey
}

_TYPE_COLORS: dict[str, str] = {
    "Abgeordnete/r":        "#BBBBBB",
    "Präsidium":            "#2C5F8A",
    "Bundeskanzler/in":     "#111111",
    "Bundesminister/in":    "#E07020",
    "Parl. Staatssekr.":    "#F0A855",
    "Staatssekretär/in":    "#C8B400",
    "Staatsminister/in":    "#7B4BA0",
    "Ministerpräsident/in": "#2A8C50",
    "Sonstige":             "#DDDDDD",
}


def _save(fig: plt.Figure, out_dir: Path, stem: str) -> None:
    """Save a figure as both PNG (300 dpi) and PDF."""
    for ext in ("png", "pdf"):
        path = out_dir / f"{stem}.{ext}"
        fig.savefig(path, dpi=300, bbox_inches="tight")
        print(f"Saved: {path}")
    plt.close(fig)


# -----------------------------------------------------------------------------
# Figure 1 — Speeches per Wahlperiode
# -----------------------------------------------------------------------------

def fig1_speeches_per_wp(data: dict, out_dir: Path) -> None:
    """
    Bar chart of speeches per WP, coloured by city (Bonn vs Berlin).
    A secondary axis shows the number of sessions as a dotted line.
    WP12 (reunification) and WP14 (Berlin move) are annotated.
    """
    speeches = [data["speeches_per_wp"].get(wp, 0) for wp in WPS]
    sessions = [data["sessions_per_wp"].get(wp, 0)  for wp in WPS]
    colors   = [_BONN_COLOR if WP_META[wp]["city"] == "Bonn"
                else _BERLIN_COLOR for wp in WPS]
    x        = np.arange(len(WPS))
    xlabels  = [f"WP{wp}" for wp in WPS]

    fig, ax1 = plt.subplots(figsize=(13, 4.5))

    ax1.bar(x, speeches, color=colors, edgecolor="white",
            linewidth=0.5, alpha=0.88, zorder=3)
    ax1.set_ylabel("Number of speeches")
    ax1.set_xlabel("Wahlperiode")
    ax1.set_xticks(x)
    ax1.set_xticklabels(xlabels, rotation=45, ha="right")
    ax1.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax1.set_axisbelow(True)
    ax1.yaxis.grid(True, linestyle="--", alpha=0.35, zorder=0)

    # Secondary y-axis: sessions
    ax2 = ax1.twinx()
    ax2.plot(x, sessions, "o--", color="#444444", linewidth=1.3,
             markersize=4, alpha=0.75, zorder=4)
    ax2.set_ylabel("Number of sessions", color="#444444")
    ax2.tick_params(axis="y", colors="#444444")
    ax2.spines["right"].set_color("#888888")
    ax2.spines["top"].set_visible(False)

    # Event annotations
    max_sp = max(speeches) if speeches else 1
    for wp_ann, txt in [
        ("12", "Wiedervereinigung\n(WP12, 1990)"),
        ("14", "Umzug nach Berlin\n(WP14, 1998)"),
    ]:
        idx_a = WPS.index(wp_ann)
        ax1.axvline(idx_a, color="#888888", linestyle=":", linewidth=0.9, zorder=2)
        ax1.text(idx_a + 0.18, max_sp * 0.88, txt,
                 fontsize=7.5, color="#555555", va="top", linespacing=1.4)

    # Legend
    bonn_patch   = mpatches.Patch(color=_BONN_COLOR,   label="Bonn era (WP01–13)")
    berlin_patch = mpatches.Patch(color=_BERLIN_COLOR, label="Berlin era (WP14–21)")
    sess_line    = plt.Line2D([0], [0], color="#444444", linestyle="--",
                               marker="o", markersize=4, label="Sessions (right axis)")
    ax1.legend(handles=[bonn_patch, berlin_patch, sess_line],
               loc="upper left", framealpha=0.92, fontsize=8.5)

    ax1.set_title(
        "Bundestag Plenarprotokolle — Corpus Size by Wahlperiode", pad=10)
    fig.tight_layout()
    _save(fig, out_dir, "fig_01_speeches_per_wp")


# -----------------------------------------------------------------------------
# Figure 2 — Speaker type composition (two panels)
# -----------------------------------------------------------------------------

def fig2_speaker_composition(data: dict, out_dir: Path) -> None:
    """
    Two-panel figure.

    Panel A (left):  Stacked bar chart — fraction of speeches by speaker type
                     per WP.  Shows the relative importance of different
                     institutional roles over time.

    Panel B (right): Mean speech word count per WP for the overall corpus and
                     for the four most numerically important speaker types.
    """
    # --- Panel A: fractions per WP ---
    fracs: dict[str, list[float]] = {}
    for t in ALL_TYPES:
        fracs[t] = []
        for wp in WPS:
            counts = data["type_counts"].get(wp, {})
            total  = sum(counts.values()) or 1
            fracs[t].append(counts.get(t, 0) / total)

    # --- Panel B: mean word count ---
    mean_wc_overall = []
    for wp in WPS:
        wcs = data["wc_all"].get(wp, [])
        mean_wc_overall.append(float(np.mean(wcs)) if wcs else 0.0)

    selected_types = [
        "Abgeordnete/r", "Bundesminister/in",
        "Bundeskanzler/in", "Präsidium",
    ]
    mean_wc_by_type: dict[str, list] = {}
    for t in selected_types:
        vals = []
        for wp in WPS:
            cnt = data["type_counts"].get(wp, {}).get(t, 0)
            wcs = data["type_wc_sum"].get(wp, {}).get(t, 0)
            vals.append(wcs / cnt if cnt > 0 else float("nan"))
        mean_wc_by_type[t] = vals

    x       = np.arange(len(WPS))
    xlabels = [f"WP{wp}" for wp in WPS]

    fig, (ax_a, ax_b) = plt.subplots(1, 2, figsize=(15, 5.5))

    # Panel A — stacked bars
    bottoms = np.zeros(len(WPS))
    for t in ALL_TYPES:
        ax_a.bar(x, fracs[t], bottom=bottoms,
                 color=_TYPE_COLORS[t], label=t,
                 edgecolor="white", linewidth=0.25)
        bottoms += np.array(fracs[t])

    ax_a.set_xticks(x)
    ax_a.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=7.5)
    ax_a.set_ylabel("Fraction of speeches")
    ax_a.set_xlabel("Wahlperiode")
    ax_a.set_ylim(0, 1)
    ax_a.yaxis.set_major_formatter(
        mticker.PercentFormatter(xmax=1, decimals=0))
    ax_a.set_title("A  Speaker type composition")
    ax_a.set_axisbelow(True)
    ax_a.yaxis.grid(True, linestyle="--", alpha=0.3)
    ax_a.legend(loc="lower right", fontsize=7.5, framealpha=0.88,
                handlelength=1.2, borderpad=0.6)

    # Panel B — mean word count lines
    ax_b.plot(x, mean_wc_overall, "k-o", linewidth=2, markersize=4,
              label="All speeches", zorder=5)
    line_styles = {
        "Abgeordnete/r":    ("--", 1.4),
        "Bundesminister/in":("-.", 1.4),
        "Bundeskanzler/in": (":",  1.8),
        "Präsidium":        ("--", 1.2),
    }
    for t, (ls, lw) in line_styles.items():
        vals = mean_wc_by_type[t]
        ax_b.plot(x, vals, ls, color=_TYPE_COLORS[t],
                  linewidth=lw, label=t, alpha=0.85)

    ax_b.set_xticks(x)
    ax_b.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=7.5)
    ax_b.set_ylabel("Mean speech length (words)")
    ax_b.set_xlabel("Wahlperiode")
    ax_b.set_title("B  Mean speech length by speaker type")
    ax_b.legend(loc="upper left", fontsize=7.5, framealpha=0.88)
    ax_b.set_axisbelow(True)
    ax_b.yaxis.grid(True, linestyle="--", alpha=0.3)

    fig.tight_layout()
    _save(fig, out_dir, "fig_02_speaker_composition")


# -----------------------------------------------------------------------------
# Figure 3 — Party speech share evolution
# -----------------------------------------------------------------------------

def fig3_party_evolution(data: dict, out_dir: Path) -> None:
    """
    Line chart of party speech shares per WP.

    For each WP the denominator is the total number of speeches whose party
    could be normalised to one of the canonical groups in ALL_PARTIES.
    Parties absent in a WP are plotted as zero (or omitted if always zero).
    """
    # Compute fractional share for each party in each WP
    fracs: dict[str, list[float]] = {p: [] for p in ALL_PARTIES}
    for wp in WPS:
        counts = data["party_counts"].get(wp, {})
        total  = sum(counts.get(p, 0) for p in ALL_PARTIES) or 1
        for p in ALL_PARTIES:
            fracs[p].append(counts.get(p, 0) / total)

    x       = np.arange(len(WPS))
    xlabels = [f"WP{wp}" for wp in WPS]

    fig, ax = plt.subplots(figsize=(13, 5.5))

    # Shade eras lightly
    bonn_end = WPS.index("13")
    ax.axvspan(-0.5, bonn_end + 0.5,
               alpha=0.04, color=_BONN_COLOR,   zorder=0)
    ax.axvspan(bonn_end + 0.5, len(WPS) - 0.5,
               alpha=0.04, color=_BERLIN_COLOR,  zorder=0)

    for p in ALL_PARTIES:
        vals = fracs[p]
        if max(vals) < 0.003:          # skip parties never above 0.3 %
            continue
        thick = max(vals) > 0.05       # thicker line for major parties
        ax.plot(
            x, [v * 100 for v in vals],
            "o-" if thick else ".-",
            color=_PARTY_COLORS[p],
            linewidth=2.0 if thick else 1.0,
            markersize=4   if thick else 2,
            label=p,
            alpha=0.9,
        )

    # Event annotation lines
    for wp_ann, txt in [
        ("12", "Reunification\n(1990)"),
        ("14", "→ Berlin\n(1999)"),
    ]:
        idx_a = WPS.index(wp_ann)
        ax.axvline(idx_a, color="#AAAAAA", linestyle=":", linewidth=0.9, zorder=1)
        ax.text(idx_a + 0.15, ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 60,
                txt, fontsize=7.5, color="#666666", va="top", linespacing=1.4)

    ax.set_xticks(x)
    ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Share of speeches with known party (%)")
    ax.set_xlabel("Wahlperiode")
    ax.set_title("Party Speech Share by Wahlperiode")
    ax.legend(loc="upper right", fontsize=8.5, framealpha=0.92,
              ncol=2, handlelength=1.5)
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)
    ax.set_xlim(-0.5, len(WPS) - 0.5)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    _save(fig, out_dir, "fig_03_party_evolution")


# -----------------------------------------------------------------------------
# Main runner
# -----------------------------------------------------------------------------

def run(preprocessed_dir: str | None = None,
        output_dir: str | None = None) -> None:

    pre_dir = Path(preprocessed_dir or _DEFAULT_PREPROCESSED)
    if not pre_dir.exists():
        print(f"ERROR: Preprocessed folder not found: {pre_dir}")
        sys.exit(1)

    if output_dir:
        out_dir = Path(output_dir)
    else:
        # Default: sibling of preprocessed named "figures"
        out_dir = pre_dir.parent / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Preprocessed : {pre_dir}")
    print(f"Output       : {out_dir}")
    print()

    data = load_corpus(pre_dir)

    print("\nWriting tables ...")
    write_table_01(data, out_dir)
    write_table_02(data, out_dir)
    write_table_03(data, out_dir)
    write_table_04(data, out_dir)
    write_table_05(data, out_dir)
    write_table_06(data, out_dir)
    write_table_07(data, out_dir)

    print("\nGenerating figures ...")
    fig1_speeches_per_wp(data, out_dir)
    fig2_speaker_composition(data, out_dir)
    fig3_party_evolution(data, out_dir)

    print("\nAll done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate Bundestag corpus statistics and figures."
    )
    parser.add_argument(
        "--preprocessed-dir", default=None, metavar="PATH",
        help=f"Root preprocessed folder. Default: {_DEFAULT_PREPROCESSED}",
    )
    parser.add_argument(
        "--output-dir", default=None, metavar="PATH",
        help="Output directory for tables and figures. "
             "Default: <preprocessed>/../figures",
    )
    args = parser.parse_args()
    run(preprocessed_dir=args.preprocessed_dir,
        output_dir=args.output_dir)
