# Methodology

## Data Processing Pipeline for Bundestag Plenarprotokolle

This document describes the methodology used to convert raw PDF transcripts of the
German Bundestag into a structured, speaker-attributed speech corpus. It is written
to support the methods section of a research paper and can be adapted directly.

---

### 1. Data Source

The primary data source consists of the official verbatim transcripts
(*Stenografische Berichte* / *Plenarprotokolle*) of the German Bundestag, covering
all twenty parliamentary terms (*Wahlperioden*, WP) from the first Bundestag in 1949
(WP01) to the ongoing twentieth term (WP20, from 2021). Transcripts were obtained as
PDF files from the Bundestag open-data portal
([https://www.bundestag.de/protokolle](https://www.bundestag.de/protokolle)).

Each transcript records every speech, interjection, and procedural note from a single
plenary session in full. The corpus therefore captures the complete deliberative
record of the German parliament across more than seven decades.

The PDF corpus divides into two distinct generations. Sessions from WP01 through WP09
(1949–1980) are scanned documents: the original printed transcripts were photographed
and processed through optical character recognition (OCR). Sessions from WP10 onwards
(1983–present) are born-digital PDFs produced directly from typesetting software.
This distinction has significant implications for text quality and extraction strategy,
as described below.

---

### 2. Text Extraction

Text was extracted from PDF files using the command-line tool `pdftotext` (part of
the `poppler` library), with `pdfminer` as a fallback when `pdftotext` produced empty
output or was unavailable.

For the scanned WP01–WP09 sessions, extraction used the `-raw` flag, which reads the
PDF content stream in its encoded order rather than applying layout analysis. This flag
is critical for scanned two-column layouts: the OCR layer in these PDFs encodes text
in left-column-first order, faithfully reflecting the reading order of the original
printed page. Without `-raw`, `pdftotext`'s layout analysis interleaves lines from
both columns, destroying the speaker attribution sequence and producing mixed-column
speech bodies. The trade-off is that 1–3 sentences from the adjacent column may
occasionally appear at the boundary of a speech segment; this artefact has negligible
impact on bag-of-words analysis.

For born-digital WP10–WP20 sessions, `-raw` was not used, as these documents encode
text in a single logical stream that layout analysis handles correctly.

Metadata (session date, Wahlperiode number, session number, and city) was extracted
both from the PDF filename and from the document header text, then cross-validated.
Discrepancies between the two sources were logged as warnings. The final resolved
values were stored alongside the extracted text.

---

### 3. Text Cleaning

Extracted text was cleaned through an eleven-step pipeline designed on a conservative
principle: artefacts were removed only when they could be identified with certainty;
ambiguous content was retained. No speech content was discarded.

The following artefact classes were removed:

**Repeated page headers.** Each page of a Plenarprotokoll carries a header line
identifying the Wahlperiode, session number, city, and date. These headers repeat
across every page and appear as noise throughout the extracted text. Two header formats
were targeted — the older format used in Bonn (*N. Sitzung. Bonn, Datum*) and the
newer Berlin format (*N. Wahlperiode – N. Sitzung. Berlin, Datum*) — along with
several OCR-induced variants:
- WP18: `pdftotext` inserts spaces around the period in session numbers (`18 . Wahlperiode`)
- WP18/WP19: a leading space precedes "Deutscher"
- WP07/WP08: column markers (`(A)`, `(C)`) appear directly before the header text
- WP08: a page number and CID glyph token appear on the same line as the header

The last variant required that CID token removal (see below) be placed before header
removal in the pipeline order, so that compound artefacts were resolved in the correct
sequence.

**CID glyph-mapping failures.** OCR and some PDF encodings produce `(cid:N)` tokens
where a glyph could not be mapped to a Unicode character. These tokens were removed
globally before any content-dependent processing.

**Table of contents.** Each session transcript opens with a table of contents listing
speakers and page numbers. This section was identified using a two-pass anchor
strategy: a strong anchor searched for the session-opening phrase
(*"Die Sitzung wird … eröffnet"* or *"Beginn: HH.MM Uhr"*); if absent, a fallback
anchor used the first presidential speaker-attribution line. Content before the anchor
matching table-of-contents patterns (dot-leader lines, spaced-letter headings) was
removed; everything after the anchor was untouched.

**Standalone page numbers, footnotes, column markers, and separator lines.**
Lines consisting solely of a page number, footnote markers (`*`, `†`, numbered or
lettered references), column position markers (`(A)`–`(D)`), or single-character
separator lines (OCR artefacts from printed vertical rules) were removed.

**Back-matter reference blocks.** The final pages of each session transcript contain
blocks of EU and Ratsdokument references (*Drucksache*, *Ratsdokument*). These were
removed conservatively within detected back-matter zones.

**End-of-line hyphenation.** Both scanned and born-digital PDFs break long German
words across lines with a hyphen. A two-case rejoining rule was applied: when the
character following the hyphen and line break is lowercase, the hyphen is removed and
the word halves are concatenated (e.g. `Her-\nren` → `Herren`); when the following
character is uppercase, the hyphen is retained and only the line break is removed
(e.g. `CDU-\nCSU` → `CDU-CSU`), preserving legitimate compound hyphenation.
Whitespace normalisation was applied before hyphen-joining so that blank lines
containing only spaces — a pattern occurring in some scanned sessions — did not
obscure line-break hyphen patterns.

---

### 4. Speaker Identification and Speech Segmentation

The cleaned transcript text was segmented into individual speeches by identifying
speaker attribution lines. A speaker attribution line is defined as a standalone line
ending with a colon (`:`). The colon rule is reliable across all Wahlperioden: named
interjections embedded within speech text use square brackets for party qualification
(e.g. `[CDU/CSU]: Sehr richtig!`) and are never confused with speaker attributions.

Four speaker attribution patterns were identified through inspection of sessions across
all twenty parliamentary terms:

**Pattern A (Chair roles):** The Bundestagspräsident/in, Vizepräsident/in, or
Alterspräsident/in is identified by a role keyword preceding the name.
Older sessions additionally use the honorifics *Frau* and *Herr*.

**Pattern B (Regular members):** A member's attribution consists of the full name,
optionally preceded by academic titles (*Dr.*, *Prof. Dr.*), followed by one or more
parenthesised qualifiers and the terminal colon. Qualifiers identify the member's
constituency or city and party affiliation.

**Pattern C (Government officials):** Ministers, the Chancellor, Parliamentary State
Secretaries, and State Ministers are identified by their name followed by a comma and
their official title. State-level representatives (*Ministerpräsident/in*,
*Regierender Bürgermeister/in*) may appear with the role prefix before the name,
analogously to Pattern A.

**Pattern D (Parliamentary procedural roles):** Rapporteurs (*Berichterstatter/in*)
and questioners (*Anfragender/Anfragerin*) follow a name-qualifier-comma-role format.

Parenthetical qualifiers appearing after a member's name were classified in a fixed
priority order: Drucksache and Frage references were ignored; German state names
(*Bundesländer*) were always classified as location qualifiers; strings matching a
known party abbreviation were classified as party; the first remaining ambiguous
qualifier was treated as location and the second as party. If no party qualifier was
present, the `party` field was set to null rather than inferred.

Party name abbreviations vary across eras for the same political grouping. The
classifier handled the following known variants: `F.D.P.` (WP10–WP12) versus `FDP`;
`GRÜNE` and `DIE GRÜNEN` (early sessions) versus `BÜNDNIS 90/DIE GRÜNEN` (WP11
onwards); `PDS/Linke Liste` (WP12) versus `Die Linke` (WP17 onwards); `DKP`
(WP06–WP10); `NPD` (WP05–WP06); and `fraktionslos` for non-attached members.

**Inline attributions (Zwischenfragen).** Follow-up questions interjected during a
speech (*Zwischenfragen*) follow a distinct format where the attribution and text
appear on the same line, separated by a colon. These were identified as a secondary
pattern and treated as new speech segments beginning at the colon.

**Within-session name normalisation.** OCR errors occasionally produce variant
spellings of the same speaker's name across multiple attribution lines within a single
session (e.g. an umlaut misread as a different character). Within each session, speaker
names sharing the same non-null role were clustered by Levenshtein edit distance ≤ 1
(after Unicode umlaut folding). The majority surface form — determined by occurrence
count, with name length as a tiebreaker — was substituted for all minority variants.
This normalisation was restricted to speakers with identified roles to avoid
incorrectly merging distinct regular members with similar names.

**Repeated-speaker sequences.** A new speech sequence is created each time a speaker
attribution line is encountered, even when the same speaker appears consecutively.
This reflects the structure of parliamentary transcripts, in which a speaker may be
interrupted by applause, a Zwischenruf, or a procedural note, after which the
transcript records the interruption and then marks the speaker's resumption with a
repeated attribution line. In older sessions (WP01–WP09), resumed speech is
additionally indicated by a leading dash at the start of the text body, a typographic
convention of the era.

---

### 5. Related Work

The Open Discourse project (Richter et al.) has produced a full-corpus structured
dataset from the same source documents using a similar extraction and attribution
approach. The schema and design principles of the present pipeline draw on the Open
Discourse documentation (https://open-discourse.github.io/open-discourse-documentation/).

An example of downstream analysis applied to this type of corpus is:
> Haunss, S., Sommer, M., & Hollway, J. (2020). Who cares about coal? Analyzing
> 70 years of German parliamentary debates on coal with dynamic topic modeling.
> *Energy Research & Social Science*, 70, 101869.
> https://doi.org/10.1016/j.erss.2020.101869
