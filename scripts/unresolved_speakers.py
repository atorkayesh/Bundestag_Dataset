"""List the rows whose speaker could not be resolved against MdB-Stammdaten.

`gender = unknown` is the symptom, not the defect: it means the name matched
nothing in the Stammdaten, so e_01 had nothing to resolve a gender from. The
names fall into four groups wanting four different remedies — misspelled real
members (name_corrections.csv), genuine non-members such as ministers and
state secretaries (manual_gender_overrides.csv), truncations the b_01
split-word rules cannot reach, and prose absorbed into the name by b_02.
See the "143 rows carry an unresolved speaker" section of CLAUDE.md.

Nothing is modified.
"""
import pathlib
import sys
import pandas as pd

# project root, from this file; an explicit argument lets the script run from a
# worktree, where the built dataset lives in the main checkout rather than here
B = (pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1
     else pathlib.Path(__file__).resolve().parent.parent)
OUT = B / "outputs" / "diagnostics"
OUT.mkdir(parents=True, exist_ok=True)

sp = pd.read_parquet(
    B / "bundestag_dataset_current" / "speeches.parquet",
    columns=["speech_id", "session_id", "wp", "name", "name_cleaned", "role",
             "party", "gender", "stammdaten_id", "contribution_type"])

u = sp[sp["gender"] == "unknown"].copy()
print(f"{len(sp):,} rows in the dataset")
print(f"{len(u):,} with an unresolved speaker "
      f"({len(u) / len(sp) * 100:.3f}%), {u['name'].nunique()} distinct names")
if u.empty:
    raise SystemExit(0)

print("\nby contribution_type:")
for k, v in u["contribution_type"].value_counts().items():
    print(f"  {k:10s} {v:6,}")

print("\nby Wahlperiode:")
for k, v in u["wp"].value_counts().sort_index().items():
    print(f"  WP{int(k):02d}  {v:5,}")

g = (u.groupby("name")
       .agg(rows=("name", "size"),
            wps=("wp", lambda s: ",".join(str(int(x)) for x in sorted(set(s)))),
            kinds=("contribution_type", lambda s: ",".join(sorted(set(s)))),
            example=("session_id", "first"))
       .sort_values("rows", ascending=False))

print(f"\n{len(g)} distinct unresolved names:")
for n, r in g.iterrows():
    print(f"  {r['rows']:4}  {str(n):36s} WP{r['wps']:<14s} "
          f"{r['kinds']:<14s} {r['example']}")

dest = OUT / "unresolved_speakers.csv"
g.to_csv(dest, encoding="utf-8")
print(f"\nwrote {dest}")
