# Blueprint: building a parliamentary-protocol corpus pipeline

This describes the architecture of the Bundestag Plenarprotokolle pipeline so the
same thing can be built for another corpus — the Bundesrat — on another machine.
It is written for a Claude Code session starting from scratch on that corpus.

It is a **blueprint, not a port**. Do not copy the Bundestag regexes: the document
layout, the attribution grammar and the membership register are all different.
Copy the *structure*, the *contracts between stages*, and above all the
*method* in the last two sections, which is where the time actually went.

---

## 0. Before writing any code: establish the ground truth

Answer these first, from the real documents. Every one of them changed a design
decision in the Bundestag pipeline, and guessing any of them costs a rebuild.

1. **Where do the source PDFs come from?** A stable URL pattern, or a search
   interface? Is there an ETag / Last-Modified header? Are documents ever
   silently *revised* after publication?
2. **What identifies a sitting?** The Bundestag uses `WP<term>_Session<nnn>`.
   The Bundesrat numbers its sittings continuously (Sitzung 1 … 1050+) with no
   electoral term, so the key is probably just `Session<nnnn>` — decide this
   once, because it is baked into every filename, log line and ID.
3. **What does a speaker attribution look like?** Collect 30 real examples
   across the whole date range, including the oldest. In the Bundestag they are
   `Name (Ort) (Partei):` and `Name, Rolle:`. The Bundesrat is a chamber of
   *Land governments*: expect `Name (Land), Ministerpräsident:` or
   `Minister X (Bayern):`, so **Land is the primary qualifier, not party**, and
   party may be absent entirely. This single difference reshapes the whole
   speaker module.
4. **Is there a membership register?** The Bundestag has `MdB-Stammdaten.XML`,
   which gives gender, term and party per member and is the backbone of
   attribution. If the Bundesrat has no equivalent, gender and identity
   resolution need a different source — and you must say so in the data paper
   rather than inferring gender from first names (see §9, "gender_guesser").
5. **How many documents, and how large?** This decides whether a full rebuild is
   a coffee break or an overnight job. For the Bundestag it is ~4,650 sittings
   and ~1.05 M speeches; a forced re-clean plus re-attribution of everything
   takes *hours*, which is why §6 exists.
6. **Two-column layout?** Old scanned protocols are two-column with column
   markers. If so, extraction order will be wrong and §9 applies.

---

## 1. Repository layout

```
<project>/
  src/                     one module per stage, named so they sort in order
    a_01_fetch.py
    b_00_text_extractor.py
    b_01_text_cleaning.py
    b_02_speaker_identifier.py
    c_01_validate_corpus.py
    e_01_gender.py
    f_01_build_dataset.py
    h_01_quality_audit.py
    run_pipeline.py            text stages only, for bulk work
    monthly_update.py          the incremental driver (§5)
    utils.py                   append_log and friends
  docs/                    reference DATA, hand-curated, version-controlled
    <register>.XML
    parties.csv / chambers.csv
    name_corrections.csv
    role_corrections.csv
    location_is_party.csv
    excluded_false_speakers.csv
    manual_gender_overrides.csv
  data/
    raw/                   PDFs as downloaded, never edited
    preprocessed/
      WP01/1949/<id>.txt            extracted text
      WP01/1949/<id>_cleaned.txt    cleaned text
      WP01/1949/<id>_speakers.json  parsed attributions
      processed.log  cleaning.log  speakers.log    resume logs
      metadata/
  outputs/                 reports, figures, tables, logs, _update_state.json
  <corpus>_dataset_current/   the working release
  <corpus>_dataset_v1/        the frozen, published release
  scripts/run_monthly_update.ps1
  CLAUDE.md                project record — see §10
```

Two conventions matter more than they look:

- **Stage prefixes (`a_`, `b_00`, `b_01`, …) sort into execution order.** Anyone
  — including a future session — can read the directory and know the order.
- **Derived files sit next to their input**, sharing a stem. `<id>.txt`,
  `<id>_cleaned.txt`, `<id>_speakers.json`. Invalidation is then a glob
  (§5), and a half-finished run is obvious from the filesystem.

---

## 2. Stage contracts

Every stage obeys the same four rules. They are what make the incremental
update possible, so do not bend them for convenience.

1. **One input root, one output root**, both injectable as CLI arguments.
   Never hard-code a path in a function body. Hard-coded defaults at module
   top-level are fine, and useful, but every one must be overridable.
2. **Idempotent.** Running a stage twice produces the same result.
3. **Resume-safe.** Each stage appends the path of each output it writes to a
   log (`processed.log`, `cleaning.log`, `speakers.log`). On start it reads the
   log into a set and skips work already recorded. `--force` ignores the log.
4. **`--wp` / scope filter.** Every bulk stage can be restricted to a subset.
   This is what turns a 4-hour rebuild into a 20-minute one when a fix only
   touches three terms. **Build this in from day one.** It is painful to add
   later and you will want it on the first real bug.

### a_01 — fetch

Downloads PDFs and maintains a manifest keyed by session id, recording ETag,
Last-Modified, content length and MD5. On each run:

- a session absent from the manifest is **new**;
- a session whose ETag differs from the manifest is **revised** — the chamber
  silently replaces corrected protocols, and a revised document must be
  reprocessed from scratch, not skipped;
- only the last *N* sittings are re-checked for revision (`--revision-window`),
  because re-requesting the whole archive every month is rude and slow.

Returns a `FetchReport` dataclass: `new_sessions`, `revised_sessions`,
`backfilled_pdfs`, `checked_for_revision`, `errors`. The driver consumes this;
it is not just logging. Support `--dry-run` that fetches nothing and prints the
report — you will use it constantly.

### b_00 — extraction

PDF → plain text, one file per sitting, with an explicit page separator
(`\n\n--- Page Break ---\n\n`). **Keep the page breaks.** Several later fixes
(column ordering, duplicate-page removal) are only possible while pages are
still addressable, and the cleaner removes the markers at the end.

Record *which* extractor produced the text. See §9 on `pdftotext`.

### b_01 — cleaning

A list of named functions applied in order:

```python
PIPELINE = [
    ("drop_duplicate_pages",      drop_duplicate_pages),
    ("fix_column_order",          fix_column_order),
    ("remove_page_breaks",        remove_page_breaks),
    ...
    ("rejoin_wrapped_qualifiers", rejoin_wrapped_qualifiers),
]

def clean(text):
    for _name, fn in PIPELINE:
        text = fn(text)
    return text
```

This shape is worth copying exactly. It makes order explicit and reviewable, it
lets you run the pipeline up to step *n* in a probe script, and each function
is independently testable on a string. **Order is load-bearing** — write the
reason into a comment beside every entry, including what must come before and
after it, because the reasons are not recoverable later.

### b_02 — speaker attribution

The hardest stage, and the one you must not copy. It turns cleaned text into
`{name, role, party, location}` per speech.

Structure it as a small set of named patterns tried in order, each documented
with a real example:

- *chair prefix*: `Präsident Müller:`
- *name + parenthesised qualifiers*: `Schmidt (Hitzhofen) (GRÜNE):`
- *name + comma + office*: `Schmidt, Bundesminister der Finanzen:`
- *name + qualifier + procedural role*: `Dr. Kather (GB/BHE), Anfragender:`

Then one classifier that decides what each parenthetical **means**. The
Bundestag rule is:

1. a document reference, applause note or stage direction → discard
2. a known Bundesland → `location`
3. a known party abbreviation → `party`
4. first remaining ambiguous qualifier → `location`
5. second remaining ambiguous qualifier → `party`

For the Bundesrat, rules 2–5 will need rethinking: the Land is the *affiliation*,
not a disambiguator, and the office is usually present. Expect to want a `land`
column and perhaps no `party` column at all.

**Two failure modes to design against from the start**, because both cost real
data here:

- *An unknown label falls through to the wrong field.* Rule 3 is a lookup
  against a CSV. Anything missing from that CSV silently lands in `location`.
  A whole parliamentary group (236 speeches, a current one) was invisible this
  way for months. **Emit a warning listing qualifier values that matched no
  rule**, and read it.
- *A recognised value with nowhere to go is dropped.* An office in a
  parenthetical was correctly refused as "not a location, not a party" and then
  discarded, because the parser had no `role` slot on that code path. Make
  every classifier return *all* the fields it can fill, and assert that a
  non-empty qualifier always lands somewhere or is explicitly logged as
  discarded.

### e_01 — identity and gender

Join the parsed names to the membership register. Record **where each value came
from** in a `*_source` column (`register`, `title_prefix`, `role_title`,
`manual`, `inferred`, `unknown`). This provenance column is not decoration: it
is how you later prove a change improved things, and how you notice a weaker
source displacing a stronger one.

### f_01 — dataset build

Assembles the release: one row per speech, plus speaker and session tables, a
`datapackage.json`, a README and a citation file. This is also where **all
curated corrections are applied**, as numbered sub-stages — see §4.

Order within f_01 matters:

```
2c  exclusion list          (keys on raw observed names, so it runs first)
2d  structural repairs
2e  name canonicalisation
2f  recover a role the parser discarded
2g  role canonicalisation   (after 2f, so recovered roles are canonicalised too)
2h  move mis-filed party labels out of location
3   identity / gender join  (sees the corrected names and roles)
```

Take a `--version` argument and **write it into the metadata**. See §9.

### c_01 — validation, h_01 — quality audit

Two different jobs; keep them separate.

- `c_01` checks the *text*: page-header leakage, unrejoined hyphens,
  attribution patterns surviving in a speech body, CID artefacts, sequence gaps.
- `h_01` flags *speeches*: one row per speech with boolean columns, written to
  a CSV so you can pivot on them.

Neither catches structural or cross-field problems. Add a third probe (§8).

---

## 3. The bulk runner

`run_pipeline.py` chains the text stages (`b_00` → `b_01` → `b_02`) with
`--force`, `--skip-extract` and the scope filter. Used for "I changed a cleaning
rule, re-clean everything". It deliberately does **not** build the dataset.

---

## 4. Curated data belongs in CSVs, not in code

Everything hand-decided lives in `docs/*.csv`, is read at run time, and is
version-controlled. Not in a Python dict, not in a regex.

| file | purpose |
|---|---|
| `excluded_false_speakers.csv` | `name,reason` — drops non-person attributions |
| `name_corrections.csv` | `name_original,name_canonical` — merges spelling variants |
| `role_corrections.csv` | `role_original,role_canonical` |
| `location_is_party.csv` | repairs a label filed in the wrong column |
| `manual_gender_overrides.csv` | gender for names the register cannot resolve |

Rules for these files, each learned the hard way:

- **Support `#` comment lines, and read them with `comment="#"`.** Then a
  correction that was *considered and rejected* can be recorded beside the ones
  applied, with the reason. Without this the same bad suggestion gets
  re-proposed every time someone reads the candidate list.
- **Refuse to chain.** If a target is itself another row's source, the result
  depends on row order. Detect it and skip, with a warning.
- **Only ever fill a field that is empty.** A correction table must never
  overwrite what the parser resolved itself.
- **Log what was applied**: `"Canonicalised 1,205 speeches across 395 variants;
  1,203 merged into a speaker already present"`. That one line is how you spot a
  table doing far more, or far less, than intended.
- **Watch the trailing newline.** One reference CSV shipped without a final
  newline; appending to it concatenated the new first row onto the last existing
  row and corrupted both. Check, or always write via `csv.writer`.

---

## 5. The incremental driver

`monthly_update.py` is the whole point of the architecture: it brings the corpus
up to date without reprocessing everything. Ten stages:

```
 1 Fetch (a_01)                → FetchReport
     if nothing new or revised: stop, say so, exit 0
 2 Invalidate revised sessions
 3 Extraction (b_00)           scoped
 4 Cleaning (b_01)             scoped
 5 Speaker identification (b_02)
 6 Identity / gender (e_01)
 7 Dataset build (f_01)        with every correction CSV passed explicitly
 8 Validation (c_01)
 9 Quality audit (h_01)
10 Change report
```

**Stage 2 is the subtle one.** A revised protocol already has derived files and
log entries, so every stage would skip it. Invalidation must delete the derived
files *and* remove the matching lines from every resume log:

```python
for path in wp_dir.glob(f"*/{stem}*"):
    path.unlink()
for log_name in ("processed.log", "cleaning.log", "speakers.log"):
    kept = [ln for ln in log.read_text().splitlines() if stem not in ln]
    log.write_text("\n".join(kept) + "\n")
```

Forgetting the logs produces the worst kind of bug: a corrected document that
silently keeps its old text forever.

**Stage 10** compares a snapshot of the built dataset against the previous run,
stored in `outputs/_update_state.json`, and writes a dated Markdown report:
sessions and speeches added, new party values, new role strings, names with
unresolved identity. End it with a line telling the reader to review it before
announcing a release. Treat any *new* party or role string as a question, not a
fact — that is how an unknown label gets noticed in month one rather than
month nine.

Flags worth having: `--dry-run`, `--skip-fetch`, `--wp`, `--revision-window`,
and a guard that **refuses to write into the published release directory**
without an explicit `--allow-v1`.

---

## 6. Two releases, and never confuse them

Keep `<corpus>_dataset_v1/` (published, frozen, cited) separate from
`<corpus>_dataset_current/` (the working release).

Stamp the version into `datapackage.json` and the README, and derive it from the
output directory:

```python
version_label = "v1" if dataset.name.endswith("_v1") else "v1.1-internal"
```

In this project both directories declared `"version": "v1"` for months, because
the build defaulted to `v1` and the driver never overrode it. The working copy
described itself as the published deposit — exactly the thing a colleague needs
to be able to tell apart.

---

## 7. Scheduling (Windows)

A thin PowerShell wrapper, registered as a scheduled task:

- resolve the project root from `$PSScriptRoot`, never a hard-coded path;
- log to `outputs/logs/<name>_<timestamp>.log`, keeping the last ~12;
- record which external tools are on `PATH` at run time — a scheduled task has a
  different environment from your shell, and this is the first thing you will
  want when a run behaves differently;
- exit with the Python exit code so the scheduler records failure.

Two Windows traps:

- **PowerShell 5.1 writes `*>>` redirection as UTF-16.** Mixed with UTF-8 output
  from Python you get `S t a g e  1` in the log. Append through
  `cmd /c "... >> log 2>&1"` instead.
- **`schtasks /Create` cannot parse an `&` in a path.** Register with
  `Register-ScheduledTask -Xml` and escape the command with
  `[System.Security.SecurityElement]::Escape`.

---

## 8. Probe the dataset, not just the text

The validation and audit stages check the things you thought of while writing
them. Add a third script that checks *invariants*, and re-run it after every
significant change. Checks that each found something real here:

- duplicate `speech_id`; duplicate `(session_id, sequence)`
- `char_offset` decreasing within a sitting
- exact duplicate `(session, speaker, text)` for texts over ~200 characters
- names containing digits, party labels, role words; names over 45 characters
- a speaker with more than one gender
- one register id used for several names — **this is also the best source of
  name-variant candidates**, because the matcher has already decided they are
  one person
- `year` disagreeing with the date field; impossible dates (a day of `00`)
- a party or affiliation appearing outside the term it existed in
- a field whose content belongs in another field

Report counts first, examples second, and **state explicitly which checks
returned rows that are *not* defects** — lower-case `von` surnames, genuinely
short surnames, very short speeches. Otherwise the same false alarms get
re-investigated every time.

---

## 9. Traps that cost real time here

**Extraction**

- *Know which binary is extracting your text.* `pdftotext` on Windows may be
  Xpdf (bundled with Git for Windows) rather than Poppler; they produce
  different layouts. If the tool is missing, a fallback library may silently
  take over for the entire corpus. Log the tool and its version per run.
- *Two-column pages are read in horizontal bands*, giving ACBD block order.
  Fix it before removing page breaks.
- *Some PDFs contain pages twice*, and one contained the entire protocol twice,
  doubling that sitting's speech count. Detect it, but see the two near-misses
  below.
- *Cloud-synced folders* (OneDrive, Sciebo) keep placeholder files. Reads fail
  intermittently with `[Errno 22]`. Retry, and do not treat the first failure
  as data loss.

**Attribution**

- *A long name pushes its parenthesised qualifier onto the next line*, and a
  pattern that forbids newlines inside the parentheses then matches nothing, so
  the speech is absorbed into the previous speaker. This cost ~1,800 speeches,
  90% from one party and 59% from women against a 27% baseline — **a layout bug
  with a demographic signature**. If your attribution regex forbids a newline
  anywhere, ask what happens when the typesetter wraps.
- *Do not infer gender from first names.* A name-based guesser was measured here
  and rejected: it would have displaced a stronger signal on ~7,000 speeches to
  gain 577, and it encodes its own biases. Use the register; curate the
  remainder by hand; record `unknown` honestly.

**Correction tables**

- *A string-distance match is not a person match.* Of five candidates marked
  "approved" in a review spreadsheet, one merged a Green woman (124 speeches)
  into a CDU man, because the sheet had no party or term columns. Three
  successive filters each failed on their own — party/term/gender overlap, string
  similarity, and presence in the membership register (blind to speakers who
  were never members). **The field that finally discriminated was the office**:
  a speaker carrying a government post is not a misspelling of a backbencher.
  Only two classes are safe to automate — strings identical once encoding is
  normalised, and diacritic-only differences where the plain form is not itself
  a real name. Everything else needs a human.
- *Near-miss, duplicate-page detection, twice.* A fingerprint that stripped
  non-letters made every page of a proprietary-font sitting look alike —
  1.5 MB of real debate, nearly deleted. A fingerprint that stripped digits made
  100 distinct recorded votes identical, since each lists the same 459 members
  and differs only in its page number and document number. **The noise you are
  normalising away may be the only thing distinguishing two records.**

---

## 10. Method — the part that actually matters

1. **Measure before changing.** Capture a snapshot (row counts, distinct values
   per field, a watch-list of specific records) before, and diff after. A change
   that cannot be shown to do what you claimed is not finished.
2. **Pre-flight every rule over the whole corpus before applying it.** Print
   what it would change, how often, and the distinct values it touches. Three
   rules here were rejected at this stage, after looking plausible.
3. **Write adversarial unit tests, including cases that must *not* change.**
   Half the value is in the negatives.
4. **Make the arithmetic close.** If a change should move 31 records and the
   totals move by 135, something else moved too — find it before continuing.
   That check caught a 124-speech gender flip.
5. **Fix the root cause in the cheap layer when the expensive one would cost
   hours**, and write down that you did. A correction applied at dataset-build
   time is re-applied on every build and costs minutes; the parser fix can wait
   for the next full rebuild, as long as it is recorded.
6. **Keep a `CLAUDE.md` in the project root** holding the architecture, the
   current numbers, decisions taken *with their reasoning*, approaches tried and
   rejected, and known-open defects. Update it as part of the work, not at the
   end. Assume the session that did the work is gone and only this file remains.
7. **Report honestly.** If a rebuild did not run, say so. If a filter you
   proposed turned out to be unsound, withdraw it explicitly rather than
   quietly shipping a smaller version.
