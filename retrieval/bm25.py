"""Local Russian lexical retrieval over one immutable index generation."""
from collections import Counter, defaultdict
from math import log1p
import re
import unicodedata

import snowballstemmer


WORDS = re.compile(r"\d+(?:\.\d+)+|[^\W_]+", re.UNICODE)
STOP = frozenset("а в во на к у по и с со из для о об от это как кто что где когда какой какая какие ли чем сколько можно нужно пункт пункте п".split())
QUALIFIERS = frozenset("не ни без если кроме только при нельзя исключение".split())


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


class BM25:
    def __init__(self, chunks, aliases=None):
        self.stemmer = snowballstemmer.stemmer("russian")
        self.abbreviations = set()
        fields = []
        for c in chunks:
            metadata = " ".join(str(s) for s in [c["document"], c["department"], c["stage"],
                                c["section"], *(s["section"] for s in c["spans"])] if s)
            text = c["index_text"].removeprefix("passage: ") + " " + metadata
            self.abbreviations.update(normalize(w) for w in WORDS.findall(text)
                                      if 2 <= len(w) <= 6 and w.isupper())
            fields.append(text)
        self.aliases = {normalize(k): normalize(v) for k, v in (aliases or {}).items() if k.strip()}
        self.pattern = None
        if self.aliases:
            variants = "|".join(re.escape(k) for k in sorted(self.aliases, key=len, reverse=True))
            self.pattern = re.compile(r"(?<!\w)(?:" + variants + r")(?!\w)")
        self.postings = defaultdict(list)
        self.lengths = []
        for row, text in enumerate(fields):
            terms = self.terms(text)
            self.lengths.append(len(terms))
            for term, freq in Counter(terms).items():
                self.postings[term].append((row, freq))
        self.average = sum(self.lengths) / max(1, len(fields)) or 1
        self.idf = {t: log1p((len(fields) - len(p) + .5) / (len(p) + .5))
                    for t, p in self.postings.items()}

    def terms(self, text):
        text = normalize(text)
        if self.pattern:
            text = self.pattern.sub(lambda m: self.aliases[m[0]], text)
        return [self.stemmer.stemWord(w) if len(w) >= 4 and w.isalpha() and w not in
                self.abbreviations and w not in QUALIFIERS else w
                for w in WORDS.findall(text) if w not in STOP]

    def scores(self, question, allowed, coverage):
        terms = set(self.terms(question))
        anchors = terms - QUALIFIERS
        if not anchors:
            return {}
        scores = defaultdict(float)
        hits = Counter()
        for term in sorted(terms):
            for i, freq in self.postings.get(term, ()):
                if i in allowed:
                    norm = .25 + .75 * self.lengths[i] / self.average
                    scores[i] += self.idf[term] * freq * 2.2 / (freq + 1.2 * norm)
                    hits[i] += term in anchors
        return {i: score for i, score in scores.items()
                if hits[i] and hits[i] / len(anchors) >= coverage}
