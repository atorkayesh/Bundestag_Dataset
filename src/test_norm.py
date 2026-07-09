"""Quick regression test for b_02 normalization rules."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import b_02_speaker_identifier as b

def nr(r):
    d = {'role': r, 'party': None, 'name': 'T', 'location': None}
    return b._clean_parsed(d)['role']

def np_(p):
    d = {'role': None, 'party': p, 'name': 'T', 'location': None}
    return b._clean_parsed(d)['party']

role_cases = [
    # Landwirtschaft — split correction applies regardless of umlaut context
    ('Bundesminister fuer Ernaehrung, Land wirtschaft und Forsten',
     'Bundesminister fuer Ernaehrung, Landwirtschaft und Forsten'),
    ('Bundesminister für Ernährung, Landwirt schaft und Forsten',
     'Bundesminister für Ernährung, Landwirtschaft und Forsten'),
    ('Bundesminister für Ernährung, Landwirtchaft und Forsten',
     'Bundesminister für Ernährung, Landwirtschaft und Forsten'),
    ('Bundesminister für Ernährung, Landschaft und Forsten',
     'Bundesminister für Ernährung, Landwirtschaft und Forsten'),
    ('Bundesminister für Ernährung, Landwirtschaft mid Forsten',
     'Bundesminister für Ernährung, Landwirtschaft und Forsten'),
    ('Bundesminister für Ernährung, Landwirtwirtschaft und Forsten',
     'Bundesminister für Ernährung, Landwirtschaft und Forsten'),
    # Wirtschaft
    ('Bundesminister für Wi rtschaft', 'Bundesminister für Wirtschaft'),
    ('Bundesminister für Wirschaft', 'Bundesminister für Wirtschaft'),
    ('Bundesminister für Wartschaft', 'Bundesminister für Wirtschaft'),
    ('Bundesminister für Wirtchaft', 'Bundesminister für Wirtschaft'),
    # dm/heim
    ('Staatssekretär dm Bundesministerium des Innern',
     'Staatssekretär im Bundesministerium des Innern'),
    ('Parl. Staatssekretär heim Bundesminister der Justiz',
     'Parl. Staatssekretär beim Bundesminister der Justiz'),
    # ides/deis
    ('Bundesminister ides Innern', 'Bundesminister des Innern'),
    ('Bundesminister deis Auswärtigen', 'Bundesminister des Auswärtigen'),
    # missing space
    ('Bundesministerfür Verkehr', 'Bundesminister für Verkehr'),
    ('BundesministerderJustiz', 'Bundesminister der Justiz'),
    ('Bundesministerdes Auswärtigen', 'Bundesminister des Auswärtigen'),
    # Innern variants
    ('Bundesminister des Innerv', 'Bundesminister des Innern'),
    ('Bundesminister des Innnern', 'Bundesminister des Innern'),
    ('Bundesminister des Inneren', 'Bundesminister des Innern'),
    # Jusitz
    ('Bundesminister der Jusitz', 'Bundesminister der Justiz'),
    # Verteidigung
    ('Bundesminister der Verteidigng', 'Bundesminister der Verteidigung'),
    ('Bundesminister der Vertreidigung', 'Bundesminister der Verteidigung'),
    # Auswärtigen
    ('Bundesminister des Auswärten', 'Bundesminister des Auswärtigen'),
    ('Bundesminister des Auswattigen', 'Bundesminister des Auswärtigen'),
    # Gesundheit
    ('Bundesminister für Gesundheistwesen', 'Bundesminister für Gesundheitswesen'),
    ('Bundesminister für Gesundheitwesen', 'Bundesminister für Gesundheitswesen'),
    # Sozialordnung
    ('Bundesminister für Arbeit und Sozialordunng', 'Bundesminister für Arbeit und Sozialordnung'),
    ('Bundesminister für Arbeit und Sozilordnug', 'Bundesminister für Arbeit und Sozialordnung'),
    # Trailing punct
    ('Parl. Staatssekretär beim Bundesminister des Auswärtigen.',
     'Parl. Staatssekretär beim Bundesminister des Auswärtigen'),
    # Bundesministerium genitive
    ('Staatssekretär im Bundesministeriums der Finanzen',
     'Staatssekretär im Bundesministerium der Finanzen'),
    ('Staatssekretär des Bundesministeriums der Finanzen',
     'Staatssekretär des Bundesministeriums der Finanzen'),
    # Leakage
    ('Bundesminister für Arbeit und Sozia-Hubertus Heil, Bundesminister für Arbeit und Soziales',
     'Bundesminister für Arbeit und Soziales'),
    ('Bundesminister für Arbeit und Sozia-Überweisungsvorschlag',
     'Bundesminister für Arbeit und Soziales'),
    ('Bundesminister für Arbeit und Sozial-Ordnung',
     'Bundesminister für Arbeit und Sozialordnung'),
    ('Parl. Staatssekretär beim Bun-Vizepräsident Dr. Hermann Otto Solms',
     'Parl. Staatssekretär beim Bundesminister'),
    ('Parl. Staatssekretär bei der Bun-Matthias Hauer (CDU/CSU)',
     'Parl. Staatssekretär bei der Bundesministerin'),
    ('Parl. Staatssekretär im Bun-Ich rufe den Tagesordnungspunkt 2 auf',
     'Parl. Staatssekretär im Bundesministerium'),
    # No regressions on already-correct forms
    ('Bundesminister der Finanzen', 'Bundesminister der Finanzen'),
    ('Bundesministerin für Gesundheit', 'Bundesministerin für Gesundheit'),
    ('Parl. Staatssekretär beim Bundesminister des Innern',
     'Parl. Staatssekretär beim Bundesminister des Innern'),
    ('Bundeskanzler', 'Bundeskanzler'),
    ('Präsident', 'Präsident'),
    ('Staatssekretär im Bundesministerium für Wirtschaft',
     'Staatssekretär im Bundesministerium für Wirtschaft'),
]

party_cases = [
    ('CDU / CSU', 'CDU/CSU'),
    ('CDU /CSU', 'CDU/CSU'),
    ('CDU/ CSU', 'CDU/CSU'),
    ('BÜNDNIS 90 /DIE GRÜNEN', 'BÜNDNIS 90/DIE GRÜNEN'),
    ('F VP', 'FVP'),
    ('alle CDU/CSU', 'CDU/CSU'),
    ('beide CDU/CSU', 'CDU/CSU'),
    ('Frankfurt', None),
    ('Recklinghausen', None),
    ('Wangen', None),
    ('vom Platz aus sprechend', None),
    ('vom Platz sprechend', None),
    ('zum Präsidenten', None),
    ('auf den Abg. Spies zeigend', None),
    ('der Abgeordnete trägt einen Button mit der Aufschrift: WAA nein', None),
    ('Gast', None),
    ('Gast FDP', None),
    ('SPD-Gast', None),
    ('Dr ucksache 9/1297 Fragen 130 und 131', None),
    ('F.D.P.', 'FDP'),
    ('FPD', 'FDP'),
    ('Die Linke', 'DIE LINKE'),
    ('fraktionlos', 'fraktionslos'),
    ('CDU %CSU', 'CDU/CSU'),
    ('parteilos', 'parteilos'),
    ('SDP', 'SDP'),
    ('SPD', 'SPD'),
    ('BÜNDNIS 90/DIE GRÜNEN', 'BÜNDNIS 90/DIE GRÜNEN'),
    ('DIE LINKE', 'DIE LINKE'),
    ('CDU/CSU', 'CDU/CSU'),
    ('FDP', 'FDP'),
]

ok = err = 0
print('--- Role tests ---')
for inp, exp in role_cases:
    got = nr(inp)
    if got == exp:
        ok += 1
    else:
        err += 1
        print(f'FAIL: {inp!r}')
        print(f'  exp: {exp!r}')
        print(f'  got: {got!r}')

print('--- Party tests ---')
for inp, exp in party_cases:
    got = np_(inp)
    if got == exp:
        ok += 1
    else:
        err += 1
        print(f'FAIL party: {inp!r} -> {got!r}  (exp: {exp!r})')

total = len(role_cases) + len(party_cases)
print(f'\nTotal: {ok}/{total} pass, {err} fail')
sys.exit(0 if err == 0 else 1)
