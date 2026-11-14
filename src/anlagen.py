"""Split the Anlagen appendix into authored and unauthored sub-blocks.

The Anlagen are printed after the sitting closes — written answers to
parliamentary questions, declarations under §31 GO, speeches given to the
record, alongside lists of excused members, roll-call results and Umdrucke.
Nothing removed them, so they were swallowed by whichever speech was open when
they began, almost always the chair's closing words. That gave presiding
officers enormous "speeches" of material they never said: 3,178 of 4,644
sittings, 69.7 M characters, 3.8% of the corpus, with one 721,767-character
Renger "speech" in WP10 session 194 of which 705,387 was appendix.

Two kinds, following the treatment used for the Bundesrat corpus:

  authored   a named person's contribution that was simply never delivered
             aloud. Published with contribution_type = "written".
  unauthored no personal author — a list, a result, an official notice.
             Not published; counted per sitting so the exclusion is visible.

Measured over the corpus: 18,620 authored sub-blocks (38.4 M chars) against
11,373 unauthored (31.2 M chars). Authored contributions are concentrated in
the Fragestunde-heavy 1970s — 3,494 in WP07 and 5,707 in WP08 against 28 in
WP21 — so a time series that pools them records a change in practice rather
than in the chamber.
"""
import re

RE_ANLAGEN_START = re.compile(
    r"(?:^|\s)(?:Anlagen?\s+1\b|Anlagen\s+zum\s+Stenogra[fp]hischen\s+Bericht)")
RE_SUB = re.compile(r"\bAnlage\s+\d{1,3}\b")

# Case-sensitive by construction: the keyword part of the patterns below is
# matched with re.I, and under that flag [A-ZÄÖÜ] also matches lower case, so
# an earlier version captured "Wrede auf die Schriftliche" as an author name.
# The hyphen is NOT in the inner class — with it, "Hamm-Brücher" consumed the
# hyphen and stopped, yielding "Hamm-".
_NAME = (r"(?:Dr\.\s*|Prof\.\s*|h\.\s*c\.\s*|Frau\s+|von\s+|van\s+|de\s+)*"
         # [^\W\d_A-ZÄÖÜ] is "any letter that is not upper case", which keeps
         # Dağdelen and Hamm-Brücher intact; an explicit [a-zäöüß] class cut
         # "Sevim Dağdelen" to "Sevim Da".
         r"[A-ZÄÖÜ][^\W\d_A-ZÄÖÜ]+"
         # A nobiliary particle only counts between two name parts. Without the
         # lookahead this swallowed the preposition in "Erklärung des
         # Abgeordneten Dr. Atzenroth zu § 10", giving "Dr. Atzenroth zu".
         r"(?:[ -](?:von|van|de|zu)(?=\s+[A-ZÄÖÜ]))?"
         r"(?:[ -][A-ZÄÖÜ][^\W\d_A-ZÄÖÜ]+){0,3}")

# "zu" is a nobiliary particle in "Prinz zu Löwenstein" and "Meyer zu
# Bentrup", but a preposition in "Erklärung des Abgeordneten Spitzmüller zu
# Punkt 5 der Tagesordnung". Capitalisation does not separate the two, and an
# inline negative lookahead proved easy to misplace in the paren nesting — it
# silently never fired. The trailing preposition is trimmed after capture
# instead, where it is obvious and directly testable.
_RE_TRAILING_PREPOSITION = re.compile(
    r"\s+(?:von|van|de|zu)\s+"
    r"(?:Protokoll|Punkt|Tagesordnung|Drucksache|Nummer|Ziffer|Artikel|"
    r"Abstimmung|Beratung|Beginn)\s*$")

# "Antwort des Parl. Staatssekretärs Würzbach auf die Fragen des Abgeordneten …"
# Feminine forms are explicit: an earlier version matched only the masculine
# and silently dropped every answer given by a woman.
RE_ANSWER = re.compile(
    r"Antwort(?:en)?\s+(?:des|der)\s+"
    r"(?:Parl\.?\s*|Parlamentarischen?\s+)?"
    r"(?:Staatssekret[äa]rs?|Staatssekret[äa]rin|Staatsministers?|Staatsministerin|"
    r"Bundesministers?|Bundesministerin|Bundeskanzlers?|Bundeskanzlerin)\s+"
    rf"(?-i:(?P<author>{_NAME}))", re.I)
# "Erklärung nach § 31 GO des Abgeordneten Wolfgang Tiefensee"
RE_DECLARATION = re.compile(
    r"Erkl[äa]rung(?:en)?\s+(?:nach\s+§\s*3[01]\s*(?:GO)?\s*)?"
    r"(?:des|der)\s+Abgeordneten\s+"
    rf"(?-i:(?P<author>{_NAME}))", re.I)
RE_ZU_PROTOKOLL = re.compile(r"[Zz]u\s+Protokoll\s+gegebene[nr]?\s+Rede")

RE_UNAUTHORED = re.compile(
    r"Liste\s+der\s+(?:entschuldigten|beurlaubten)\s+Abgeordneten"
    r"|Entschuldigte\s+Abgeordnete"
    r"|Amtliche\s+Mitteilung"
    r"|Endg[üu]ltige[s]?\s+Ergebnis"
    r"|Namensverzeichnis|Namensliste"
    r"|Unterrichtung\s+durch"
    r"|^\s*Anlage\s+\d+\s+(?:Umdruck|Drucksache)", re.I)

# Inside a "zu Protokoll gegebene Reden" block each contribution carries its
# own ordinary attribution.
RE_INNER_SPEAKER = re.compile(
    rf"^(?P<who>{_NAME}\s*\([^)\n]{{1,40}}\))\s*:\s*", re.M)


def split_anlagen(text: str):
    """Return (speech_text_without_anlagen, [sub-block dicts])."""
    m = RE_ANLAGEN_START.search(text)
    if not m:
        return text, []
    head, block = text[:m.start()], text[m.start():]
    marks = [mm.start() for mm in RE_SUB.finditer(block)]
    if not marks:
        return head, [{"kind": "unauthored", "author": None,
                       "header": "", "text": block}]
    marks.append(len(block))
    subs = []
    for i in range(len(marks) - 1):
        sub = block[marks[i]:marks[i + 1]]
        header = " ".join(sub[:260].split())
        if RE_UNAUTHORED.search(header):
            subs.append({"kind": "unauthored", "author": None,
                         "header": header, "text": sub})
            continue
        if RE_ZU_PROTOKOLL.search(header):
            inner = list(RE_INNER_SPEAKER.finditer(sub))
            if inner:
                for j, im in enumerate(inner):
                    end = inner[j + 1].start() if j + 1 < len(inner) else len(sub)
                    subs.append({"kind": "written", "author": _clean(im.group("who")),
                                 "header": header,
                                 "text": sub[im.end():end]})
                continue
            subs.append({"kind": "unauthored", "author": None,
                         "header": header, "text": sub})
            continue
        a = RE_ANSWER.search(header) or RE_DECLARATION.search(header)
        if a:
            subs.append({"kind": "written", "author": _clean(a.group("author")),
                         "header": header, "text": sub})
            continue
        subs.append({"kind": "unauthored", "author": None,
                         "header": header, "text": sub})
    return head, subs


def _clean(s: str) -> str:
    s = " ".join(s.split())
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)      # drop a trailing (SPD)
    s = _RE_TRAILING_PREPOSITION.sub("", s)
    return s.strip(" ,:;")
