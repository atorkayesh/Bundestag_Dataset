"""How much Anlagen back matter is absorbed into a speech?

The Anlagen are the appendix of written questions and answers printed after
the sitting closes. They are not spoken debate, and nothing removes them, so
they are swallowed by whichever speech was open when they begin - usually the
chair's closing words. In WP08 session 033 that gave the presiding officer a
131,029-character "speech" of ministerial written answers.
"""
import pathlib, re
import pandas as pd

B = pathlib.Path(__file__).resolve().parent.parent  # project root, from this file
OUT = B / "outputs" / "diagnostics"
OUT.mkdir(parents=True, exist_ok=True)
sp = pd.read_parquet(B / "bundestag_dataset_current" / "speeches.parquet",
                     columns=["session_id", "wp", "sequence", "name", "text"])

# The appendix opens with "Anlage 1" or a heading naming the Anlagen, normally
# right after the chair closes the sitting.
RE_ANLAGE = re.compile(
    r"(?:^|\s)(?:Anlagen?\s+1\b|Anlagen\s+zum\s+Stenographischen\s+Bericht)")

rows = []
for (sid, wp), g in sp.groupby(["session_id", "wp"], sort=False):
    g = g.sort_values("sequence")
    total = int(g["text"].str.len().sum())
    for _, r in g.iterrows():
        t = str(r["text"])
        m = RE_ANLAGE.search(t)
        if m and len(t) - m.start() > 2000:
            rows.append({"session_id": sid, "wp": int(wp), "name": r["name"],
                         "sequence": int(r["sequence"]), "speech_chars": len(t),
                         "anlagen_chars": len(t) - m.start(),
                         "session_chars": total,
                         "is_last": bool(r["sequence"] == g["sequence"].max())})
            break

d = pd.DataFrame(rows)
print(f"{len(d):,} sessions carry an absorbed Anlagen block "
      f"(of {sp['session_id'].nunique():,})")
print(f"total absorbed text: {d['anlagen_chars'].sum():,} chars "
      f"({d['anlagen_chars'].sum() / sp['text'].str.len().sum() * 100:.1f}% of the corpus)")
print(f"absorbed into the LAST speech of the sitting: {int(d['is_last'].sum()):,}")
print("\nby Wahlperiode:")
t = d.groupby("wp").agg(sessions=("session_id", "size"),
                        chars=("anlagen_chars", "sum"))
print(t.to_string())
print("\nwho ends up credited with it (top 12):")
print(d["name"].value_counts().head(12).to_string())
print("\nlargest 12:")
print(d.nlargest(12, "anlagen_chars")[
    ["session_id", "name", "speech_chars", "anlagen_chars", "session_chars"]
].to_string(index=False))
d.to_csv(OUT / "anlagen.csv", index=False,
         encoding="utf-8-sig")
