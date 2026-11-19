"""Offline label-to-MFDS candidate search. Retrieval scores never authorize a verdict."""
import argparse
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERIC = frozenset('acid as and from in of for the with containing extract standardized powder blend complex root roots seed seeds leaf leaves bark fruit plant herb oil salt citrate sulfate sulphate chloride hydrochloride hcl phosphate acetate bitartrate sodium potassium calcium magnesium zinc other ingredient ingredients organic mg mcg g 등 추출물 분말'.split())
CAS_PATTERN = re.compile(r'\b\d{2,7}-\d{2}-\d\b')
DASHES = str.maketrans('−–—‑‐αβγΔ', '-----abgd')
TRADEMARKS = str.maketrans('', '', '™℠®')
REFERENCE_RANKABLE = frozenset({'same_compound', 'taxon_name', 'form_or_part'})
RANK = {'normalized_exact': 1, 'identifier': 2, 'whole_phrase': 3, 'reference_trail': 4,
        'token_overlap': 5, 'character_ngram': 6}
SCOPE_LANGUAGE = re.compile(r'[*＊]|제외|에 한함|\b(?:except|only)\b', flags=re.I)


def jsonl(path):
    with Path(path).open(encoding='utf-8') as rows:
        return [json.loads(row) for row in rows if row.strip()]


def normalize(text):
    text = unicodedata.normalize('NFKC', (text or '').translate(TRADEMARKS)).translate(DASHES).casefold()
    return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in text).split())


def meaningful(text):
    return frozenset(t for t in text.split() if len(t) >= 3 and not t.isdigit() and t not in GENERIC)


def grams(text):
    return frozenset(text[i:i + 3] for i in range(len(text) - 2) if text[i:i + 3] != '   ')


def overlap(left, right):
    return 2 * len(left & right) / (len(left) + len(right)) if left and right else 0


def cas_numbers(text):
    for candidate in CAS_PATTERN.findall(text):
        digits = candidate.replace('-', '')
        if sum(i * int(d) for i, d in enumerate(reversed(digits[:-1]), 1)) % 10 == int(digits[-1]):
            yield candidate


def whole(needle, haystack):
    return bool(needle) and f' {needle} ' in f' {haystack} '


@dataclass(frozen=True, slots=True)
class Name:
    id: str
    text: str
    source: str
    source_url: str
    relation: str
    official: bool
    clean: str
    tokens: frozenset
    trigrams: frozenset

    @classmethod
    def from_row(cls, registry_id, text, source, source_url, relation, official=False):
        clean = normalize(text)
        return cls(registry_id, text, source, source_url, relation, official, clean, meaningful(clean), grams(clean))

    def result(self, method, score):
        return {'id': self.id, 'matched_name': self.text, 'official': self.official,
                'source': self.source, 'source_url': self.source_url, 'relation': self.relation,
                'match': method, 'score': round(score, 4), 'authority': 'candidate_only'}


class IngredientKB:
    def __init__(self, registry, common):
        self.entries = {r['id']: r for r in registry}
        self.names = []
        self.cas = defaultdict(list)
        self.cas_links = defaultdict(set)
        self.cids = defaultdict(set)
        self.common = defaultdict(list)
        self.scoped = set()
        for row in registry:
            ident = row['id']
            # Only flags explicit scope wording; absence of the flag is not approval.
            if SCOPE_LANGUAGE.search(' '.join((row['kr'], row['en'], *row['etc']))):
                self.scoped.add(ident)
            for field, values in (('kr', [row['kr']]), ('en', [row['en']]), ('etc', row['etc'])):
                for text in dict.fromkeys(v for v in values if v):
                    self.names.append(Name.from_row(ident, text, 'mfds', row['official_source'], f'official_{field}', True))
            for ref in row.get('references', []):
                if ref['relation'] in REFERENCE_RANKABLE or ref['relation'] == 'uncertain':
                    self.names.append(Name.from_row(ident, ref['name'], ref['source'], ref['source_url'], ref['relation']))
                if ref.get('cid'):
                    self.cids[str(ref['cid'])].add(ident)
            for code in row.get('identifiers', []):
                if code['scheme'] == 'pubchem_cid':
                    self.cids[str(code['value'])].add(ident)
                elif code['scheme'] == 'cas':
                    value = str(code['value'])
                    self.cas_links[value].add(ident)
                    if code['relation'] in REFERENCE_RANKABLE:
                        self.cas[value].append(Name.from_row(
                            ident, value, code.get('source', 'identifier_reference'),
                            code['source_url'], code['relation']))
        for row in common:
            # A CID resolves the recorded label spelling, not other spellings in a loose alias list.
            self.common[normalize(row['name'])].append(row)

    @classmethod
    def load(cls, kb=HERE / 'kb.jsonl', common=HERE / 'common.jsonl'):
        return cls(jsonl(kb), jsonl(common))

    def search(self, label, k=5):
        """Return at most five ranked candidates plus a separate, non-authoritative common hint."""
        if not 1 <= k <= 5:
            raise ValueError('k must be in 1..5')
        if not isinstance(label, str) or len(label) > 1024:
            raise ValueError('label must be a string of at most 1024 characters')
        query = normalize(label)
        tokens = meaningful(query)
        trigrams = grams(query)
        pool = {}
        conflicts = set()

        def add(name, method, score):
            key = (RANK[method], -score, not name.official, name.clean, name.id)
            if name.id not in pool or key < pool[name.id][0]:
                pool[name.id] = (key, name.result(method, score))
            if method in ('normalized_exact', 'identifier', 'reference_trail') or (
                    method == 'whole_phrase' and (name.official or name.relation == 'same_compound')):
                conflicts.add(name.id)

        if query:
            for name in self.names:
                if not name.clean or name.clean in GENERIC:
                    continue
                if name.relation == 'uncertain':
                    if (query == name.clean or len(name.clean) >= 4 and name.tokens and whole(name.clean, query)):
                        add(name, 'reference_trail', 1.0 if query == name.clean else 0.8)
                    continue
                if query == name.clean:
                    add(name, 'normalized_exact', 1.0)
                elif len(name.clean) >= 4 and name.tokens and whole(name.clean, query):
                    add(name, 'whole_phrase', min(0.99, 0.75 + 0.24 * len(name.clean) / len(query)))
                elif tokens & name.tokens:
                    add(name, 'token_overlap', overlap(tokens, name.tokens))
                elif len(query) >= 5 and len(name.clean) >= 5 and tokens and name.tokens:
                    score = overlap(trigrams, name.trigrams)
                    if score >= 0.28:
                        add(name, 'character_ngram', score)
            for cas in set(cas_numbers(label)):
                for name in self.cas[cas]:
                    add(name, 'identifier', 1.0)

        candidates = [item for _, item in sorted(pool.values(), key=lambda pair: pair[0])[:k]]
        for item in candidates:
            item['scope_check'] = item['id'] in self.scoped or item['relation'] in ('taxon_name', 'form_or_part')
        common = self.common.get(query, [])
        verified = [r for r in common if r['identity_status'] == 'exact_cid' and r.get('cid') and r.get('identity_source_url')]
        cids = {str(r['cid']) for r in verified}
        cid_conflicts = set().union(*(self.cids[cid] for cid in cids))
        substance = [r['substance_evidence'] for r in common if r.get('substance_evidence')]
        cas_conflicts = set().union(*(self.cas_links[x['cas']] for x in substance if x.get('cas')))
        hint = None
        if common:
            if conflicts or cid_conflicts or cas_conflicts or len(cids) > 1:
                status = 'conflict'
            elif len(cids) == 1 and len(verified) == len(common):
                status = 'known_identity_no_indexed_collision'
            elif substance and len(substance) == len(common):
                status = 'named_substance_no_indexed_collision'
            else:
                status = 'identity_unresolved'
            hint = {'status': status, 'label_match': 'normalized_exact', 'matches': common,
                    'name_collision_ids': sorted(conflicts), 'cid_collision_ids': sorted(cid_conflicts),
                    'cas_collision_ids': sorted(cas_conflicts),
                    'authority': 'reference_only'}
        return {'label': label, 'normalized': query, 'candidates': candidates, 'common_hint': hint}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    parser.add_argument('--kb', type=Path, default=HERE / 'kb.jsonl')
    parser.add_argument('--common', type=Path, default=HERE / 'common.jsonl')
    args = parser.parse_args()
    print(json.dumps(IngredientKB.load(args.kb, args.common).search(args.label), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
