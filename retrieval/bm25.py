"""Local Russian lexical retrieval over one immutable index generation."""
from collections import Counter, defaultdict
from math import log1p
import re
import unicodedata

import snowballstemmer


WORDS = re.compile(r"\d+(?:\.\d+)+|[^\W_]+", re.UNICODE)
STOP = frozenset("а в во на к у по и с со из для о об от это как кто что где когда какой какая какие ли чем сколько можно нужно пункт пункте п".split())
POLITE_INTRO = re.compile(r"\b(?:пожалуйста|подскажите|каким образом)\b")
SEPARATED_ABBREVIATION = re.compile(r"(?<!\w)[a-zа-я](?:[.\-‑–][a-zа-я]){1,5}(?!\w)")
RUSSIAN = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
LATIN_LOOKALIKES = str.maketrans("abcehkmoptxy", "авсенкмортху")
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

        def join_abbreviation(match):
            joined = re.sub(r"[^a-zа-я]", "", match[0])
            return joined if joined in self.abbreviations else match[0]

        text = SEPARATED_ABBREVIATION.sub(join_abbreviation, text)
        if self.pattern:
            text = self.pattern.sub(lambda m: self.aliases[m[0]], text)
        return [self.stemmer.stemWord(w) if len(w) >= 4 and w.isalpha() and w not in
                self.abbreviations and w not in QUALIFIERS else w
                for w in WORDS.findall(text) if w not in STOP]

    def scores(self, question, allowed, coverage, *, repair=True):
        terms = set(self.terms(question))
        scores = self._scores(terms, allowed, coverage)
        if scores or not repair:
            return scores
        simplified = set(self.terms(POLITE_INTRO.sub(" ", normalize(question))))
        if simplified != terms:
            scores = self._scores(simplified, allowed, coverage)
        if scores:
            return scores
        corrected = self._repair(question, simplified, allowed)
        return self._scores(corrected, allowed, coverage) if corrected else {}

    def _repair(self, question, terms, allowed):
        def eligible(term):
            return (term not in self.abbreviations and term not in QUALIFIERS
                    and any(i in allowed for i, _ in self.postings.get(term, ())))

        unknown = {}
        for raw in WORDS.findall(question):
            word = normalize(raw)
            if not 5 <= len(word) <= 18 or not word.isalpha() or raw.isupper():
                continue
            stem = self.stemmer.stemWord(word)
            if stem in terms and stem not in self.postings and word not in self.abbreviations:
                unknown[stem] = word
        if len(unknown) > 4:
            return None
        for stem, word in unknown.items():
            if re.search(r"[а-я]", word) and re.search(r"[a-z]", word):
                canonical = word.translate(LATIN_LOOKALIKES)
                match = self.stemmer.stemWord(canonical)
                matches = {match} if eligible(match) else set()
            elif all(letter in RUSSIAN for letter in stem):
                matches = {candidate for candidate in self._one_edits(stem) if eligible(candidate)}
            else:
                continue
            if len(matches) == 1:
                return (terms - {stem}) | matches
        return None

    @staticmethod
    def _one_edits(term):
        for i in range(len(term) + 1):
            left, right = term[:i], term[i:]
            for letter in RUSSIAN:
                yield left + letter + right
                if right and letter != right[0]:
                    yield left + letter + right[1:]
            if right:
                yield left + right[1:]
            if len(right) > 1 and right[0] != right[1]:
                yield left + right[1] + right[0] + right[2:]

    def _scores(self, terms, allowed, coverage):
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
