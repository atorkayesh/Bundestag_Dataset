"""Entry point for the full Bundestag pipeline: b_00 → b_01 → b_02.

Use --wp to process specific Wahlperioden; --skip-extract/--skip-clean/--skip-speakers
to run individual stages.
"""

import argparse
import sys
from pathlib import Path

# Ensure src/ is importable when this script is invoked from the project root
# e.g.  python src/run_pipeline.py
sys.path.insert(0, str(Path(__file__).parent))

import b_00_text_extractor    as b00
import b_01_text_cleaning     as b01
import b_02_speaker_identifier as b02


def _section(title: str) -> None:
    bar = "=" * 60
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full Bundestag text pipeline (b_00 -> b_01 -> b_02).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Path overrides (all optional — scripts fall back to their own defaults)
    parser.add_argument(
        "--input-dir",
        default=None,
        metavar="PATH",
        help="PDF source folder (b_00 input). "
             f"Default: {b00._DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="Preprocessed output root (b_00 output / b_01 + b_02 input+output). "
             f"Default: {b00._DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--parties-csv",
        default=None,
        metavar="PATH",
        help="Path to political-parties-past-75-years.csv (b_02). "
             "Default: docs/ relative to script file.",
    )

    # Scope filter
    parser.add_argument(
        "--wp",
        default=None,
        metavar="NN[,NN,...]",
        help="Comma-separated Wahlperiode numbers to process, e.g. '01' or '01,02'. "
             "All others are skipped.  If omitted all WPs are processed.",
    )

    # Processing flags
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reprocessing of already-completed files in every stage.",
    )

    # Stage-skip flags (useful for re-running one stage after a bug-fix)
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="Skip Stage 1 (b_00 PDF extraction).",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Skip Stage 2 (b_01 text cleaning).",
    )
    parser.add_argument(
        "--skip-speakers",
        action="store_true",
        help="Skip Stage 3 (b_02 speaker identification).",
    )

    args = parser.parse_args()

    # Parse WP filter
    wp_filter = None
    if args.wp:
        wp_filter = {w.strip().zfill(2) for w in args.wp.split(",")}

    out_dir = Path(args.output_dir) if args.output_dir else None

    # ------------------------------------------------------------------ #
    # Stage 1 — PDF extraction                                            #
    # ------------------------------------------------------------------ #
    _section("Stage 1 / 3 — PDF Extraction (b_00)")
    if args.skip_extract:
        print("[SKIPPED via --skip-extract]")
    else:
        b00.run(
            force=args.force,
            wp_filter=wp_filter,
            in_root=args.input_dir,
            out_root=out_dir,
        )

    # ------------------------------------------------------------------ #
    # Stage 2 — Text cleaning                                             #
    # ------------------------------------------------------------------ #
    _section("Stage 2 / 3 — Text Cleaning (b_01)")
    if args.skip_clean:
        print("[SKIPPED via --skip-clean]")
    else:
        b01.run(
            force=args.force,
            preprocessed_dir=out_dir,
            wp_filter=wp_filter,
        )

    # ------------------------------------------------------------------ #
    # Stage 3 — Speaker identification                                    #
    # ------------------------------------------------------------------ #
    _section("Stage 3 / 3 — Speaker Identification (b_02)")
    if args.skip_speakers:
        print("[SKIPPED via --skip-speakers]")
    else:
        b02.run(
            force=args.force,
            preprocessed_dir=out_dir,
            parties_csv=args.parties_csv,
            wp_filter=wp_filter,
        )

    print("\n" + "=" * 60)
    print("  Pipeline complete.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
