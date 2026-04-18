# Pipeline Reference

Technical reference for the three-stage text processing pipeline that converts raw
Bundestag Plenarprotokoll PDFs into structured, speaker-attributed speech data.

---

## Overview

```
data/raw/*.pdf
    │
    ▼
b_00_text_extractor.py  →  preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}.txt
                           preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}.json
                           preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}_metadata.json
                           preprocessed/metadata/metadata_catalogue.csv
    │
    ▼
b_01_text_cleaning.py   →  preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}_cleaned.txt
    │
    ▼
b_02_speaker_identifier.py  →  preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}_speakers.json
```

All three stages are **resume-safe**: each script checks whether its output already
exists and skips files that have been processed. Use `--force` to reprocess.

---

## Stage 1 — Text Extraction (`b_00_text_extractor.py`)

**Purpose:** Extract raw text from Bundestag PDF files with metadata, faithful to the
original document structure.

### Input
- PDF files in `data/raw/`, named `WW{wp}SSS.pdf` (e.g., `WW01001.pdf` for WP01 Session 001)

### Output (per session)
| File | Content |
|---|---|
| `WP{nn}_Session{nnn}.txt` | Plain text with human-readable `--- Page Break ---` markers |
| `WP{nn}_Session{nnn}.json` | Full record including extracted text and all metadata fields |
| `WP{nn}_Session{nnn}_metadata.json` | Metadata only (no text field); used to rebuild the catalogue |

### Catalogue
`preprocessed/metadata/metadata_catalogue.csv` — one row per session, rebuilt from all
existing `_metadata.json` files on every run regardless of whether new PDFs were processed.

**Catalogue columns:**

| Column | Description |
|---|---|
| `session_id` | Unique identifier, e.g. `"WP18_Session243"` |
| `wp_filename` / `session_filename` | WP and session number parsed from filename |
| `wp_text` / `session_text` | WP and session number parsed from document text |
| `wp` / `session` | Final resolved values (text takes precedence over filename) |
| `date` | Session date, DD-MM-YYYY |
| `date_iso` | Session date, YYYY-MM-DD |
| `city` | `"Bonn"` (WP01–WP13) or `"Berlin"` (WP14–WP20) |
| `year` | Session year, e.g. `"1949"` |
| `source_pdf` | Relative path from PDF input folder |
| `md5` | MD5 hex digest of source PDF |
| `engine` | `"pdftotext"` or `"pdfminer"` |
| `meta_warnings_count` | Number of cross-validation warnings |
| `meta_warnings_detail` | JSON string with warning details |

### CLI
```
python src/b_00_text_extractor.py [--force] [--wp NN[,NN,...]]

--force          Reprocess PDFs even if output files already exist
--wp 01,02,03    Process only the specified Wahlperioden (zero-padded or plain)
```

### Extraction engine selection
- **pdftotext** (primary): Called with `-raw` flag for WP01–WP09 (scanned two-column
  documents) to read the PDF content stream in order, which preserves the
  left-column-first speaker sequence as laid down by OCR.
  Called without `-raw` for WP10–WP20 (born-digital single-column documents).
- **pdfminer** (fallback): Used if pdftotext is unavailable or returns empty output.

---

## Stage 2 — Text Cleaning (`b_01_text_cleaning.py`)

**Purpose:** Remove PDF/OCR formatting artefacts from extracted text while preserving
all speech content.

### Input
`preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}.txt`

### Output
`preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}_cleaned.txt`

### CLI
```
python src/b_01_text_cleaning.py [--force]

--force    Overwrite existing _cleaned.txt files
```

### Cleaning pipeline (11 steps, in order)

| Step | Name | What it removes |
|---|---|---|
| 1 | `remove_page_breaks` | `--- Page Break ---` separator lines inserted by b_00 |
| 2 | `remove_cid_artefacts` | `(cid:N)` tokens from failed glyph-to-Unicode mapping |
| 3 | `remove_headers` | Repeated page headers in old format (`N. Sitzung. Bonn/Berlin, Datum`) and new format (`N. Wahlperiode – N. Sitzung. Berlin, Datum`), including OCR variants with leading spaces, spaces around the period, column letter prefixes, and embedded CID tokens |
| 4 | `remove_toc` | Table-of-contents entries: dot-leader lines, spaced-letter headings (`I n h a l t :`), and page-reference entries — detected using a strong anchor (session-opening phrase) or fallback anchor (first presidential speaker line) |
| 5 | `remove_page_numbers` | Standalone page-number lines (digits only, optionally with Roman prefix, trailing `*`, or column letter A–D) |
| 6 | `remove_footnotes` | Lines starting with `*`, `†`, numbered `(1)` or lettered `(a))` markers; colophon lines (`Gesamtherstellung:`, `Druck:`, `ISSN`, etc.) |
| 7 | `remove_margin_markers` | Isolated `(A)` `(B)` `(C)` `(D)` column markers on their own line or inline |
| 8 | `remove_bare_separators` | Lines consisting solely of one or two hyphens/dashes/em-dashes (OCR vertical rule artefacts) |
| 9 | `remove_backm_references` | EU/Ratsdokument reference lines in back-matter zones (`Drucksache`, `Ratsdokument` patterns) |
| 10 | `normalise_whitespace` | Collapses three or more consecutive newlines to two; strips trailing spaces from lines |
| 11 | `rejoin_hyphens` | Rejoins words broken by end-of-line hyphenation: lowercase continuation → remove hyphen and join; uppercase continuation → keep hyphen and drop newlines |

**Step ordering is significant:** CID removal (step 2) precedes header removal (step 3)
because some headers contain embedded `(cid:9)` tokens that would prevent the header
regex from matching. Whitespace normalisation (step 10) precedes hyphen-joining (step 11)
so that blank lines containing only spaces are collapsed first, exposing all line-break
hyphen patterns to the joining step.

---

## Stage 3 — Speaker Identification (`b_02_speaker_identifier.py`)

**Purpose:** Identify speaker attribution lines in cleaned text, split sessions into
individual speech segments, and classify each speaker's name, role, party, and location.

### Input
`preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}_cleaned.txt`

### Output
`preprocessed/WP{nn}/{year}/WP{nn}_Session{nnn}_speakers.json`

### CLI
```
python src/b_02_speaker_identifier.py [--force]

--force    Overwrite existing _speakers.json files
```

### Output schema

**Top-level object:**
```json
{
  "session_id":    "WP18_Session243",
  "speech_count":  317,
  "speeches":      [ ... ]
}
```

**Each speech object:**

| Field | Type | Description |
|---|---|---|
| `sequence` | `int` | 1-based index, contiguous within session, never gaps |
| `speaker_raw` | `string` | Verbatim attribution line as it appears in the transcript |
| `name` | `string` | Normalised full name (whitespace collapsed, OCR variants merged) |
| `role` | `string\|null` | Chair or official role (e.g. `"Präsident"`, `"Bundeskanzler"`, `"Bundesminister des Auswärtigen"`). `null` for regular Bundestag members |
| `party` | `string\|null` | Party abbreviation as written in the transcript (e.g. `"SPD"`, `"CDU/CSU"`, `"fraktionslos"`). `null` when no party qualifier appeared in the attribution — **not** equivalent to independent |
| `location` | `string\|null` | City, constituency, or Bundesland from the attribution. `null` when not present |
| `text` | `string` | Speech body text. May contain `\n\n` line separators from the source PDF |
| `char_offset` | `int` | Character position of the attribution line in the cleaned source file |

### Speaker attribution patterns

Four patterns are recognised, confirmed across WP01–WP20:

**Pattern A — Chair roles**
```
[Frau|Herr] Alterspräsident(in)|Präsident(in)|Vizepräsident(in)  Full Name :
```
Examples: `Präsidentin Bärbel Bas:` · `Frau Vizepräsidentin Roth:`

**Pattern B — Regular member with party**
```
[Dr.] [Prof. Dr.] Full Name  (Location)?  (Party) :
```
Examples: `Dr. Adenauer (CDU):` · `Achim Post (Minden) (SPD):`

**Pattern C — Government and state officials**
```
Full Name , Bundeskanzler(in)|Bundesminister(in)|Parl. Staatssekretär(in)|
            Staatsminister(in)|Ministerpräsident(in)  [description] :
```
Examples: `Olaf Scholz, Bundeskanzler:` · `Dr. Klaus Kinkel, Bundesminister des Auswärtigen:`

State-level role prefixes may also appear before the name (without a comma):
```
Ministerpräsident(in)|Regierender Bürgermeister(in)  Full Name  (Bundesland) :
```
Example: `Ministerpräsident Oskar Lafontaine (Saarland):`

**Pattern D — Parliamentary procedural roles**
```
Full Name  (Qualifier)+ ,  Berichterstatter(in)|Anfragender|Anfragerin :
```

### Qualifier classification rules

Parenthetical qualifiers after a name are classified in the following priority order:

1. **Drucksache / Frage references** — ignored entirely
2. **German Bundesland name** — always classified as `location`
3. **Known party abbreviation** (from `docs/political-parties-past-75-years.csv` plus
   era-specific variants) — classified as `party`
4. **First remaining ambiguous qualifier** — classified as `location`
5. **Second remaining ambiguous qualifier** — classified as `party`

Era-specific party name variants handled:

| Abbreviation used in transcript | Era | Canonical |
|---|---|---|
| `F.D.P.` | WP10–WP12 | FDP |
| `GRÜNE` | early | BÜNDNIS 90/DIE GRÜNEN |
| `DIE GRÜNEN` | mid | BÜNDNIS 90/DIE GRÜNEN |
| `PDS/Linke Liste` | WP12 | Die Linke |
| `DKP` | WP06–WP10 | Deutsche Kommunistische Partei |
| `NPD` | WP05–WP06 | Nationaldemokratische Partei Deutschlands |
| `fraktionslos` | all eras | non-attached member |

### Colon rule

The terminal colon on a **standalone line** is the definitive marker that what follows
is the attributed speaker's text. Named interjections embedded in speech text use square
brackets for the party qualifier — e.g. `(Dr. Weber [CDU/CSU]: Sehr richtig!)` — and
are never matched as speaker attributions.

### Inline attributions (Zwischenfragen)

Inline attributions of the form `Name (Location) (Party): text on same line` are also
recognised as new speech segments. The colon is not at the end of the line.

### Within-session name normalisation

Speaker names are normalised within each session using Levenshtein distance ≤ 1
clustering (after umlaut folding). This corrects OCR misreads of the same speaker's
name that recur across multiple attribution lines within a single session. The majority
surface form (by occurrence count; length as tiebreaker) is used for all occurrences.
Normalisation is applied only to speakers sharing the same non-null `role`; regular
members (`role = null`) are excluded to avoid merging distinct MPs with similar names.

### Repeated-speaker sequences

A new sequence begins every time a speaker attribution line is encountered, regardless
of whether the speaker is the same as the previous sequence. This occurs when a speaker
is interrupted — by applause, a Zwischenruf, or a procedural note — the interruption
is recorded in the transcript, and the speaker then resumes with their attribution
repeated. Sequences beginning with a leading dash (`-`) in the text body are a
typographic convention in older sessions indicating resumed speech after interruption.
