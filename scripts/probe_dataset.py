"""Broad integrity probe over bundestag_dataset_current.

Deliberately looks for things c_01 (text artefacts) and h_01 (speech-level
flags) do not: structural invariants, cross-field consistency, and values that
are individually well-formed but impossible in context.

Each check prints a count and a few examples. Nothing is modified.
"""
import pathlib, re, sys, collections, datetime as dt
import pandas as pd

B = pathlib.Path(__file__).resolve().parent.parent  # project root, from this file
sp = pd.read_parquet(B / "bundestag_dataset_current" / "speeches.parquet")
print(f"{len(sp):,} speeches, {sp['session_id'].nunique():,} sessions, "
      f"{len(sp.columns)} columns\n")

findings = []


def check(label, mask_or_df, note="", ex=None, limit=4):
    d = sp[mask_or_df] if hasattr(mask_or_df, "dtype") else mask_or_df
    n = len(d)
    findings.append((n, label))
    flag = "." if n == 0 else "!"
    print(f"[{flag}] {label}: {n:,} {note}")
    if n and ex is not None:
        for v in list(dict.fromkeys(map(str, ex(d))))[:limit]:
            print(f"        {v}")
    return d


print("=== 1. structural invariants " + "=" * 50)
check("duplicate speech_id", sp[sp.duplicated("speech_id", keep=False)],
      ex=lambda d: d["speech_id"].head(6))
check("duplicate (session_id, sequence)",
      sp[sp.duplicated(["session_id", "sequence"], keep=False)],
      ex=lambda d: (d["session_id"] + " #" + d["sequence"].astype(str)).head(6))
g = sp.groupby("session_id")["sequence"]
gaps = g.agg(lambda s: int(s.max() - s.min() + 1 - s.nunique()))
check("sessions with a gap in sequence", sp[sp["session_id"].isin(gaps[gaps > 0].index)],
      note=f"({int((gaps > 0).sum())} sessions)",
      ex=lambda d: sorted(set(d["session_id"]))[:6])
bad_off = sp.sort_values(["session_id", "sequence"]).groupby("session_id")["char_offset"] \
            .apply(lambda s: bool((s.diff().dropna() < 0).any()))
check("sessions where char_offset goes backwards",
      sp[sp["session_id"].isin(bad_off[bad_off].index)],
      note=f"({int(bad_off.sum())} sessions)",
      ex=lambda d: sorted(set(d["session_id"]))[:6])

print("\n=== 2. text " + "=" * 64)
txt = sp["text"].fillna("")
check("empty text", sp[txt.str.strip() == ""], ex=lambda d: d["speech_id"].head(4))
check("text under 10 chars", sp[(txt.str.len() < 10) & (txt.str.strip() != "")],
      ex=lambda d: d["text"].head(4).map(repr))
check("text begins with an interjection block",
      sp[txt.str.match(r"\s*\((?:Beifall|Zuruf|Heiterkeit|Lachen|Widerspruch)", na=False)],
      ex=lambda d: d["text"].str[:70].head(4).map(repr))
check("exact duplicate (session, name, text)",
      sp[sp.duplicated(["session_id", "name", "text"], keep=False) & (txt.str.len() > 200)],
      ex=lambda d: (d["speech_id"] + "  " + d["text"].str[:48]).head(4))

print("\n=== 3. names " + "=" * 63)
nm = sp["name"].fillna("")
check("name is null or blank", sp[nm.str.strip() == ""], ex=lambda d: d["speech_id"].head(4))
check("name contains a digit", sp[nm.str.contains(r"\d", na=False)],
      ex=lambda d: d["name"].value_counts().head(5).items())
check("name starts lower-case", sp[nm.str.match(r"[a-zäöüß]", na=False)],
      ex=lambda d: d["name"].value_counts().head(5).items())
check("name longer than 45 chars", sp[nm.str.len() > 45],
      ex=lambda d: d["name"].value_counts().head(4).items())
check("name contains a party label",
      sp[nm.str.contains(r"\b(?:SPD|CDU|CSU|FDP|GRÜNE|LINKE|AfD|KPD|BSW)\b", na=False)],
      ex=lambda d: d["name"].value_counts().head(5).items())
check("name contains a role word",
      sp[nm.str.contains(r"\b(?:Präsident|Minister|Staatssekretär|Abgeordnete[nr]?|"
                         r"Berichterstatter|Schriftführer)\b", na=False)],
      ex=lambda d: d["name"].value_counts().head(5).items())
check("name is a single word under 4 chars", sp[nm.str.match(r"^\S{1,3}$", na=False)],
      ex=lambda d: d["name"].value_counts().head(6).items())

print("\n=== 4. cross-field consistency " + "=" * 45)
mixed_g = sp.groupby("name")["gender"].nunique()
check("speakers with more than one gender",
      sp[sp["name"].isin(mixed_g[mixed_g > 1].index)],
      note=f"({int((mixed_g > 1).sum())} names)",
      ex=lambda d: d["name"].value_counts().head(6).items())
nid = sp[sp["stammdaten_id"].notna()].groupby("stammdaten_id")["name"].nunique()
check("one stammdaten_id used for several names",
      sp[sp["stammdaten_id"].isin(nid[nid > 1].index)],
      note=f"({int((nid > 1).sum())} ids)",
      ex=lambda d: [f"{i}: {list(v)[:3]}" for i, v in
                    d.groupby("stammdaten_id")["name"].unique().head(3).items()])
bad_year = sp[sp["date_iso"].notna() &
              (sp["date_iso"].str[:4].astype(str) != sp["year"].astype(str))]
check("year disagrees with date_iso", bad_year,
      ex=lambda d: (d["session_id"] + " " + d["date_iso"] + " vs " +
                    d["year"].astype(str)).head(4))
check("city is neither Bonn nor Berlin",
      sp[~sp["city"].isin(["Bonn", "Berlin"]) & sp["city"].notna()],
      ex=lambda d: d["city"].value_counts().head(5).items())

print("\n=== 5. party plausible for the term " + "=" * 40)
ERA = {"AfD": (18, 21), "BSW": (20, 21), "GRÜNE": (10, 13), "BÜNDNIS 90/DIE GRÜNEN": (12, 21),
       "DIE LINKE": (16, 21), "PDS": (12, 15), "PDS/Linke Liste": (12, 13),
       "KPD": (1, 2), "WAV": (1, 2), "BP": (1, 2), "DP": (1, 4), "GB/BHE": (2, 3),
       "SRP": (1, 1), "FU": (1, 2), "NR": (1, 1), "FVP": (2, 2), "DA": (2, 2),
       "Z": (1, 2), "SSW": (1, 1)}
rows = []
for p, (lo, hi) in ERA.items():
    d = sp[(sp["party"] == p) & ((sp["wp"] < lo) | (sp["wp"] > hi))]
    if len(d):
        rows.append((p, lo, hi, len(d), sorted(d["wp"].unique())))
findings.append((sum(r[3] for r in rows), "party outside its parliamentary era"))
print(f"[{'.' if not rows else '!'}] party outside its parliamentary era: "
      f"{sum(r[3] for r in rows):,}")
for p, lo, hi, n, wps in sorted(rows, key=lambda r: -r[3]):
    print(f"        {p:24s} expected WP{lo}-{hi}, found {n:5d} in WP {wps}")

print("\n=== 6. roles " + "=" * 63)
rl = sp["role"].fillna("")
check("role contains a digit", sp[rl.str.contains(r"\d", na=False)],
      ex=lambda d: d["role"].value_counts().head(5).items())
check("role longer than 90 chars", sp[rl.str.len() > 90],
      ex=lambda d: d["role"].value_counts().head(3).items())
check("role looks like a sentence (has a lower-case verb ending)",
      sp[rl.str.contains(r"\b(?:ist|hat|wird|haben|werden|sind)\b", na=False)],
      ex=lambda d: d["role"].value_counts().head(4).items())

print("\n=== 7. location " + "=" * 60)
lc = sp["location"].fillna("")
check("location contains a digit", sp[lc.str.contains(r"\d", na=False)],
      ex=lambda d: d["location"].value_counts().head(5).items())
check("location longer than 30 chars", sp[lc.str.len() > 30],
      ex=lambda d: d["location"].value_counts().head(4).items())
check("location has an unbalanced bracket",
      sp[lc.str.contains(r"[()\[\]]", na=False)],
      ex=lambda d: d["location"].value_counts().head(5).items())
check("location looks OCR-split (' rt ', ' li ', ' tr ')",
      sp[lc.str.contains(r"[a-zäöü]\s(?:rt|li|tr|ti|rn)\s?[a-zäöü]", na=False)],
      ex=lambda d: d["location"].value_counts().head(6).items())

print("\n" + "=" * 78)
hot = sorted([f for f in findings if f[0]], reverse=True)
print(f"{len(hot)} checks returned rows; largest first:")
for n, label in hot:
    print(f"   {n:8,}  {label}")
