"""Would the appendix-header gender rule ever contradict an authoritative source?

f_01 applies `anlagen_header` only where the gender is still unknown, so the
dataset alone can never reveal a disagreement: the rule could be subtly wrong
and the output would look fine. This re-runs it over the appendices and
compares it against the written rows whose gender came from Stammdaten, an
independent source, so the rule is tested where it is NOT the deciding voice.

Result on 2026-11-15: 35,217 rows where both decided, 99.920% agreement
(32,365 male and 2,824 female), 28 disagreements. The disagreements split two
ways, and most are not the rule's fault:

  25 rows   the DATASET is wrong. Surname-only Stammdaten matching ignored an
            explicit forename ("Dr. Michael Naumann" linked to Kersten
            Naumann, female) and in one case ignored a total absence of
            Wahlperiode overlap ("Dr. Jochimsen", corpus WP7-8, linked to
            Lukrezia Jochimsen of WP16-17). Here the header rule was right.
   3 rows   the RULE is wrong: a woman in a masculine-inflected office with no
            "Frau" in the name — "des Parl. Staatssekretärs Dr. Barbara
            Hendricks". None of the three was ever applied, because Stammdaten
            had already resolved them and the rule only fills unknowns.

Run it after any change to _header_gender or to the gender pipeline. Takes an
optional project root. Nothing is modified.
"""
import collections
import pathlib
import sys

import pandas as pd

# project root, from this file; an explicit argument lets it run from a
# worktree, where the built dataset lives in the main checkout
B = (pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1
     else pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(B / "src"))
import anlagen as A  # noqa: E402

print(f"using {A.__file__}")

sp = pd.read_parquet(B / "bundestag_dataset_current" / "speeches.parquet",
                     columns=["name", "gender", "gender_source",
                              "contribution_type", "session_id"])
w = sp[(sp["contribution_type"] == "written")
       & (sp["gender_source"] == "stammdaten")]
truth = (w.groupby("name")["gender"]
          .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else None)
          .to_dict())
print(f"{len(w):,} written rows resolved by Stammdaten, "
      f"{len(truth)} distinct authors\n")

agree = collections.Counter()
disagree = []
checked = 0
files = sorted((B / "data" / "preprocessed").rglob("*_cleaned.txt"))
print(f"scanning {len(files)} sittings", flush=True)
for i, f in enumerate(files, 1):
    try:
        t = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    _, subs = A.split_anlagen(t)
    for s in subs:
        a, g = s.get("author"), s.get("gender")
        if not a or g is None:
            continue
        want = truth.get(a)
        if want is None or want == "unknown":
            continue
        checked += 1
        if g == want:
            agree[g] += 1
        else:
            disagree.append((a, g, want, f.stem))
    if i % 1500 == 0:
        print(f"  {i}/{len(files)}  checked={checked:,} "
              f"disagree={len(disagree)}", flush=True)

print(f"\n{checked:,} rows where both the header rule and Stammdaten decided")
print(f"  agree    : {sum(agree.values()):,}  {dict(agree)}")
print(f"  DISAGREE : {len(disagree):,}")
if checked:
    print(f"  agreement: {sum(agree.values()) / checked * 100:.3f}%")
print("\nEach disagreement is one of the two kinds in this file's docstring —\n"
      "check which before assuming the rule is at fault:")
seen = collections.Counter((a, g, t) for a, g, t, _ in disagree)
for (a, g, t), k in seen.most_common(30):
    print(f"    {k:4}  {a!r:34s} header={g:6s} dataset={t}")
