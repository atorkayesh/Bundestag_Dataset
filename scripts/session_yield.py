"""Why do some sessions hold far fewer speeches than their term's median?

A short sitting and a damaged one look the same in a speech count. They differ
in how much text went in: compare the raw extracted file against what reached
the dataset. A genuinely short sitting has a small source file and a normal
share of it surviving; a damaged one has a large source file and little of it.
"""
import pathlib, collections
import pandas as pd

B = pathlib.Path(__file__).resolve().parent.parent  # project root, from this file
OUT = B / "outputs" / "diagnostics"
OUT.mkdir(parents=True, exist_ok=True)
sp = pd.read_parquet(B / "bundestag_dataset_current" / "speeches.parquet",
                     columns=["session_id", "wp", "text"])
per = sp.groupby(["wp", "session_id"]).agg(
    speeches=("text", "size"), ds_chars=("text", lambda s: s.str.len().sum())
).reset_index()
med = per.groupby("wp")["speeches"].median().rename("wp_median")
per = per.join(med, on="wp")
per["ratio"] = per["speeches"] / per["wp_median"]

raw_chars, cl_chars = {}, {}
for f in (B / "data" / "preprocessed").rglob("*.txt"):
    stem = f.stem
    try:
        n = f.stat().st_size
    except OSError:
        continue
    if stem.endswith("_cleaned"):
        cl_chars[stem[:-8]] = n
    else:
        raw_chars[stem] = n
per["raw"] = per["session_id"].map(raw_chars).fillna(0).astype(int)
per["cleaned"] = per["session_id"].map(cl_chars).fillna(0).astype(int)
per["kept_pct"] = (per["ds_chars"] / per["raw"].replace(0, pd.NA) * 100).round(1)

thin = per[per["ratio"] < 0.25].copy()
print(f"{len(thin)} sessions under 25% of their term median "
      f"(of {len(per):,} sessions)\n")

# A damaged session has a big source file; a short sitting has a small one.
wp_raw_med = per.groupby("wp")["raw"].median().rename("wp_raw_median")
thin = thin.join(wp_raw_med, on="wp")
thin["raw_ratio"] = thin["raw"] / thin["wp_raw_median"]

suspicious = thin[thin["raw_ratio"] > 0.5]
normal = thin[thin["raw_ratio"] <= 0.5]
print(f"  source file is a normal size but few speeches  -> {len(suspicious)} SUSPICIOUS")
print(f"  source file is small too (a genuinely short sitting) -> {len(normal)}")

print("\n=== suspicious: big file, few speeches (worst 25 by kept%) ===")
cols = ["session_id", "speeches", "wp_median", "raw", "ds_chars", "kept_pct", "raw_ratio"]
print(suspicious.nsmallest(25, "kept_pct")[cols].to_string(index=False))

print("\n=== the short-sitting group, for contrast (10 smallest files) ===")
print(normal.nsmallest(10, "raw")[cols].to_string(index=False))

print("\n=== corpus baseline: median kept_pct by WP ===")
print(per.groupby("wp")["kept_pct"].median().round(1).to_string())

per.to_csv(OUT / "session_yield.csv", index=False,
           encoding="utf-8-sig")
suspicious.to_csv(OUT / "thin_suspicious.csv", index=False,
                  encoding="utf-8-sig")
