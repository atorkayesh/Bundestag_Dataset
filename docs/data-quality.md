# Data Quality Report

Corpus statistics, validation results, known limitations, and accepted edge cases
for the Bundestag Plenarprotokolle pipeline.

*Current as of: pipeline version post-dehyphenation (b_00 → b_01 → b_02).*

---

## Corpus Coverage

The current development corpus comprises **20 sessions**, one per Wahlperiode,
spanning the full history of the Federal Republic from 1949 to 2022.

| Session ID | Date | City | Speeches |
|---|---|---|---|
| WP01_Session001 | 07 Sep 1949 | Bonn | 39 |
| WP02_Session080 | 05 May 1955 | Bonn | 50 |
| WP03_Session105 | 09 Mar 1960 | Bonn | 281 |
| WP04_Session124 | 29 Apr 1964 | Bonn | 342 |
| WP05_Session050 | 23 Jun 1966 | Bonn | 177 |
| WP06_Session007 | 30 Oct 1969 | Bonn | 352 |
| WP07_Session037 | 25 May 1973 | Bonn | 112 |
| WP08_Session230 | 04 Jul 1980 | Bonn | 50 |
| WP09_Session001 | 04 Nov 1980 | Bonn | 14 |
| WP10_Session252 | 03 Dec 1986 | Bonn | 275 |
| WP11_Session006 | 20 Mar 1987 | Bonn | 65 |
| WP12_Session240 | 22 Jul 1994 | Bonn | 99 |
| WP13_Session005 | 23 Nov 1994 | Bonn | 139 |
| WP14_Session249 | 05 Jul 2002 | Berlin | 161 |
| WP15_Session008 | 07 Nov 2002 | Berlin | 126 |
| WP16_Session231 | 03 Jul 2009 | Berlin | 194 |
| WP17_Session004 | 11 Nov 2009 | Berlin | 203 |
| WP18_Session243 | 29 Jun 2017 | Berlin | 317 |
| WP19_Session002 | 21 Nov 2017 | Berlin | 220 |
| WP20_Session010 | 12 Jan 2022 | Berlin | 239 |
| **Total** | | | **3,455** |

---

## Validation Results

A full quality audit was conducted after all three pipeline stages completed.
The following checks were applied across all 3,455 speeches.

| Check | Result | Notes |
|---|---|---|
| Null or empty speaker names | **0** | All speeches have a name |
| Sequence gaps within sessions | **0** | All sequences are contiguous 1-based integers |
| Multiple consecutive whitespace in `name`, `role`, `party` fields | **0** | All fields are clean |
| Party qualifier misclassified as location | **0** | Qualifier disambiguation logic verified |
| `speaker_raw` vs. normalised `name` mismatches (structural) | **0** | Normalisation consistent |
| Unique role values | **42** | All are valid parliamentary, ministerial, or chair roles |
| Speeches with body text shorter than 20 characters | **91** | Legitimate short chair remarks and procedural notes |
| Consecutive sequences with the same speaker | **358** | Legitimate interrupted speeches and chair moderation |
| Speeches with empty body text | **1** | WP15 Seq 64 — see Known Limitations |
| Speeches with page headers embedded in body | **0** | Fixed; see Design Decisions |

---

## Issue Log and Fixes Applied

### WP18 / WP19 — Page headers surviving cleaning

**Problem:** `pdftotext` produced header lines in WP18 and WP19 with formatting
variants not covered by the original header-removal regex:
- Spaces inserted around the period: `18 . Wahlperiode`
- Leading space before "Deutscher": ` Deutscher Bundestag`
- Column marker prefix before header text: `(A)(C)Deutscher Bundestag`
- Page number and CID token before header: `2068* (cid:9) Deutscher Bundestag`

**Impact before fix:** 114 of 301 WP18 speeches (38%) contained embedded header lines
in their body text. WP19 had 2 surviving headers.

**Fix:** The `RE_HEADER_NEW` regex was updated four times to cover each variant.
The cleaning pipeline was reordered to place CID removal before header removal, so
that compound artefacts (`page_number (cid:9) header`) are resolved correctly.

**Result after fix:** 0 header lines in any speech body across all 20 sessions.

### DKP and NPD — Missing from known party list

**Problem:** Deutsche Kommunistische Partei (DKP, active in Bundestag WP06–WP10) and
Nationaldemokratische Partei Deutschlands (NPD, WP05–WP06) were absent from both
the reference CSV and the hardcoded era-variant list in b_02. Without them, their
parenthetical qualifiers would be misclassified as location rather than party when
the corpus is scaled to full coverage.

**Fix:** Both abbreviations were added to `docs/political-parties-past-75-years.csv`
and to the manual-additions set in `b_02_speaker_identifier.py`.

---

## Known Limitations

### WP01–WP09: Two-column text order leakage

Sessions from WP01 to WP09 are scanned two-column documents. `pdftotext` with `-raw`
reads the PDF content stream in OCR-encoded order, which correctly preserves
left-column-first speaker sequence. However, near column breaks, 1–3 sentences from
the right column may appear inside the preceding left-column speech body. The
alternative (removing `-raw`) was tested and found to produce worse outcomes: it
interleaves lines from both columns throughout the document, breaking the speaker
attribution sequence entirely. The minor leakage under `-raw` has negligible impact
on bag-of-words topic models because word co-occurrence statistics are robust to
small amounts of boundary noise.

### WP01–WP09: OCR replacement characters

The scanned sessions contain `\uFFFD` (Unicode replacement character) throughout,
at positions where OCR failed to recognise a character — commonly umlauts in degraded
print quality. No correction was applied. For counting and topic-model analyses,
affected tokens simply do not contribute to vocabulary; they do not distort
unaffected words.

### `party = null` does not mean independent

When the `party` field is null, it means no party qualifier appeared in the speaker's
attribution line for that speech — not that the speaker belongs to no party. This is
common for government officials (ministers, the Chancellor, state secretaries), whose
attributions list their title and ministry but not their party. It also occurs in
older sessions (WP01–WP09) where party attribution conventions differed. Null should
not be recoded as "fraktionslos" without additional source verification.

### Within-session only: name normalisation scope

Speaker name variants are normalised only within a single session. The same speaker
may have slightly different name spellings across different sessions (for example,
due to inconsistent OCR quality). Cross-session deduplication — linking the same
person across sessions — is left for the analysis stage and requires a separate
name-matching or authority-file linkage step.

### WP15 Seq 64: Empty speech body

Session WP15_Session008, sequence 64, has an empty `text` field. The attribution
line was correctly identified (a chair remark), but no body text was captured between
it and the next attribution. This is an isolated edge case representing a procedural
announcement with no substantive content. It is retained in the corpus as-is rather
than removed, so sequence numbering remains contiguous.

---

## Design Decisions Accepted Without Fix

| Decision | Rationale |
|---|---|
| `-raw` flag for WP01–WP09 | Removing it was tested and breaks speaker sequence order entirely; minor text leakage is the lesser of two evils |
| WP15 Seq 64 empty body | 1 empty speech in 3,455 is negligible; removing it would renumber sequences |
| Levenshtein normalisation for same-role speakers only | Applying to regular members (`role = null`) risks merging distinct MPs with similar names; no safe discriminating signal is available without a reference authority file |
| Leading dash (`-`) in resumed-speech segments preserved | The dash is a typographic convention, not content; it is preserved because removing it would require pattern-matching that could accidentally strip meaningful dashes |
| No cross-session deduplication | Requires a separate name-resolution step (authority file or fuzzy linkage); out of scope for the extraction pipeline |
