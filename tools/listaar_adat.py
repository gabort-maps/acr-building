#!/usr/bin/env python3
"""A listaar.html épület- és helyiségadatait frissíti az acr-admin.html PACKS adataiból.

Használat (a repó gyökeréből):  python3 tools/listaar_adat.py
Csak a szükséges mezőket viszi át (szintek, szárnyak, falmértékek, helyiségek), a rajzokat nem.
"""
import json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADMIN, PAGE = ROOT / 'acr-admin.html', ROOT / 'listaar.html'
START, END = '/*ADAT-KEZDETE*/', '/*ADAT-VÉGE*/'

line = next((l for l in ADMIN.read_text(encoding='utf-8').split('\n') if l.startswith('const PACKS = ')), None)
if line is None:
    sys.exit('Nem található a PACKS sor az acr-admin.html-ben.')
packs = json.loads(line[line.index('{'):line.rstrip().rstrip(';').rindex('}') + 1])

out = {}
for bid, p in packs.items():
    out[bid] = {
        'name': p['name'],
        'lvl': [[k, lab] for k, lab, *_ in p['lvl']],
        'wings': [[w[0], w[2]] for w in p['wings']],
        'wmeas': p.get('wmeas', {}),
        'def': p.get('def', {}),
        'parents': {k: {'a': v['a']} for k, v in (p.get('parents') or {}).items()},
        'rooms': [{k: r[k] for k in ('id', 'k', 'w', 'code', 'name', 'a', 'cat', 'par', 'rem') if k in r} for r in p['rooms']],
    }

data = json.dumps(out, ensure_ascii=False, separators=(',', ':'))
html = PAGE.read_text(encoding='utf-8')
i, j = html.index(START), html.index(END)
PAGE.write_text(html[:i + len(START)] + data + html[j:], encoding='utf-8')
print('Kész:', {b: len(v['rooms']) for b, v in out.items()}, 'helyiség,', len(data), 'bájt')
