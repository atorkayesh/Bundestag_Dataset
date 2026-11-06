"""Monthly update: fetch new protocols, reprocess what changed, rebuild, report.

Chains a_01 (fetch) -> b_00 -> b_01 -> b_02 -> e_01 -> f_01 -> c_01 -> h_01 and
writes a change report for review before the rebuild is handed to anyone.

Sessions the server has revised are invalidated before the text stages run:
their derived files are deleted and their log lines dropped, so the normal
resume-safe logic reprocesses exactly those and nothing else.

The default dataset directory is bundestag_dataset_current. Writing into
bundestag_dataset_v1 is refused without --allow-v1, because that directory is
the published artefact behind the Zenodo deposit and the manuscript.
"""

# defaults — all relative to the project root, override via CLI flags
_PROJECT_ROOT = None          # resolved at runtime from this file's location
_FROZEN_DIRS = {"bundestag_dataset_v1"}

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import a_01_fetch_protocols as a01

STATE_NAME = "_update_state.json"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _section(title: str) -> None:
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


# --------------------------------------------------------------------- #
# Invalidation of revised sessions                                       #
# --------------------------------------------------------------------- #

def invalidate(session_ids, preprocessed: Path) -> int:
    """Delete derived files and log lines for the given WWSSS sessions."""
    if not session_ids:
        return 0

    stems = []
    for sid in session_ids:
        wp, sess = sid[:2], sid[2:5]
        stems.append(f"WP{wp}_Session{sess}")

    removed = 0
    for stem in stems:
        wp_dir = preprocessed / f"WP{stem[2:4]}"
        if not wp_dir.exists():
            continue
        for path in wp_dir.glob(f"*/{stem}*"):
            path.unlink()
            removed += 1

    # Drop the matching lines from each stage log, or the stages will skip
    # these sessions as already done.
    for log_name in ("processed.log", "cleaning.log", "speakers.log"):
        log = preprocessed / log_name
        if not log.exists():
            continue
        lines = log.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if not any(s in ln for s in stems)]
        if len(kept) != len(lines):
            log.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    print(f"Invalidated {len(stems)} session(s); removed {removed} derived file(s).")
    return removed


# --------------------------------------------------------------------- #
# Corpus snapshot, for month-on-month comparison                         #
# --------------------------------------------------------------------- #

def snapshot(dataset_dir: Path) -> dict:
    """Summarise the built dataset for comparison against the previous run."""
    import pandas as pd

    speeches = dataset_dir / "speeches.parquet"
    if not speeches.exists():
        return {}

    cols = ["session_id", "wp", "name", "party", "role", "gender", "gender_source"]
    df = pd.read_parquet(speeches, columns=cols)

    unresolved = sorted(
        df.loc[df["gender_source"].isin(["inferred", "unknown"]), "name"]
          .dropna().unique().tolist()
    )
    return {
        "taken_at": _now(),
        "n_speeches": int(len(df)),
        "n_sessions": int(df["session_id"].nunique()),
        "parties": sorted(df["party"].dropna().unique().tolist()),
        "roles": sorted(df["role"].dropna().unique().tolist()),
        "unresolved_gender_names": unresolved,
    }


def load_state(outputs: Path) -> dict:
    path = outputs / STATE_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(outputs: Path, snap: dict) -> None:
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / STATE_NAME).write_text(
        json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------- #
# Report                                                                 #
# --------------------------------------------------------------------- #

def write_report(outputs: Path, fetch_rep, prev: dict, curr: dict) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Bundestag corpus update — {stamp}", ""]

    lines += ["## Fetch", ""]
    lines.append(f"- New sessions: **{len(fetch_rep.new_sessions)}**"
                 + (f" ({', '.join(fetch_rep.new_sessions)})" if fetch_rep.new_sessions else ""))
    lines.append(f"- Revised sessions: **{len(fetch_rep.revised_sessions)}**"
                 + (f" ({', '.join(fetch_rep.revised_sessions)})" if fetch_rep.revised_sessions else ""))
    if fetch_rep.backfilled_pdfs:
        lines.append(f"- Backfilled PDFs (already in corpus): {len(fetch_rep.backfilled_pdfs)}")
    lines.append(f"- Checked for revision: {fetch_rep.checked_for_revision}")
    for n in fetch_rep.notes:
        lines.append(f"- NOTE: {n}")
    for e in fetch_rep.errors:
        lines.append(f"- ERROR: {e}")
    lines.append("")

    if not curr:
        lines += ["## Corpus", "", "_No dataset built; nothing to compare._", ""]
    else:
        lines += ["## Corpus", ""]
        if prev:
            d_sp = curr["n_speeches"] - prev.get("n_speeches", 0)
            d_se = curr["n_sessions"] - prev.get("n_sessions", 0)
            lines.append(f"- Speeches: {curr['n_speeches']:,} ({d_sp:+,})")
            lines.append(f"- Sessions: {curr['n_sessions']:,} ({d_se:+,})")
        else:
            lines.append(f"- Speeches: {curr['n_speeches']:,} (baseline)")
            lines.append(f"- Sessions: {curr['n_sessions']:,} (baseline)")
        lines.append("")

        def _new(key, label):
            before = set(prev.get(key, []))
            after = set(curr.get(key, []))
            added = sorted(after - before)
            lines.append(f"### {label}: {len(added)} new")
            lines.append("")
            if not prev:
                lines.append("_First run; no comparison baseline._")
            elif added:
                lines.append("Review these and extend the reference tables if any are wrong:")
                lines.append("")
                for v in added[:50]:
                    lines.append(f"- `{v}`")
                if len(added) > 50:
                    lines.append(f"- … and {len(added) - 50} more")
            else:
                lines.append("None.")
            lines.append("")

        _new("parties", "Party strings")
        _new("roles", "Role strings")
        _new("unresolved_gender_names", "Names with inferred or unknown gender")

    lines += ["## Action", "",
              "- [ ] Reviewed new party and role strings",
              "- [ ] Reviewed names with unresolved gender",
              "- [ ] Checked the quality audit CSV",
              "- [ ] Release approved for institute use",
              ""]

    outputs.mkdir(parents=True, exist_ok=True)
    path = outputs / f"update_report_{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------- #
# Main                                                                   #
# --------------------------------------------------------------------- #

def main() -> None:
    root = project_root()
    p = argparse.ArgumentParser(
        description="Monthly Bundestag corpus update.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--raw-dir", default=str(root / "data" / "raw"))
    p.add_argument("--preprocessed-dir", default=str(root / "data" / "preprocessed"))
    p.add_argument("--dataset-dir", default=str(root / "bundestag_dataset_current"))
    p.add_argument("--outputs-dir", default=str(root / "outputs"))
    p.add_argument("--docs-dir", default=str(root / "docs"))
    p.add_argument("--wp", type=int, default=None,
                   help="Wahlperiode to fetch. Default: highest found locally.")
    p.add_argument("--revision-window", type=int, default=a01.REVISION_WINDOW)
    p.add_argument("--skip-fetch", action="store_true")
    p.add_argument("--skip-rebuild", action="store_true",
                   help="Fetch and run the text stages only; no dataset rebuild.")
    p.add_argument("--allow-v1", action="store_true",
                   help="Permit writing into the frozen published dataset directory.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch check only; download and process nothing.")
    args = p.parse_args()

    raw = Path(args.raw_dir)
    pre = Path(args.preprocessed_dir)
    dataset = Path(args.dataset_dir)
    outputs = Path(args.outputs_dir)
    docs = Path(args.docs_dir)

    if dataset.name in _FROZEN_DIRS and not args.allow_v1:
        sys.exit(
            f"REFUSED: {dataset.name} is the published dataset behind the Zenodo "
            f"deposit and the manuscript. Write to bundestag_dataset_current "
            f"instead, or pass --allow-v1 if you really mean it."
        )

    # ---- 1. fetch ---------------------------------------------------- #
    _section("Stage 1 — Fetch (a_01)")
    if args.skip_fetch:
        print("[skipped]")
        fetch_rep = a01.FetchReport(started_at=_now(), finished_at=_now())
    else:
        fetch_rep = a01.run(raw_dir=str(raw), wp=args.wp,
                            preprocessed_dir=str(pre),
                            revision_window=args.revision_window,
                            dry_run=args.dry_run)

    if args.dry_run:
        print("\n[dry run] stopping before processing.")
        print(json.dumps(asdict(fetch_rep), indent=2, ensure_ascii=False))
        return

    if not fetch_rep.changed and not args.skip_fetch:
        print("\nNothing new or revised. Corpus is already current.")
        return

    # ---- 2. invalidate revised sessions ------------------------------- #
    if fetch_rep.revised_sessions:
        _section("Stage 2 — Invalidate revised sessions")
        invalidate(fetch_rep.revised_sessions, pre)

    # ---- 3. text stages ----------------------------------------------- #
    import b_00_text_extractor as b00
    import b_01_text_cleaning as b01
    import b_02_speaker_identifier as b02

    wp_filter = {f"{fetch_rep.wp:02d}"} if fetch_rep.wp else None

    _section("Stage 3 — Extraction (b_00)")
    b00.run(in_root=str(raw), out_root=str(pre), wp_filter=wp_filter)

    _section("Stage 4 — Cleaning (b_01)")
    b01.run(preprocessed_dir=str(pre), wp_filter=wp_filter)

    _section("Stage 5 — Speaker identification (b_02)")
    b02.run(preprocessed_dir=str(pre),
            parties_csv=str(docs / "political-parties-past-75-years.csv"),
            wp_filter=wp_filter)

    if args.skip_rebuild:
        print("\n[--skip-rebuild] stopping before the dataset rebuild.")
        return

    # ---- 4. gender, build, validate, audit ----------------------------- #
    import e_01_gender as e01
    import c_01_validate_corpus as c01

    _section("Stage 6 — Gender annotation (e_01)")
    e01.run(preprocessed_dir=str(pre),
            stammdaten_path=str(docs / "MdB-Stammdaten.xml"),
            output_dir=str(outputs))

    _section("Stage 7 — Dataset build (f_01)")
    build = [
        sys.executable, str(Path(__file__).parent / "f_01_build_dataset.py"),
        "--preprocessed-dir", str(pre),
        "--gender-csv", str(outputs / "gender_lookup.csv"),
        "--parties-csv", str(docs / "political-parties-past-75-years.csv"),
        "--excluded-speakers-csv", str(docs / "excluded_false_speakers.csv"),
        "--manual-gender-csv", str(docs / "manual_gender_overrides.csv"),
        "--stammdaten", str(docs / "MdB-Stammdaten.xml"),
        "--output-dir", str(dataset),
    ]
    if subprocess.call(build) != 0:
        sys.exit("f_01 failed; stopping before validation.")

    _section("Stage 8 — Validation (c_01)")
    c01.run(preprocessed_dir=str(pre))

    _section("Stage 9 — Quality audit (h_01)")
    audit = [
        sys.executable, str(Path(__file__).parent / "h_01_quality_audit.py"),
        "--dataset-dir", str(dataset),
        "--output-dir", str(outputs),
    ]
    subprocess.call(audit)

    # ---- 5. report ------------------------------------------------------ #
    _section("Stage 10 — Change report")
    prev = load_state(outputs)
    curr = snapshot(dataset)
    path = write_report(outputs, fetch_rep, prev, curr)
    if curr:
        save_state(outputs, curr)
    print(f"Report: {path}")
    print("\nReview it before telling anyone the release is good.")


if __name__ == "__main__":
    main()
