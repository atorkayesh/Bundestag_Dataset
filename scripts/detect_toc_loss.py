"""How much real debate does remove_toc discard?

remove_toc keeps everything from the first anchor ("Beginn: HH.MM Uhr", or a
presiding-officer line) onwards and throws the rest away. That is sound only
if nothing before the anchor is real speech. When extraction interleaves the
cover page with the first debate page, it is not: the first speaker's
attribution can sit ahead of the anchor and is silently deleted.

A discarded prefix is counted as a loss when it contains a speaker attribution
followed by real prose — ToC lines are excluded, since they carry dot leaders
and a page reference rather than a sentence.
"""
import pathlib, re, sys
import pandas as pd

B = pathlib.Path(__file__).resolve().parent.parent  # project root, from this file
OUT = B / "outputs" / "diagnostics"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(B / "src"))
import b_01_text_cleaning as b01

PRE = []
for name, fn in b01.PIPELINE:
    if name == "remove_toc":
        break
    PRE.append((name, fn))

# Line-based, deliberately: a regex with nested quantifiers over a 50k prefix
# backtracks catastrophically on a long colon-less line and hangs the scan.
RE_ATTR_LINE = re.compile(
    r"^(?P<who>[^:]{6,90}?(?:,[^:]{3,80}|\([^)]{1,40}\)))\s*:\s*(?P<rest>.*)$")
RE_TOC_LINE = re.compile(r"\.\s*\.\s*\.|\d{4,5}\s*[A-D]")


def lost_attributions(prefix: str):
    """Attributions in the discarded prefix that are followed by real prose.

    The colon may end the line or be followed by the speech on the same line -
    "Dr. Carsten Linnemann, Bundesminister fuer Gesundheit: Frau Praesidentin!"
    - so both forms are accepted and the tail is assembled from whatever
    follows the colon plus the next few lines.
    """
    lines = prefix.splitlines()
    out = []
    for i, line in enumerate(lines):
        line = line.strip()
        if ":" not in line:
            continue
        head = line.split(":", 1)[0]
        if len(head) > 110 or len(head) < 6:
            continue            # keeps the per-line regex bounded
        m = RE_ATTR_LINE.match(line)
        if not m:
            continue
        tail = " ".join((m.group("rest") + " " +
                         " ".join(lines[i + 1:i + 8])).split())
        if len(tail) < 200 or RE_TOC_LINE.search(tail[:200]):
            continue            # a contents line, not a speech
        out.append(" ".join(m.group("who").split())[:60])
    return out


wps = sys.argv[1:] or None
files = sorted(p for p in (B / "data" / "preprocessed").rglob("*.txt")
               if not p.stem.endswith("_cleaned"))
if wps:
    files = [f for f in files if any(f"WP{w.zfill(2)}" in str(f) for w in wps)]

rows = []
for i, f in enumerate(files, 1):
    try:
        t = f.read_text(encoding="utf-8", errors="replace")
        for _n, fn in PRE:
            t = fn(t)
    except Exception as exc:        # one bad file must not kill the scan
        print(f"  SKIP {f.stem}: {type(exc).__name__}", flush=True)
        continue
    anchor = b01.RE_ANCHOR_STRONG.search(t) or b01.RE_ANCHOR_SPEAKER.search(t)
    if anchor is None:
        continue
    prefix = t[:anchor.start()]
    lost = lost_attributions(prefix)
    if lost:
        rows.append((f.stem, len(prefix), len(lost), lost[:3]))
    if i % 1000 == 0:
        print(f"  {i}/{len(files)}", flush=True)

rows.sort(key=lambda r: -r[2])
print(f"\n{len(rows)} of {len(files)} sessions discard real speech with the ToC")
print(f"{sum(r[2] for r in rows)} attributions lost in total\n")
for sid, plen, n, ex in rows[:25]:
    print(f"  {sid:20s} prefix {plen:>8,} chars, {n:3d} lost: {ex}")
if len(rows) > 25:
    print(f"  ... and {len(rows)-25} more")
pd.DataFrame(rows, columns=["session", "prefix_chars", "lost_attributions",
                            "examples"]).to_csv(
    OUT / "toc_loss.csv", index=False, encoding="utf-8-sig")
