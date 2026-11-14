"""Capture a comparable snapshot of the dataset. Usage: snapshot.py <label>"""
import json, pathlib, sys
import pandas as pd

B = pathlib.Path(__file__).resolve().parent.parent  # project root, from this file
OUT = B / "outputs" / "diagnostics"
OUT.mkdir(parents=True, exist_ok=True)
label = sys.argv[1]

df = pd.read_parquet(B / "bundestag_dataset_current" / "speeches.parquet",
                     columns=["session_id", "wp", "name", "party", "role", "gender", "text"])
snap = {
    "label": label,
    "speeches": int(len(df)),
    "sessions": int(df["session_id"].nunique()),
    "speakers": int(df["name"].nunique()),
    "gender": {k: int(v) for k, v in df["gender"].value_counts().items()},
    "by_wp": {str(k): int(v) for k, v in df["wp"].value_counts().sort_index().items()},
    "party": {str(k): int(v) for k, v in df["party"].value_counts().head(12).items()},
    "n_roles": int(df["role"].nunique()),
    "n_parties": int(df["party"].nunique()),
    "total_chars": int(df["text"].str.len().sum()),
    "watch": {n: int((df["name"] == n).sum()) for n in [
        "Dr. Wolfgang Strengmann-Kuhn", "Irmingard Schewe-Gerigk",
        "Silke Stokar von Neuforn", "Franziska Eichstädt-Bohlig",
        "Dr. Kirsten Kappert-Gonther", "Dr. Jan-Niclas Gesenhues",
        "Swantje Henrike Michaelsen", "Dr. Konstantin von Notz",
        "Hans-Christian Ströbele", "Friedrich Merz", "Julia Klöckner"]},
}
(OUT / f"snap_{label}.json").write_text(json.dumps(snap, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
print(json.dumps(snap, indent=2, ensure_ascii=False)[:900])
print(f"\nsaved snap_{label}.json")
