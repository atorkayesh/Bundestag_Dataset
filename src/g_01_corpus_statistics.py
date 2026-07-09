"""Compute corpus statistics and generate figures for the Bundestag data paper.

Reads speeches.parquet + sessions.parquet; writes 8 CSV tables and 6 figures to <output_dir>.
Requires: matplotlib, pandas, pyarrow.
"""

# matplotlib backend must be set before pyplot is imported.
# "Agg" renders to files without requiring a display (works on HPC).
import matplotlib
matplotlib.use("Agg")

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# defaults — override via CLI flags
_DEFAULT_DATASET = (
    r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics"
    r"\bundestag-project\bundestag_dataset_v1"
)


# Wahlperiode metadata

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


# Speaker type classification

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
    ("Alterspräsident",          "alterspräsident"),
    ("Vizepräsident",            "vizepräsidenten"),
    ("Bundeskanzler",            "bundeskanzler"),
    ("Präsident des Bundesrat",  "bundesratspraesident"),
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


# Canonical role aggregation  (for table_07)
# Maps raw role strings → canonical labels by prefix match.
# Rules are evaluated in order; first match wins.
# More-specific prefixes must come before more general ones
# (e.g. "Alterspräsident" before "Präsident").

_ROLE_CANONICAL: list[tuple[tuple[str, ...], str]] = [
    (("Alterspräsidentin",   "Alterspräsident"),           "Alterspräsident/in"),
    (("Vizepräsidentin",     "Vizepräsident"),             "Vizepräsident/in"),
    (("Bundespräsidentin",   "Bundespräsident"),           "Bundespräsident/in"),
    (("Ministerpräsidentin", "Ministerpräsident"),         "Ministerpräsident/in"),
    (("Bundeskanzlerin",     "Bundeskanzler"),             "Bundeskanzler/in"),
    (("Bundesministerin",    "Bundesminister"),            "Bundesminister/in"),
    (("Parl. Staatssekretärin", "Parl. Staatssekretär"),  "Parl. Staatssekretär/in"),
    (("Staatssekretärin",    "Staatssekretär"),            "Staatssekretär/in"),
    (("Staatsministerin",    "Staatsminister"),            "Staatsminister/in"),
    (("Regierender Bürgermeister",),                       "Regierender Bürgermeister"),
    (("Senatorin",           "Senator"),                   "Senator/in"),
    (("Berichterstatterin",  "Berichterstatter"),          "Berichterstatter/in"),
    (("Anfragende",          "Anfragender"),               "Anfragender/e"),
    (("Schriftführerin",     "Schriftführer"),             "Schriftführer/in"),
    # "Präsident/in" is the fallback for all remaining Präsident variants
    # (Bundestagspräsident/in, Präsident des Bundesrates, etc.)
    (("Präsidentin",         "Präsident"),                 "Präsident/in"),
]

# Canonical label for speeches with no role (regular MdBs)
_ROLE_CANONICAL_NONE = "Abgeordnete/r"
_ROLE_CANONICAL_OTHER = "Sonstige"

# Display order in the table
ALL_CANONICAL_ROLES: list[str] = [
    "Abgeordnete/r",
    "Präsident/in",
    "Vizepräsident/in",
    "Alterspräsident/in",
    "Bundespräsident/in",
    "Bundeskanzler/in",
    "Bundesminister/in",
    "Parl. Staatssekretär/in",
    "Staatssekretär/in",
    "Staatsminister/in",
    "Ministerpräsident/in",
    "Regierender Bürgermeister",
    "Senator/in",
    "Berichterstatter/in",
    "Anfragender/e",
    "Schriftführer/in",
    "Sonstige",
]


def _canonical_role(role: str | None) -> str:
    """Map a raw role string to a canonical aggregated role label."""
    if not role:
        return _ROLE_CANONICAL_NONE
    for prefixes, label in _ROLE_CANONICAL:
        if any(role.startswith(p) for p in prefixes):
            return label
    return _ROLE_CANONICAL_OTHER


# Party normalisation

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
    "BÜNDNIS 90", "Bündnis 90",                            # standalone (WP12, pre-merger)
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

_FRAKTIONSLOS = frozenset({
    "fraktionslos", "Fraktionslos", "fraktionlos", "parteilos",
})

_GARBAGE_SUBS = (
    "begrüßt", "Drucksache", "Tagesordnungspunkt",
    "Sitzung", "Button", "Aufschrift", "Nr. ",
)

# Keyword groups for topic-trend validation figure
# Each entry: display label → regex pattern (case-insensitive OR-group)

_KEYWORD_GROUPS: list[tuple[str, str]] = [
    ("Reunification",    r"Wiedervereinigung|Deutsche\s+Einheit"),
    ("Terrorism",        r"[Tt]errorismus|[Tt]erroranschlag|[Tt]erroristen"),
    ("Financial Crisis", r"Finanzkrise|Bankenkrise|Finanzmarktkrise"),
    ("Climate Change",   r"Klimawandel|Klimaschutz|Klimakrise"),
    ("COVID/Pandemic",   r"COVID|Pandemie|Coronavirus|SARS-CoV"),
]

_KEYWORD_COLORS: list[str] = [
    "#2C5F8A",   # Reunification    — blue
    "#C44E52",   # Terrorism        — red
    "#E07020",   # Financial Crisis — orange
    "#2A8C50",   # Climate Change   — green
    "#7B4BA0",   # COVID/Pandemic   — purple
]


def normalize_party(raw: str | None) -> str | None:
    """Map a raw party string to a canonical group name."""
    if not isinstance(raw, str) or not raw:
        return None
    p = raw.strip().lstrip("(").strip()
    if not p or len(p) > 80:
        return None
    for sub in _GARBAGE_SUBS:
        if sub in p:
            return None
    if p in _FRAKTIONSLOS:
        return None
    return _PARTY_MAP.get(p, None)


ALL_PARTIES: list[str] = [
    "CDU/CSU", "SPD", "FDP", "GRÜNEN/B90", "PDS/Linke", "AfD",
    "KPD", "Sonst.",
]


# Data loading — reads from published parquet files

def _sets_from_groupby(df: pd.DataFrame,
                       group_cols: list[str],
                       value_col: str) -> dict:
    """
    Build a nested dict: outer key = group_cols[0], inner key = group_cols[1],
    value = set of unique values from value_col.
    """
    result: dict = {}
    grouped = df.groupby(group_cols)[value_col].apply(
        lambda x: set(x.dropna().unique())
    )
    for keys, names in grouped.items():
        outer, inner = keys
        result.setdefault(outer, {})[inner] = names
    return result


def load_corpus(dataset_dir: Path) -> dict:
    """
    Read speeches.parquet and sessions.parquet and build the statistics
    data structure consumed by the table writers and figure generators.

    The data dict has the same keys as d_01's load_corpus() so existing
    table writers and figures can be reused without modification.
    Additional key:
        gender_counts  dict[wp, dict[gender, int]]  — for table_08 / fig_04
    """
    speeches_path  = dataset_dir / "speeches.parquet"
    sessions_path  = dataset_dir / "sessions.parquet"

    for p in (speeches_path, sessions_path):
        if not p.exists():
            print(f"ERROR: {p} not found.")
            sys.exit(1)

    print(f"Reading {speeches_path} ...")
    sp = pd.read_parquet(speeches_path)
    print(f"Reading {sessions_path} ...")
    sess = pd.read_parquet(sessions_path)
    print(f"Loaded: {len(sp):,} speeches, {len(sess):,} sessions.")

    # ── Normalise wp to zero-padded string ───────────────────────────────────
    sp["wp_str"]   = sp["wp"].apply(lambda x: f"{int(x):02d}")
    sess["wp_str"] = sess["wp"].apply(lambda x: f"{int(x):02d}")

    # ── Derived columns ───────────────────────────────────────────────────────
    sp["wc"]            = sp["text"].str.split().str.len().fillna(0).astype(int)
    sp["role_filled"]   = sp["role"].fillna("")
    sp["party_filled"]  = sp["party"].fillna("")
    sp["stype"]         = sp["role_filled"].apply(classify_role)
    sp["role_canonical"]= sp["role_filled"].apply(_canonical_role)
    sp["pnorm"]         = sp["party_filled"].apply(normalize_party)
    sp["notable_type"]  = sp["role_filled"].apply(_notable_role_type)

    # ── Per-WP totals ─────────────────────────────────────────────────────────
    sessions_per_wp  = sess.groupby("wp_str").size().to_dict()
    speeches_per_wp  = sp.groupby("wp_str").size().to_dict()
    # Unique speakers per WP (distinct non-null name values)
    speakers_per_wp: dict[str, int] = (
        sp[sp["name"].notna()]
        .groupby("wp_str")["name"]
        .nunique()
        .to_dict()
    )

    # ── Speaker type counts and word-count sums ───────────────────────────────
    type_counts: dict = {}
    type_wc_sum: dict = {}
    for wp, grp in sp.groupby("wp_str"):
        type_counts[wp] = grp.groupby("stype")["wc"].count().to_dict()
        type_wc_sum[wp] = grp.groupby("stype")["wc"].sum().to_dict()

    # ── Normalised party counts (8-group, for table_03 / fig_03) ─────────────
    sp_party = sp[sp["pnorm"].notna()]
    party_counts: dict = {}
    for wp, grp in sp_party.groupby("wp_str"):
        party_counts[wp] = grp.groupby("pnorm").size().to_dict()

    # ── Per-WP word-count lists (for mean/total in table_01 / fig_02) ─────────
    wc_all: dict = {
        wp: grp["wc"].tolist()
        for wp, grp in sp.groupby("wp_str")
    }

    # ── Notable speakers ──────────────────────────────────────────────────────
    notable_df = sp[sp["notable_type"].notna() & sp["name"].notna()]
    notable_speakers = _sets_from_groupby(
        notable_df, ["wp_str", "notable_type"], "name"
    )

    # ── Full-granularity party stats (table_05) ───────────────────────────────
    party_speech_counts: dict = {}
    for wp, grp in sp.groupby("wp_str"):
        party_speech_counts[wp] = grp.groupby("party_filled").size().to_dict()

    party_speaker_sets = _sets_from_groupby(
        sp[sp["name"].notna()], ["wp_str", "party_filled"], "name"
    )

    # ── Role category speaker sets (table_06) ─────────────────────────────────
    role_cat_speaker_sets = _sets_from_groupby(
        sp[sp["name"].notna()], ["wp_str", "stype"], "name"
    )

    # ── Raw role string stats (kept in data dict for reference) ───────────────
    role_raw_speech_counts: dict = {}
    for wp, grp in sp.groupby("wp_str"):
        role_raw_speech_counts[wp] = grp.groupby("role_filled").size().to_dict()

    role_raw_speaker_sets = _sets_from_groupby(
        sp[sp["name"].notna()], ["wp_str", "role_filled"], "name"
    )

    # ── Canonical role stats (table_07) ───────────────────────────────────────
    role_canonical_speech_counts: dict = {}
    for wp, grp in sp.groupby("wp_str"):
        role_canonical_speech_counts[wp] = grp.groupby("role_canonical").size().to_dict()

    role_canonical_speaker_sets = _sets_from_groupby(
        sp[sp["name"].notna()], ["wp_str", "role_canonical"], "name"
    )

    # ── Gender counts per WP — speech level (table_08 / fig_04) ─────────────
    gender_counts: dict = {}
    for wp, grp in sp.groupby("wp_str"):
        gender_counts[wp] = grp.groupby("gender").size().to_dict()

    # ── Gender counts per WP — speaker level (fig_04 panels C & D) ──────────
    # One row per (wp, name) — takes the first gender value for each speaker
    # within each WP so a speaker who appears in multiple WPs contributes once
    # per WP (consistent with "unique speakers per WP").
    _spk_gender = (
        sp[sp["name"].notna()]
        .drop_duplicates(subset=["wp_str", "name"])
        [["wp_str", "gender"]]
    )
    speaker_gender_counts: dict = {}
    for wp, grp in _spk_gender.groupby("wp_str"):
        speaker_gender_counts[wp] = grp.groupby("gender").size().to_dict()

    # ── Data completeness per WP (table_09 / fig_05) ──────────────────────────
    # Fraction of speeches where party / role / gender are known.
    sp["_party_known"]   = sp["party"].notna() & (sp["party_filled"] != "")
    sp["_role_known"]    = sp["role"].notna()  & (sp["role_filled"]  != "")
    # "Stammdaten gender" is more informative than "gender known":
    # almost all speeches get a gender (guesser fills the gaps), but only
    # ~81 % of speakers are linked to the authoritative Bundestag Stammdaten.
    sp["_gender_stamm"]  = sp["gender_source"].fillna("") == "stammdaten"

    completeness_per_wp: dict[str, dict[str, float]] = {}
    for wp, grp in sp.groupby("wp_str"):
        total = len(grp) or 1
        completeness_per_wp[wp] = {
            "party":   float(grp["_party_known"].sum()  / total),
            "role":    float(grp["_role_known"].sum()   / total),
            "gender":  float(grp["_gender_stamm"].sum() / total),
        }

    # ── Keyword / topic fractions per WP (fig_06) ─────────────────────────────
    print("  Computing keyword trends …", flush=True)
    text_col = sp["text"].fillna("")
    for label, pat in _KEYWORD_GROUPS:
        sp[f"_kw_{label}"] = text_col.str.contains(pat, case=False, na=False, regex=True)

    keyword_fracs: dict[str, dict[str, float]] = {}
    for wp, grp in sp.groupby("wp_str"):
        total = len(grp) or 1
        keyword_fracs[wp] = {
            label: float(grp[f"_kw_{label}"].sum() / total)
            for label, _ in _KEYWORD_GROUPS
        }

    # ── Mean speeches & speakers per session (fig_01 Panel C) ─────────────────
    # speeches/session: straightforward ratio of WP totals.
    mean_speeches_per_session: dict[str, float] = {
        wp: speeches_per_wp.get(wp, 0) / (sessions_per_wp.get(wp, 0) or 1)
        for wp in WPS
    }
    # speakers/session: must be the mean of *per-session* unique speaker counts,
    # NOT total unique WP speakers ÷ sessions (which gives ~4–6, meaningless).
    _sess_spk = (
        sp[sp["name"].notna()]
        .groupby(["wp_str", "session_id"])["name"]
        .nunique()                   # unique speakers within each session
        .reset_index(name="n_spk")
    )
    mean_speakers_per_session: dict[str, float] = (
        _sess_spk.groupby("wp_str")["n_spk"]
        .mean()
        .to_dict()
    )

    n_sess = sum(sessions_per_wp.values())
    n_sp   = sum(speeches_per_wp.values())
    print(f"Corpus: {n_sess:,} sessions, {n_sp:,} speeches.\n")

    return {
        "sessions_per_wp":       sessions_per_wp,
        "speeches_per_wp":       speeches_per_wp,
        "speakers_per_wp":       speakers_per_wp,
        "type_counts":           type_counts,
        "type_wc_sum":           type_wc_sum,
        "party_counts":          party_counts,
        "wc_all":                wc_all,
        "notable_speakers":      notable_speakers,
        "party_speech_counts":   party_speech_counts,
        "party_speaker_sets":    party_speaker_sets,
        "role_cat_speaker_sets": role_cat_speaker_sets,
        "role_raw_speech_counts":         role_raw_speech_counts,
        "role_raw_speaker_sets":          role_raw_speaker_sets,
        "role_canonical_speech_counts":   role_canonical_speech_counts,
        "role_canonical_speaker_sets":    role_canonical_speaker_sets,
        "gender_counts":                  gender_counts,
        "speaker_gender_counts":          speaker_gender_counts,
        "completeness_per_wp":            completeness_per_wp,
        "keyword_fracs":                  keyword_fracs,
        "mean_speeches_per_session":      mean_speeches_per_session,
        "mean_speakers_per_session":      mean_speakers_per_session,
        "speeches_df":                    sp,   # raw DataFrame for ad-hoc analyses
    }


# CSV writers — tables 01–07 (unchanged from d_01 in format)

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
    """Table 4: notable officeholders per WP."""
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
            ns  = data["notable_speakers"].get(wp, {})
            row = [wp]
            for col in notable_cols:
                names = sorted(ns.get(col, set()))
                row.append("; ".join(names))
            w.writerow(row)
    print(f"Saved: {path}")


def write_table_05(data: dict, out_dir: Path) -> None:
    """Table 5: full-granularity party statistics per WP + ALL aggregate."""
    path = out_dir / "table_05_party_stats.csv"

    sc = data["party_speech_counts"]
    ss = data["party_speaker_sets"]
    grand_total = sum(data["speeches_per_wp"].values()) or 1

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
            wp_sc    = sc.get(wp, {})
            wp_ss    = ss.get(wp, {})
            total_wp = data["speeches_per_wp"].get(wp, 0) or 1
            for party in sorted(wp_sc.keys()):
                n_sp     = wp_sc[party]
                n_unique = len(wp_ss.get(party, set()))
                share    = round(n_sp / total_wp, 6)
                w.writerow([wp, party, n_sp, n_unique, share])
        for party in sorted(total_sc.keys()):
            n_sp     = total_sc[party]
            n_unique = len(total_ss.get(party, set()))
            share    = round(n_sp / grand_total, 6)
            w.writerow(["ALL", party, n_sp, n_unique, share])
    print(f"Saved: {path}")


def write_table_06(data: dict, out_dir: Path) -> None:
    """Table 6: role-category statistics per WP + ALL aggregate."""
    path = out_dir / "table_06_role_stats.csv"

    tc  = data["type_counts"]
    rcs = data["role_cat_speaker_sets"]
    grand_total = sum(data["speeches_per_wp"].values()) or 1

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
            wp_tc    = tc.get(wp, {})
            wp_rcs   = rcs.get(wp, {})
            total_wp = data["speeches_per_wp"].get(wp, 0) or 1
            for rc in ALL_TYPES:
                n_sp     = wp_tc.get(rc, 0)
                n_unique = len(wp_rcs.get(rc, set()))
                share    = round(n_sp / total_wp, 6)
                w.writerow([wp, rc, n_sp, n_unique, share])
        for rc in ALL_TYPES:
            n_sp     = total_tc.get(rc, 0)
            n_unique = len(total_rcs.get(rc, set()))
            share    = round(n_sp / grand_total, 6)
            w.writerow(["ALL", rc, n_sp, n_unique, share])
    print(f"Saved: {path}")


def write_table_07(data: dict, out_dir: Path) -> None:
    """Table 7: canonical role statistics per WP + ALL aggregate.

    Raw role strings (e.g. "Bundesminister für Arbeit und Soziales",
    "Bundesministerin für Finanzen") are aggregated into canonical labels
    (e.g. "Bundesminister/in") that collapse male/female spelling variants
    and all ministry/department suffixes.  This makes the table directly
    usable for comparisons such as Bundeskanzler/in vs. Bundesminister/in
    speech counts over time.

    Rows follow the fixed order in ALL_CANONICAL_ROLES within each WP so
    the table can be pivoted without resorting.  An ALL aggregate block
    is appended.  The ``n_unique_speakers`` column counts distinct speaker
    names within the canonical role category per WP.
    """
    path = out_dir / "table_07_role_canonical_stats.csv"

    rc = data["role_canonical_speech_counts"]
    rs = data["role_canonical_speaker_sets"]
    grand_total = sum(data["speeches_per_wp"].values()) or 1

    # ALL-WP aggregates
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
        w.writerow(["wp", "role_canonical", "n_speeches",
                    "n_unique_speakers", "share_speeches"])
        for wp in WPS:
            wp_rc    = rc.get(wp, {})
            wp_rs    = rs.get(wp, {})
            total_wp = data["speeches_per_wp"].get(wp, 0) or 1
            for role in ALL_CANONICAL_ROLES:
                n_sp     = wp_rc.get(role, 0)
                n_unique = len(wp_rs.get(role, set()))
                share    = round(n_sp / total_wp, 6)
                w.writerow([wp, role, n_sp, n_unique, share])
        for role in ALL_CANONICAL_ROLES:
            n_sp     = total_rc.get(role, 0)
            n_unique = len(total_rs.get(role, set()))
            share    = round(n_sp / grand_total, 6)
            w.writerow(["ALL", role, n_sp, n_unique, share])
    print(f"Saved: {path}")


def write_table_08(data: dict, out_dir: Path) -> None:
    """Table 8: gender breakdown per WP.

    Columns: wp, n_female, n_male, n_unknown, total_speeches,
             pct_female, pct_male, pct_unknown.
    An ALL aggregate row is appended.
    """
    path = out_dir / "table_08_gender_over_time.csv"

    gc          = data["gender_counts"]
    grand_total = sum(data["speeches_per_wp"].values()) or 1
    grand_gc: dict[str, int] = {}
    for wp_dict in gc.values():
        for g, cnt in wp_dict.items():
            grand_gc[g] = grand_gc.get(g, 0) + cnt

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp", "n_female", "n_male", "n_unknown",
                    "total_speeches", "pct_female", "pct_male", "pct_unknown"])
        for wp in WPS:
            gd    = gc.get(wp, {})
            nf    = gd.get("female",  0)
            nm    = gd.get("male",    0)
            nu    = gd.get("unknown", 0)
            total = nf + nm + nu or 1
            w.writerow([
                wp, nf, nm, nu, total,
                round(nf / total * 100, 2),
                round(nm / total * 100, 2),
                round(nu / total * 100, 2),
            ])
        nf    = grand_gc.get("female",  0)
        nm    = grand_gc.get("male",    0)
        nu    = grand_gc.get("unknown", 0)
        total = grand_total
        w.writerow([
            "ALL", nf, nm, nu, total,
            round(nf / total * 100, 2),
            round(nm / total * 100, 2),
            round(nu / total * 100, 2),
        ])
    print(f"Saved: {path}")


def write_table_09(data: dict, out_dir: Path) -> None:
    """Table 9: metadata completeness per WP.

    For each WP: fraction of speeches where party, role, and gender are
    known (non-null / non-empty / non-'unknown').  An ALL aggregate row
    uses grand totals rather than an average of fractions.
    """
    path = out_dir / "table_09_data_completeness.csv"
    cp   = data["completeness_per_wp"]
    spw  = data["speeches_per_wp"]

    # Grand totals (weight by speech count, not simple mean of fractions)
    grand_total = sum(spw.values()) or 1
    grand: dict[str, float] = {"party": 0.0, "role": 0.0, "gender": 0.0}
    for wp in WPS:
        n = spw.get(wp, 0)
        for metric in grand:
            grand[metric] += cp.get(wp, {}).get(metric, 0.0) * n
    for metric in grand:
        grand[metric] /= grand_total

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["wp", "n_speeches",
                    "pct_party_known", "pct_role_known", "pct_gender_stammdaten"])
        for wp in WPS:
            n  = spw.get(wp, 0)
            cd = cp.get(wp, {"party": 0.0, "role": 0.0, "gender": 0.0})
            w.writerow([wp, n,
                        round(cd["party"]  * 100, 2),
                        round(cd["role"]   * 100, 2),
                        round(cd["gender"] * 100, 2)])
        w.writerow(["ALL", grand_total,
                    round(grand["party"]  * 100, 2),
                    round(grand["role"]   * 100, 2),
                    round(grand["gender"] * 100, 2)])
    print(f"Saved: {path}")


# Plot style

plt.rcParams.update({
    "font.family":       "sans-serif",
    "font.size":         12,
    "axes.titlesize":    13,
    "axes.labelsize":    12,
    "xtick.labelsize":   10,
    "ytick.labelsize":   10,
    "legend.fontsize":   10,
    "figure.dpi":        300,
    "pdf.fonttype":      42,   # TrueType fonts — stays sharp when scaled in LaTeX
    "ps.fonttype":       42,   # same for PostScript
    "axes.spines.top":   False,
    "axes.spines.right": False,
})

_BONN_COLOR   = "#4C72B0"
_BERLIN_COLOR = "#DD8452"

_PARTY_COLORS: dict[str, str] = {
    "CDU/CSU":    "#2E2E2E",
    "SPD":        "#E3000F",
    "FDP":        "#C9A800",
    "GRÜNEN/B90": "#1AA037",
    "PDS/Linke":  "#BE3075",
    "AfD":        "#009EE0",
    "KPD":        "#8B0000",
    "Sonst.":     "#999999",
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

_FEMALE_COLOR = "#C44E52"   # muted red
_MALE_COLOR   = "#4C72B0"   # blue

# English display names for speaker-type labels (used in figure legends/titles).
# Internal data keys stay German; only plot labels use these translations.
_TYPE_LABELS_EN: dict[str, str] = {
    "Abgeordnete/r":        "MdB (Bundestag Member)",
    "Präsidium":            "Präsidium (Speaker & Deputies)",
    "Bundeskanzler/in":     "Federal Chancellor",
    "Bundesminister/in":    "Federal Minister",
    "Parl. Staatssekr.":    "Parl. State Secretary",
    "Staatssekretär/in":    "State Secretary",
    "Staatsminister/in":    "Minister of State",
    "Ministerpräsident/in": "Minister-President",
    "Sonstige":             "Other",
}

# Party blocs for keyword-by-bloc and seats-vs-speeches analyses.
# Keys map to canonical party names returned by normalize_party().
_PARTY_BLOCS: dict[str, list[str]] = {
    "Left":         ["SPD", "GRÜNEN/B90", "PDS/Linke", "KPD"],
    "Centre-Right": ["CDU/CSU", "FDP"],
    "Far-Right":    ["AfD", "NPD"],
}
_BLOC_COLORS: dict[str, str] = {
    "Left":         "#C44E52",   # red
    "Centre-Right": "#2C5F8A",   # blue
    "Far-Right":    "#7B4BA0",   # purple
}


def _save(fig: plt.Figure, out_dir: Path, stem: str) -> None:
    """Save a figure as PDF."""
    path = out_dir / f"{stem}.pdf"
    fig.savefig(path, bbox_inches="tight")
    print(f"Saved: {path}")
    plt.close(fig)


# Figure 1 — Corpus size: speeches AND unique speakers per Wahlperiode

def fig1_corpus_size(data: dict, out_dir: Path) -> None:
    """Three-panel figure.

    Panel A: Bar chart of speeches per WP, coloured by city;
             secondary axis = sessions.
    Panel B: Bar chart of unique speakers per WP, coloured by city;
             secondary axis = sessions (same line for easy comparison).
    Panel C: Mean speeches per session and mean unique speakers per session
             per WP — normalises panels A/B for session count and shows
             whether individual sessions are getting longer or more diverse.
    """
    speeches      = [data["speeches_per_wp"].get(wp, 0)             for wp in WPS]
    speakers      = [data["speakers_per_wp"].get(wp, 0)             for wp in WPS]
    sessions      = [data["sessions_per_wp"].get(wp, 0)             for wp in WPS]
    mean_sp_sess  = [data["mean_speeches_per_session"].get(wp, 0.0) for wp in WPS]
    mean_spk_sess = [data["mean_speakers_per_session"].get(wp, 0.0) for wp in WPS]
    colors        = [_BONN_COLOR if WP_META[wp]["city"] == "Bonn"
                     else _BERLIN_COLOR for wp in WPS]
    x             = np.arange(len(WPS))
    xlabels       = [f"WP{wp}" for wp in WPS]

    annotations = [
        ("12", "Wiedervereinigung\n(WP12, 1990)"),
        ("14", "Umzug nach Berlin\n(WP14, 1998)"),
    ]

    fig, (ax_a, ax_b, ax_c) = plt.subplots(1, 3, figsize=(20, 5))

    # ── Shared legend handles ─────────────────────────────────────────────────
    bonn_patch   = mpatches.Patch(color=_BONN_COLOR,   label="Bonn era (WP01–13)")
    berlin_patch = mpatches.Patch(color=_BERLIN_COLOR, label="Berlin era (WP14–21)")
    sess_line    = plt.Line2D([0], [0], color="#444444", linestyle="--",
                               marker="o", markersize=4, label="Sessions (right axis)")

    def _annotate_panel(ax: plt.Axes, max_val: float) -> None:
        for wp_ann, txt in annotations:
            idx_a = WPS.index(wp_ann)
            ax.axvline(idx_a, color="#888888", linestyle=":", linewidth=0.9, zorder=2)
            ax.text(idx_a + 0.18, max_val * 0.88, txt,
                    fontsize=10, color="#555555", va="top", linespacing=1.4)

    def _style_secondary(ax2: plt.Axes, label: str) -> None:
        ax2.set_ylabel(label, color="#444444")
        ax2.tick_params(axis="y", colors="#444444")
        ax2.spines["right"].set_color("#888888")
        ax2.spines["top"].set_visible(False)

    # ── Panel A: speeches ─────────────────────────────────────────────────────
    ax_a.bar(x, speeches, color=colors, edgecolor="white",
             linewidth=0.5, alpha=0.88, zorder=3)
    ax_a.set_ylabel("Number of speeches")
    ax_a.set_xlabel("Legislative Period (Wahlperiode)")
    ax_a.set_xticks(x); ax_a.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax_a.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax_a.set_axisbelow(True)
    ax_a.yaxis.grid(True, linestyle="--", alpha=0.35, zorder=0)
    ax2_a = ax_a.twinx()
    ax2_a.plot(x, sessions, "o--", color="#444444", linewidth=1.3,
               markersize=4, alpha=0.75, zorder=4)
    _style_secondary(ax2_a, "Number of sessions")
    _annotate_panel(ax_a, max(speeches) if speeches else 1)
    ax_a.legend(handles=[bonn_patch, berlin_patch, sess_line],
                loc="upper left", framealpha=0.92, fontsize=10)

    # ── Panel B: unique speakers ───────────────────────────────────────────────
    ax_b.bar(x, speakers, color=colors, edgecolor="white",
             linewidth=0.5, alpha=0.88, zorder=3)
    ax_b.set_ylabel("Number of unique speakers")
    ax_b.set_xlabel("Legislative Period (Wahlperiode)")
    ax_b.set_xticks(x); ax_b.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax_b.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax_b.set_axisbelow(True)
    ax_b.yaxis.grid(True, linestyle="--", alpha=0.35, zorder=0)
    ax2_b = ax_b.twinx()
    ax2_b.plot(x, sessions, "o--", color="#444444", linewidth=1.3,
               markersize=4, alpha=0.75, zorder=4)
    _style_secondary(ax2_b, "Number of sessions")
    _annotate_panel(ax_b, max(speakers) if speakers else 1)
    ax_b.legend(handles=[bonn_patch, berlin_patch, sess_line],
                loc="upper left", framealpha=0.92, fontsize=10)

    # ── Panel C: session intensity (normalised) ────────────────────────────────
    ax_c.plot(x, mean_sp_sess,  "o-",  color=_BONN_COLOR,   linewidth=2,
              markersize=4, alpha=0.9, label="Speeches / session")
    ax_c.plot(x, mean_spk_sess, "s--", color=_BERLIN_COLOR, linewidth=2,
              markersize=4, alpha=0.9, label="Unique speakers / session")
    ax_c.set_ylabel("Mean count per session")
    ax_c.set_xlabel("Legislative Period (Wahlperiode)")
    ax_c.set_xticks(x); ax_c.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax_c.set_axisbelow(True)
    ax_c.yaxis.grid(True, linestyle="--", alpha=0.35)
    ax_c.legend(fontsize=10, framealpha=0.9, loc="upper left")
    _annotate_panel(ax_c, max(max(mean_sp_sess), max(mean_spk_sess)) if mean_sp_sess else 1)

    fig.tight_layout()
    _save(fig, out_dir, "fig_01_corpus_size")


# Figure 2a — Speaker type composition (fractions stacked bar)
# Figure 2b — Mean speech length by speaker type

def fig2a_speaker_type_fractions(data: dict, out_dir: Path) -> None:
    """Stacked bar chart: fraction of speeches by speaker type per WP."""
    fracs: dict[str, list[float]] = {}
    for t in ALL_TYPES:
        fracs[t] = []
        for wp in WPS:
            counts = data["type_counts"].get(wp, {})
            total  = sum(counts.values()) or 1
            fracs[t].append(counts.get(t, 0) / total)

    x       = np.arange(len(WPS))
    xlabels = [f"WP{wp}" for wp in WPS]

    fig, ax = plt.subplots(figsize=(13, 5))

    bottoms = np.zeros(len(WPS))
    for t in ALL_TYPES:
        ax.bar(x, fracs[t], bottom=bottoms,
               color=_TYPE_COLORS[t],
               label=_TYPE_LABELS_EN.get(t, t),
               edgecolor="white", linewidth=0.25)
        bottoms += np.array(fracs[t])

    ax.set_xticks(x)
    ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax.set_ylabel("Fraction of speeches")
    ax.set_xlabel("Legislative Period (Wahlperiode)")
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1, decimals=0))
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)
    ax.legend(loc="lower right", fontsize=10, framealpha=0.88,
              handlelength=1.2, borderpad=0.6)

    fig.tight_layout()
    _save(fig, out_dir, "fig_02a_speaker_type_fractions")


def fig2b_speech_length(data: dict, out_dir: Path) -> None:
    """Line chart: mean speech length (words) by speaker type per WP."""
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

    fig, ax = plt.subplots(figsize=(13, 5))

    ax.plot(x, mean_wc_overall, "k-o", linewidth=2, markersize=4,
            label="All speeches", zorder=5)
    line_styles = {
        "Abgeordnete/r":    ("--", 1.4),
        "Bundesminister/in":("-.", 1.4),
        "Bundeskanzler/in": (":",  1.8),
        "Präsidium":        ("--", 1.2),
    }
    for t, (ls, lw) in line_styles.items():
        vals = mean_wc_by_type[t]
        ax.plot(x, vals, ls, color=_TYPE_COLORS[t],
                linewidth=lw, label=_TYPE_LABELS_EN.get(t, t), alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax.set_ylabel("Mean speech length (words)")
    ax.set_xlabel("Legislative Period (Wahlperiode)")
    ax.legend(loc="upper left", fontsize=10, framealpha=0.88)
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)

    fig.tight_layout()
    _save(fig, out_dir, "fig_02b_speech_length")


# Figure 3 — Party speech share evolution

def fig3_party_evolution(data: dict, out_dir: Path) -> None:
    """Line chart of party speech shares per WP."""
    fracs: dict[str, list[float]] = {p: [] for p in ALL_PARTIES}
    for wp in WPS:
        counts = data["party_counts"].get(wp, {})
        total  = sum(counts.get(p, 0) for p in ALL_PARTIES) or 1
        for p in ALL_PARTIES:
            fracs[p].append(counts.get(p, 0) / total)

    x       = np.arange(len(WPS))
    xlabels = [f"WP{wp}" for wp in WPS]

    fig, ax = plt.subplots(figsize=(13, 5.5))

    bonn_end = WPS.index("13")
    ax.axvspan(-0.5, bonn_end + 0.5,
               alpha=0.04, color=_BONN_COLOR,  zorder=0)
    ax.axvspan(bonn_end + 0.5, len(WPS) - 0.5,
               alpha=0.04, color=_BERLIN_COLOR, zorder=0)

    for p in ALL_PARTIES:
        vals = fracs[p]
        if max(vals) < 0.003:
            continue
        thick = max(vals) > 0.05
        ax.plot(
            x, [v * 100 for v in vals],
            "o-" if thick else ".-",
            color=_PARTY_COLORS[p],
            linewidth=2.0 if thick else 1.0,
            markersize=4   if thick else 2,
            label=p,
            alpha=0.9,
        )

    for wp_ann, txt in [
        ("12", "Reunification\n(1990)"),
        ("14", "→ Berlin\n(1999)"),
    ]:
        idx_a = WPS.index(wp_ann)
        ax.axvline(idx_a, color="#AAAAAA", linestyle=":", linewidth=0.9, zorder=1)
        ax.text(idx_a + 0.15, ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 60,
                txt, fontsize=10, color="#666666", va="top", linespacing=1.4)

    ax.set_xticks(x)
    ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax.set_ylabel("Share of speeches with known party (%)")
    ax.set_xlabel("Legislative Period (Wahlperiode)")
    ax.legend(loc="upper right", fontsize=10, framealpha=0.92,
              ncol=2, handlelength=1.5)
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)
    ax.set_xlim(-0.5, len(WPS) - 0.5)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    _save(fig, out_dir, "fig_03_party_evolution")


# Figure 4 — Gender over time  (NEW — not in d_01)

def _gender_series(gender_dict: dict) -> tuple:
    """Build per-WP gender series from a gender_counts dict."""
    pct_f, pct_m, pct_u, n_f, n_m = [], [], [], [], []
    for wp in WPS:
        gd    = gender_dict.get(wp, {})
        nf    = gd.get("female",  0)
        nm    = gd.get("male",    0)
        nu    = gd.get("unknown", 0)
        total = nf + nm + nu or 1
        pct_f.append(nf / total * 100)
        pct_m.append(nm / total * 100)
        pct_u.append(nu / total * 100)
        n_f.append(nf)
        n_m.append(nm)
    return pct_f, pct_m, pct_u, n_f, n_m


def _gender_two_panel(pct_f, pct_m, pct_u, n_f, n_m,
                      ylabel_a: str, ylabel_b: str) -> plt.Figure:
    """Shared layout: stacked area (left) + grouped bars (right)."""
    x       = np.arange(len(WPS))
    xlabels = [f"WP{wp}" for wp in WPS]
    _ann    = [("12", "Reunification\n(1990)"), ("14", "→ Berlin\n(1999)")]

    fig, (ax_a, ax_b) = plt.subplots(1, 2, figsize=(15, 5))

    # Panel A — stacked area
    ax_a.stackplot(x, pct_f, pct_m, pct_u,
                   labels=["Female", "Male", "_nolegend_"],
                   colors=[_FEMALE_COLOR, _MALE_COLOR, "#CCCCCC"],
                   alpha=0.85)
    ax_a.set_xticks(x)
    ax_a.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax_a.set_ylabel(ylabel_a)
    ax_a.set_xlabel("Legislative Period (Wahlperiode)")
    ax_a.set_ylim(0, 100)
    ax_a.legend(loc="upper left", fontsize=10, framealpha=0.9)
    ax_a.set_axisbelow(True)
    ax_a.yaxis.grid(True, linestyle="--", alpha=0.3)
    for wp_ann, txt in _ann:
        idx_a = WPS.index(wp_ann)
        ax_a.axvline(idx_a, color="#888888", linestyle=":", linewidth=0.9)
        ax_a.text(idx_a + 0.15, 95, txt,
                  fontsize=10, color="#555555", va="top", linespacing=1.4)

    # Panel B — grouped bars
    bar_w = 0.4
    ax_b.bar(x - bar_w / 2, n_f, bar_w, label="Female",
             color=_FEMALE_COLOR, alpha=0.85, edgecolor="white", linewidth=0.3)
    ax_b.bar(x + bar_w / 2, n_m, bar_w, label="Male",
             color=_MALE_COLOR,   alpha=0.85, edgecolor="white", linewidth=0.3)
    ax_b.set_xticks(x)
    ax_b.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax_b.set_ylabel(ylabel_b)
    ax_b.set_xlabel("Legislative Period (Wahlperiode)")
    ax_b.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax_b.legend(fontsize=10, framealpha=0.9)
    ax_b.set_axisbelow(True)
    ax_b.yaxis.grid(True, linestyle="--", alpha=0.3)

    fig.tight_layout()
    return fig


def fig4a_gender_speeches(data: dict, out_dir: Path) -> None:
    """Speech-level gender figure: fraction (A) + absolute counts (B)."""
    pct_f, pct_m, pct_u, n_f, n_m = _gender_series(data["gender_counts"])
    fig = _gender_two_panel(
        pct_f, pct_m, pct_u, n_f, n_m,
        ylabel_a = "Share of speeches (%)",
        ylabel_b = "Number of speeches",
    )
    _save(fig, out_dir, "fig_04a_gender_speeches")


def fig4b_gender_speakers(data: dict, out_dir: Path) -> None:
    """Speaker-level gender figure: fraction (A) + absolute counts (B)."""
    pct_f, pct_m, pct_u, n_f, n_m = _gender_series(data["speaker_gender_counts"])
    fig = _gender_two_panel(
        pct_f, pct_m, pct_u, n_f, n_m,
        ylabel_a = "Share of unique speakers (%)",
        ylabel_b = "Number of unique speakers",
    )
    _save(fig, out_dir, "fig_04b_gender_speakers")


# Figure 5 — Data completeness heatmap

def fig5_data_completeness(data: dict, out_dir: Path) -> None:
    """Heatmap: percentage of speeches with party / role / gender known per WP.

    Rows = metadata field (party, role, gender).
    Columns = Wahlperiode.
    Cell colour encodes completeness 0–100 %.
    Each cell is annotated with the rounded percentage.
    """
    metrics       = ["party", "role", "gender"]
    metric_labels = ["Party identified", "Role identified", "Gender (Stammdaten)"]
    cp           = data["completeness_per_wp"]

    # Build 2-D array: rows = metrics, cols = WPs
    arr = np.array([
        [cp.get(wp, {}).get(m, 0.0) * 100 for wp in WPS]
        for m in metrics
    ])

    xlabels = [f"WP{wp}" for wp in WPS]
    fig, ax  = plt.subplots(figsize=(14, 3.2))

    im = ax.imshow(arr, aspect="auto", cmap="YlGn",
                   vmin=0, vmax=100, interpolation="nearest")

    ax.set_xticks(np.arange(len(WPS)))
    ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax.set_yticks(np.arange(len(metrics)))
    ax.set_yticklabels(metric_labels, fontsize=10)

    # Annotate each cell
    for row_i, metric in enumerate(metrics):
        for col_j, wp in enumerate(WPS):
            val = arr[row_i, col_j]
            txt_color = "black" if val > 20 else "white"
            ax.text(col_j, row_i, f"{val:.0f}%",
                    ha="center", va="center",
                    fontsize=10, color=txt_color)

    # Colour bar
    cbar = fig.colorbar(im, ax=ax, orientation="vertical",
                        fraction=0.015, pad=0.02)
    cbar.set_label("% of speeches", fontsize=10)
    cbar.ax.tick_params(labelsize=7.5)

    ax.set_xlabel("Legislative Period (Wahlperiode)")

    # Era divider between WP13 and WP14 — label placed inside axes at bottom row
    era_x = WPS.index("14") - 0.5
    ax.axvline(era_x, color="#555555", linewidth=1.2, linestyle="--")
    ax.text(era_x + 0.15, 2.45, "→ Berlin", fontsize=10,
            color="#555555", va="center", ha="left")

    fig.tight_layout()
    _save(fig, out_dir, "fig_05_data_completeness")


# Figure 6 — Topic / keyword time series (Technical Validation)

def fig6_topic_trends(data: dict, out_dir: Path) -> None:
    """Line chart: fraction of speeches per WP mentioning each keyword group.

    Five anchor terms are tracked whose expected peaks are historically known.
    Alignment with those peaks validates the temporal labelling of the corpus.
    """
    kf      = data["keyword_fracs"]
    x       = np.arange(len(WPS))
    xlabels = [f"WP{wp}" for wp in WPS]

    fig, ax = plt.subplots(figsize=(13, 5))

    for (label, _), color in zip(_KEYWORD_GROUPS, _KEYWORD_COLORS):
        vals = [kf.get(wp, {}).get(label, 0.0) * 100 for wp in WPS]
        # Use thicker line + circles for terms with noticeable peaks
        peak = max(vals) if vals else 0
        lw = 2.2 if peak > 1.0 else 1.4
        ms = 4   if peak > 1.0 else 2
        ax.plot(x, vals, "o-", color=color, linewidth=lw,
                markersize=ms, label=label, alpha=0.9)

    # Historical event annotations.
    # Mauerfall omitted — too close to the WP12 Wiedervereinigung marker.
    # tier: 0 = top band, 1 = mid band, 2 = lower band (all above data lines
    # because we add 30 % headroom to ylim below).
    _events = [
        ("08", "German\nAutumn\n(1977)",           "#C44E52", 0),
        ("12", "Reunifi-\ncation\n(1990)",          "#888888", 1),
        ("14", "→ Berlin\n(1999)",                 "#888888", 0),
        ("15", "9/11\n(2001)",                     "#C44E52", 2),
        ("16", "Financial\nCrisis\n(2008)",        "#E07020", 1),
        ("19", "COVID\n(2020)",                    "#7B4BA0", 0),
    ]
    for wp_ann, txt, col, _ in _events:
        if wp_ann in WPS:
            idx_a = WPS.index(wp_ann)
            ax.axvline(idx_a, color=col, linestyle=":", linewidth=0.9, alpha=0.6, zorder=1)

    ax.set_xticks(x)
    ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax.set_ylabel("Speeches mentioning term (%)")
    ax.set_xlabel("Legislative Period (Wahlperiode)")
    ax.legend(loc="upper left", fontsize=10, framealpha=0.92,
              handlelength=1.5, ncol=1)
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)
    ax.set_xlim(-0.5, len(WPS) - 0.5)

    # Add 30 % headroom above data so labels never overlap lines.
    y_data_max = ax.get_ylim()[1]
    y_top = y_data_max * 1.30
    ax.set_ylim(0, y_top)

    # Three label tiers, all within the headroom band.
    tier_fracs = [0.97, 0.88, 0.79]
    for wp_ann, txt, col, tier in _events:
        if wp_ann in WPS:
            idx_a = WPS.index(wp_ann)
            ax.text(idx_a + 0.15, y_top * tier_fracs[tier], txt,
                    fontsize=10, color=col, va="top", linespacing=1.3,
                    alpha=0.88)

    # WP21 footnote — partial term, data may be volatile
    fig.text(0.99, 0.01, "* WP21 incomplete (ongoing term)",
             ha="right", fontsize=10, color="#888888")

    fig.tight_layout()
    _save(fig, out_dir, "fig_06_topic_trends")


# Additional analyses (output → outputs/additional_analyses/)

# Figure 7 — Party speech share vs. seat share

def fig7_seats_vs_speeches(data: dict, out_dir: Path, seats_csv: Path) -> None:
    """Compare each party's share of speeches to its share of Bundestag seats.

    Requires docs/bundestag_election_results.csv with columns:
        wp (int), party (str), n_seats (int), total_seats (int)
    Party names must match the canonical names returned by normalize_party().
    """
    import csv as _csv

    if not seats_csv.exists():
        print(f"  SKIP fig_07: seats CSV not found at {seats_csv}")
        return

    # Load seat shares
    seat_shares: dict[tuple, float] = {}   # (wp_str, party) → share
    with open(seats_csv, newline="", encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            wp_str = f"{int(row['wp']):02d}"
            party  = row["party"].strip()
            share  = int(row["n_seats"]) / int(row["total_seats"])
            seat_shares[(wp_str, party)] = share

    # Speech shares from corpus (already normalised by normalize_party)
    speech_shares: dict[tuple, float] = {}
    total_by_wp = data["speeches_per_wp"]
    for wp in WPS:
        total = total_by_wp.get(wp, 0) or 1
        for party, cnt in data["party_counts"].get(wp, {}).items():
            speech_shares[(wp, party)] = cnt / total

    # Parties with seat data — exclude KPD (single WP, near-empty panel)
    _FIG7_EXCLUDE = {"KPD"}
    parties_with_seats = sorted({p for (_, p) in seat_shares
                                 if p not in _FIG7_EXCLUDE})

    # File-safe suffix for each party name
    def _party_slug(p: str) -> str:
        return (p.lower()
                 .replace("/", "_")
                 .replace("ü", "ue")
                 .replace("ä", "ae")
                 .replace("ö", "oe")
                 .replace(" ", "_")
                 .replace(".", ""))

    for party in parties_with_seats:
        color = _PARTY_COLORS.get(party, "#888888")

        x_seats, y_seats, x_speech, y_speech = [], [], [], []
        for wp in WPS:
            s_share  = seat_shares.get((wp, party))
            sp_share = speech_shares.get((wp, party), 0.0)
            if s_share is not None:
                xi = WPS.index(wp)
                x_seats.append(xi);  y_seats.append(s_share  * 100)
                x_speech.append(xi); y_speech.append(sp_share * 100)

        fig, ax = plt.subplots(figsize=(9, 4))

        ax.plot(x_seats,  y_seats,  "o-",  color=color, linewidth=1.8,
                markersize=5, label="Seat share",   alpha=0.9)
        ax.plot(x_speech, y_speech, "s--", color=color, linewidth=1.8,
                markersize=5, label="Speech share", alpha=0.6)

        ax.set_xticks(range(len(WPS)))
        ax.set_xticklabels([f"WP{w}" for w in WPS], rotation=90, fontsize=9)
        ax.set_xlabel("Legislative Period (Wahlperiode)", fontsize=10)
        ax.set_ylabel("Share (%)", fontsize=10)
        ax.yaxis.set_major_formatter(mticker.PercentFormatter(decimals=0))
        ax.set_ylim(0, None)
        ax.set_axisbelow(True)
        ax.yaxis.grid(True, linestyle="--", alpha=0.3)
        ax.legend(fontsize=10, framealpha=0.9)

        fig.tight_layout()
        slug = _party_slug(party)
        _save(fig, out_dir, f"fig_07_{slug}_seats_vs_speeches")


# Figure 8 — Keyword trends broken down by party bloc

def fig8_keyword_by_bloc(data: dict, out_dir: Path) -> None:
    """For each keyword group, plot keyword mention rate per WP broken down
    by party bloc (Left / Centre-Right / Far-Right)."""

    sp = data["speeches_df"]

    # Map party → bloc
    def _bloc(party_raw):
        canon = normalize_party(party_raw)
        for bloc, members in _PARTY_BLOCS.items():
            if canon in members:
                return bloc
        return None

    sp = sp.copy()
    sp["_bloc"] = sp["party"].map(_bloc)
    sp_with_bloc = sp[sp["_bloc"].notna()]

    def _topic_slug(label: str) -> str:
        return (label.lower()
                     .replace("/", "_")
                     .replace(" ", "_")
                     .replace("-", "_"))

    for label, pattern in _KEYWORD_GROUPS:
        sp_with_bloc = sp_with_bloc.copy()
        sp_with_bloc["_hit"] = sp_with_bloc["text"].str.contains(
            pattern, regex=True, na=False
        )

        fig, ax = plt.subplots(figsize=(9, 4))

        for bloc, color in _BLOC_COLORS.items():
            vals = []
            for wp in WPS:
                grp = sp_with_bloc[
                    (sp_with_bloc["wp_str"] == wp) & (sp_with_bloc["_bloc"] == bloc)
                ]
                total = len(grp) or 1
                vals.append(grp["_hit"].sum() / total * 100)
            ax.plot(range(len(WPS)), vals, "-o", color=color,
                    linewidth=1.5, markersize=3, label=bloc, alpha=0.85)

        ax.set_xticks(range(len(WPS)))
        ax.set_xticklabels([f"WP{w}" for w in WPS], rotation=90, fontsize=9)
        ax.set_xlabel("Legislative Period (Wahlperiode)", fontsize=10)
        ax.set_ylabel("Speeches mentioning term (%)", fontsize=10)
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
        ax.set_axisbelow(True)
        ax.yaxis.grid(True, linestyle="--", alpha=0.3)
        ax.legend(fontsize=10, framealpha=0.9)

        fig.tight_layout()
        slug = _topic_slug(label)
        _save(fig, out_dir, f"fig_08_{slug}_keyword_trends")


# Figure 9 — Speech length over time with historical event annotations

def fig9_speech_length_rules(data: dict, out_dir: Path) -> None:
    """Mean speech length per WP with historical event markers.

    Shows the same four speaker types as fig_02b but adds vertical annotations
    for key events that affected the Bundestag's debate format:
      - WP12: Reunification (enlarged parliament, new parties)
      - WP14: Move to Berlin (new chamber, procedural reforms)
      - WP19: AfD enters parliament (debate format changes)
    """
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

    fig, ax = plt.subplots(figsize=(13, 5))

    ax.plot(x, mean_wc_overall, "k-o", linewidth=2, markersize=4,
            label="All speeches (corpus mean)", zorder=5)
    line_styles = {
        "Abgeordnete/r":    ("--", 1.4),
        "Bundesminister/in":("-.", 1.4),
        "Bundeskanzler/in": (":",  1.8),
        "Präsidium":        ("--", 1.2),
    }
    for t, (ls, lw) in line_styles.items():
        vals = mean_wc_by_type[t]
        ax.plot(x, vals, ls, color=_TYPE_COLORS[t],
                linewidth=lw, label=_TYPE_LABELS_EN.get(t, t), alpha=0.85)

    # Historical event annotations
    _events = [
        ("12", "Reunification\n(1990)",      "#888888"),
        ("14", "→ Berlin\n(1999)",           "#888888"),
        ("19", "AfD enters\nparliament",     "#7B4BA0"),
    ]
    y_top = ax.get_ylim()[1]
    for wp_ann, txt, col in _events:
        if wp_ann in WPS:
            idx_a = WPS.index(wp_ann)
            ax.axvline(idx_a, color=col, linestyle=":", linewidth=0.9, alpha=0.55)
            ax.text(idx_a + 0.12, y_top * 0.97, txt,
                    fontsize=10, color=col, va="top", linespacing=1.3, alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=10)
    ax.set_ylabel("Mean speech length (words)")
    ax.set_xlabel("Legislative Period (Wahlperiode)")
    ax.legend(loc="upper right", fontsize=10, framealpha=0.88)
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)

    fig.tight_layout()
    _save(fig, out_dir, "fig_09_speech_length_annotated")


# Main runner

def run(dataset_dir: str | None = None,
        output_dir:  str | None = None,
        seats_csv:   str | None = None) -> None:

    ds_dir = Path(dataset_dir or _DEFAULT_DATASET)
    if not ds_dir.exists():
        print(f"ERROR: Dataset folder not found: {ds_dir}")
        sys.exit(1)

    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = ds_dir.parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Subdirectory for title-free, 300-dpi publication figures
    nt_dir = out_dir / "figures_no_titles"
    nt_dir.mkdir(parents=True, exist_ok=True)
    nt_add_dir = nt_dir / "additional_analyses"
    nt_add_dir.mkdir(parents=True, exist_ok=True)

    print(f"Dataset              : {ds_dir}")
    print(f"Output               : {out_dir}")
    print(f"Figures (no titles)  : {nt_dir}")
    print()

    data = load_corpus(ds_dir)

    print("Writing tables ...")
    write_table_01(data, out_dir)
    write_table_02(data, out_dir)
    write_table_03(data, out_dir)
    write_table_04(data, out_dir)
    write_table_05(data, out_dir)
    write_table_06(data, out_dir)
    write_table_07(data, out_dir)
    write_table_08(data, out_dir)
    write_table_09(data, out_dir)

    _seats = Path(seats_csv) if seats_csv else Path(__file__).parent.parent / "docs" / "bundestag_election_results.csv"

    # All figures are generated without embedded titles (titles are provided
    # by LaTeX \caption{} in the paper).  Figures are saved to figures_no_titles/
    # (main figs 01–06) and figures_no_titles/additional_analyses/ (figs 07–09).
    print("\nGenerating figures (no titles, 300 dpi) → figures_no_titles/ ...")
    fig1_corpus_size(data, nt_dir)
    fig2a_speaker_type_fractions(data, nt_dir)
    fig2b_speech_length(data, nt_dir)
    fig3_party_evolution(data, nt_dir)
    fig4a_gender_speeches(data, nt_dir)
    fig4b_gender_speakers(data, nt_dir)
    fig5_data_completeness(data, nt_dir)
    fig6_topic_trends(data, nt_dir)

    print("\nGenerating additional analyses (no titles, 300 dpi) → figures_no_titles/additional_analyses/ ...")
    fig7_seats_vs_speeches(data, nt_add_dir, _seats)
    fig8_keyword_by_bloc(data, nt_add_dir)
    fig9_speech_length_rules(data, nt_add_dir)

    print("\nAll done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate Bundestag corpus statistics and figures from "
                    "the published dataset (speeches.parquet + sessions.parquet)."
    )
    parser.add_argument(
        "--dataset-dir", default=None, metavar="PATH",
        help=f"Path to bundestag_dataset_v1/. Default: {_DEFAULT_DATASET}",
    )
    parser.add_argument(
        "--output-dir", default=None, metavar="PATH",
        help="Output directory for tables and figures. "
             "Default: <dataset>/../outputs",
    )
    parser.add_argument(
        "--seats-csv", default=None, metavar="PATH",
        help="Path to docs/bundestag_election_results.csv for fig_07 "
             "(seats vs speeches). Default: docs/bundestag_election_results.csv "
             "relative to the project root. Fig_07 is skipped if file not found.",
    )
    args = parser.parse_args()
    run(dataset_dir=args.dataset_dir,
        output_dir=args.output_dir,
        seats_csv=args.seats_csv)
