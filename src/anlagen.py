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
# "Dr.-Ing." is one title, but the hyphen stopped the title group and then the
# name itself, so "Bundesministers Dr.-Ing. Seebohm" published an author of
# "Dr" — 84 written contributions, mostly Seebohm in WP04-06. The form occurs
# 806 times in the corpus, against 666 "Dr. h. c." and 83 "Prof. Dr.".
_NAME = (r"(?:Dr\.(?:\s*-\s*Ing\.)?\s*|Prof\.\s*|h\.\s*c\.\s*|Frau\s+|von\s+|van\s+|de\s+)*"
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
    # Zusatzpunkt and Tagesordnungspunkt are spelt out rather than left to
    # "Punkt": the alternation matches a whole word, so "zu Zusatzpunkt 3"
    # was not caught by "Punkt" and published "Dr. Stecker zu Zusatzpunkt".
    r"(?:Protokoll|Punkt|Zusatzpunkt|Tagesordnungspunkt|Tagesordnung|"
    r"Drucksache|Nummer|Ziffer|Artikel|Abstimmung|Beratung|Beginn)\s*$")

# The name may also run on into the first word of the sentence that follows,
# when that word is capitalised and so indistinguishable from a further name
# part: "Erklärung des Abgeordneten Deringer Ich stimme …" gave "Deringer Ich".
# Only closed-class words are listed — none of them is a German surname, so the
# trim cannot shorten a genuine name.
_RE_TRAILING_FUNCTION_WORD = re.compile(
    r"\s+(?:Ich|In|Im|Zu|Zur|Zum|Der|Die|Das|Dem|Den|Des|Es|Auf|Bei|Mit|Nach|"
    r"Wie|Wir|Und|Als|Aus|Durch|F[üu]r|Hat|Ist|Sind|Wird|Werden|Kann|Trifft|"
    r"Welche|Wann|Warum|Was|Wer|Wo|Ein|Eine|Am|An|Um|Bis|Da|Diese|Dieser|"
    r"Dieses|Sei|Seit|Gegen|[ÜU]ber|Unter|Vor|Von|Wegen)\s*$")

# A first name split mid-word by the extraction, e.g. "Parl. Staatssekretärs
# Be rnd Wilz". The fragment is of arbitrary length and lower case, so the
# letter-group rule in b_01 (which mends "Wi rt schaft") cannot see it.
#
# A purely structural rule would do real damage: across the corpus it would
# also join "Jan van Oordt" into "Janvan" (47), "Kohl und Frau X" into
# "Kohlund" (99), "Graf von Stauffenberg" into "Grafvon" (66) and "Jahn zur
# Sache" into "Jahnzur" (37). The discriminator is a gazetteer the project
# already ships: the 926 first names and 3,757 surnames in
# docs/MdB-Stammdaten.xml. Measured over all 4,652 sittings the rule fires on
# 942 occurrences in 241 distinct forms, and every one of those forms was
# checked by eye to be a real name — Adenauer, Genscher, Lambsdorff,
# Waffenschmidt, Dresbach, Heinrich, Erich, Brigitte — while 19,780 candidate
# occurrences are rejected, which are the function-word phrases above. A form
# the gazetteer does not know is left exactly as it was: the rule fails closed,
# so an unknown name stays visibly broken rather than silently invented.
#
# The gazetteer, not the surrounding shape, is what makes this safe. An earlier
# version also demanded a capitalised word after the break, on the assumption
# that a split first name is always followed by a surname. That is only true
# when the FIRST name is the broken one: when the surname breaks, what follows
# is ordinary lower-case prose — "Dr. A rndt vom 12. Februar", "Georg Ga llus
# auf die Frage" — and the rule silently skipped every one of them.
_RE_SPLIT_FIRSTNAME = re.compile(
    # The office word anchors the repair to the one position where a short
    # capitalised token followed by a lower-case chunk can only be a broken
    # name. It sits inside the match rather than in a lookbehind, because
    # Python requires a fixed-width lookbehind; _sub re-emits it untouched.
    r"\b(?:Staatssekret[äa]rs?|Staatssekret[äa]rin|Staatsministers?"
    r"|Staatsministerin|Bundesministers?|Bundesministerin|Bundeskanzlers?"
    r"|Bundeskanzlerin|Abgeordneten)\s+(?:Dr\.\s*)?"
    # an intact first name may stand between the office and the break:
    # "Staatssekretärs Georg Ga llus"
    r"(?:[A-ZÄÖÜ][^\W\d_A-ZÄÖÜ]{2,}\s+)?"
    # The head may be a single letter — "Dr. E rich Riedl", "M anfred Carstens".
    # A genuine initial carries a full stop, which [a-zäöüß]{2,} cannot match.
    r"(?P<head>[A-ZÄÖÜ][a-zäöüß]{0,3})\s+(?P<tail>[a-zäöüß]{2,})\b")

# the title part of a capture, so _clean can tell a name from a bare honorific
_RE_TITLE_ONLY = re.compile(
    r"\b(?:Dr\.(?:\s*-\s*Ing\.)?|Prof\.|h\.\s*c\.|Frau|von|van|de)\b\.?")

_NAME_GAZETTEER: frozenset = frozenset()


def _load_name_gazetteer(path=None) -> frozenset:
    """Known names from the MdB Stammdaten; an empty set disables the repair."""
    import pathlib
    import xml.etree.ElementTree as ET
    p = (pathlib.Path(path) if path else
         pathlib.Path(__file__).resolve().parent.parent / "docs" / "MdB-Stammdaten.xml")
    if not p.exists():
        return frozenset()
    try:
        root = ET.parse(p).getroot()
    except ET.ParseError:
        return frozenset()
    names = set()
    for tag in ("VORNAME", "NACHNAME"):
        for e in root.iter(tag):
            if e.text:
                for part in re.split(r"[\s-]+", e.text.strip()):
                    if len(part) > 2:
                        names.add(part.lower())
    return frozenset(names)


def _mend_split_first_name(header: str) -> str:
    """Rejoin a name broken mid-word, when the gazetteer knows the result."""
    global _NAME_GAZETTEER
    if not _NAME_GAZETTEER:
        _NAME_GAZETTEER = _load_name_gazetteer()
    if not _NAME_GAZETTEER:
        return header

    def _sub(m: re.Match) -> str:
        head, tail = m.group("head"), m.group("tail")
        if (head + tail).lower() not in _NAME_GAZETTEER:
            return m.group(0)
        # splice by offset rather than by string replace: the office prefix can
        # itself contain the "head tail" text, and replace() would hit it first
        s, e = m.start("head") - m.start(), m.end("tail") - m.start()
        return m.group(0)[:s] + head + tail + m.group(0)[e:]

    return _RE_SPLIT_FIRSTNAME.sub(_sub, header)

# "Antwort des Parl. Staatssekretärs Würzbach auf die Fragen des Abgeordneten …"
# Feminine forms are explicit: an earlier version matched only the masculine
# and silently dropped every answer given by a woman.
RE_ANSWER = re.compile(
    r"Antwort(?:en)?\s+(?P<art>des|der)\s+"
    r"(?:Parl\.?\s*|Parlamentarischen?\s+)?"
    r"(?P<office>Staatssekret[äa]rs?|Staatssekret[äa]rin|Staatsministers?"
    r"|Staatsministerin|Bundesministers?|Bundesministerin|Bundeskanzlers?"
    r"|Bundeskanzlerin)\s+"
    rf"(?-i:(?P<author>{_NAME}))", re.I)
# "Erklärung nach § 31 GO des Abgeordneten Wolfgang Tiefensee"
RE_DECLARATION = re.compile(
    r"Erkl[äa]rung(?:en)?\s+(?:nach\s+§\s*3[01]\s*(?:GO)?\s*)?"
    r"(?P<art>des|der)\s+(?P<office>Abgeordneten)\s+"
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
        header = _mend_split_first_name(" ".join(sub[:260].split()))
        if RE_UNAUTHORED.search(header):
            subs.append({"kind": "unauthored", "author": None,
                         "header": header, "text": sub})
            continue
        if RE_ZU_PROTOKOLL.search(header):
            inner = list(RE_INNER_SPEAKER.finditer(sub))
            if inner:
                for j, im in enumerate(inner):
                    end = inner[j + 1].start() if j + 1 < len(inner) else len(sub)
                    who = _clean(im.group("who"))
                    # no article to read here: a speech given to the record
                    # carries an ordinary attribution, so only "Frau" informs
                    subs.append({"kind": "written", "author": who,
                                 "gender": _header_gender(who, None, None),
                                 "header": header,
                                 "text": sub[im.end():end]})
                continue
            subs.append({"kind": "unauthored", "author": None,
                         "header": header, "text": sub})
            continue
        a = RE_ANSWER.search(header) or RE_DECLARATION.search(header)
        if a:
            author = _clean(a.group("author"))
            subs.append({"kind": "written", "author": author,
                         "gender": _header_gender(author, a.group("art"),
                                                  a.group("office")),
                         "header": header, "text": sub})
            continue
        subs.append({"kind": "unauthored", "author": None,
                         "header": header, "text": sub})
    return head, subs


def _header_gender(author: str, art: str, office: str):
    """Gender of the author, from the grammar of the header. None if unclear.

    German case marking settles this where a name list cannot. "Antwort des
    Parl. Staatssekretärs" is genitive masculine singular and can only be a
    man; "der Parl. Staatssekretärin" can only be a woman. That is evidence,
    not a guess from the forename, and it reaches the 1960s state secretaries
    whose forename is never printed.

    Two refusals matter more than the two rules:

    - "der Abgeordneten" is both genitive singular feminine AND genitive
      plural, so "Erklärung der Abgeordneten Müller, Schmidt und Weber" says
      nothing about any one of them. Only an explicitly feminine office form
      makes "der" decisive, so a bare "der Abgeordneten" returns None.
    - the office is sometimes inflected masculine for a woman: "Antwort des
      Staatsministers Frau Dr. Hamm-Brücher". "Frau" in the name is the
      stronger signal and is checked first, or the grammar would make her a
      man.
    """
    if re.match(r"\s*Frau\b", author or ""):
        return "female"
    art = (art or "").strip().lower()
    feminine_office = (office or "").strip().lower().endswith("in")
    if art == "der" and feminine_office:
        return "female"
    if art == "des" and not feminine_office:
        return "male"
    return None


def _clean(s: str) -> str:
    s = " ".join(s.split())
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)      # drop a trailing (SPD)
    s = _RE_TRAILING_PREPOSITION.sub("", s)
    # Trim a run-on sentence word, but never down to a bare title: if nothing
    # but "Dr." or "Frau" would survive, the capture was wrong in a way this
    # cannot repair, and the original is the more honest record.
    trimmed = _RE_TRAILING_FUNCTION_WORD.sub("", s)
    if re.search(r"[A-ZÄÖÜ][^\W\d_A-ZÄÖÜ]+",
                 _RE_TITLE_ONLY.sub("", trimmed)):
        s = trimmed
    return s.strip(" ,:;")
