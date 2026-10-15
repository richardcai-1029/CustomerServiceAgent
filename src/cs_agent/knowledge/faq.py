"""Layered FAQ matcher: the zero-LLM fast path for frequent questions.

Layers, cheapest first; the first confident hit wins:
  1. exact match on the normalized question or one of its variants
  2. BM25 shortlist + two-way token coverage check (near-paraphrases)
  3. embedding cosine similarity above a strict threshold
Requests that carry personal context or ask for an action skip the FAQ so they
reach the agent, which can look up the customer's own data.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import snowballstemmer
import yaml
from langchain_core.embeddings import Embeddings
from rank_bm25 import BM25Okapi

log = logging.getLogger(__name__)

_CONTRACTIONS = {
    "what's": "what is", "where's": "where is", "how's": "how is", "it's": "it is",
    "don't": "do not", "doesn't": "does not", "can't": "cannot", "won't": "will not",
    "i'm": "i am", "i'd": "i would", "i've": "i have", "you're": "you are",
}
_STOPWORDS = {
    "a", "an", "the", "is", "are", "am", "was", "do", "does", "did", "i", "you", "your",
    "we", "our", "us", "me", "my", "can", "could", "would", "will", "should", "to", "of",
    "for", "in", "on", "at", "it", "this", "that", "be", "please", "hi", "hello", "hey",
    "there", "any", "have", "has", "with", "and", "or", "so", "just", "tell", "about",
    "know", "want", "like", "some", "if",
}
# Personal context or action requests: these need the agent, not a canned answer.
_SKIP_PATTERNS = [
    re.compile(r"\b(?:ord[-\s]?)?\d{4,}\b", re.I),  # order numbers, phone numbers, codes
    re.compile(r"\bmy\s+(?:\w+\s+)?(?:order|package|parcel|refund|account|balance|return|"
               r"appointment|delivery|address|password|headphones|watch|laptop|tablet|speaker)s?\b", re.I),
    re.compile(r"\bi\s*(?:want|need|would like|'d like|wanna)\s+to\b", re.I),
]

_stemmer = snowballstemmer.stemmer("english")


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    for short, full in _CONTRACTIONS.items():
        text = text.replace(short, full)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    words = [w for w in normalize(text).split() if w not in _STOPWORDS]
    return _stemmer.stemWords(words)


@dataclass
class FAQEntry:
    id: str
    question: str
    answer: str
    variants: list[str] = field(default_factory=list)

    @property
    def phrasings(self) -> list[str]:
        return [self.question, *self.variants]


@dataclass
class FAQMatch:
    faq_id: str
    question: str
    answer: str
    score: float
    layer: str  # "exact" | "bm25" | "embedding"


def load_faq(path: Path) -> list[FAQEntry]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    return [FAQEntry(id=r["id"], question=r["question"], answer=r["answer"].strip(),
                     variants=r.get("variants", [])) for r in raw]


class FAQMatcher:
    def __init__(self, entries: list[FAQEntry], embeddings: Embeddings | None = None, *,
                 token_coverage: float = 0.75, embedding_threshold: float = 0.82):
        self.entries = entries
        self.embeddings = embeddings
        self.token_coverage = token_coverage
        self.embedding_threshold = embedding_threshold

        # One "document" per phrasing, mapped back to its entry.
        self._owners: list[FAQEntry] = []
        self._phrasings: list[str] = []
        for entry in entries:
            for p in entry.phrasings:
                self._owners.append(entry)
                self._phrasings.append(p)
        self._exact = {normalize(p): owner for p, owner in zip(self._phrasings, self._owners)}
        self._tokens = [tokenize(p) for p in self._phrasings]
        self._bm25 = BM25Okapi(self._tokens) if self._tokens else None
        self._phrasing_vectors: list[list[float]] | None = None

    @classmethod
    def from_yaml(cls, path: Path, embeddings: Embeddings | None = None, **kwargs) -> "FAQMatcher":
        return cls(load_faq(path), embeddings, **kwargs)

    @staticmethod
    def should_skip(text: str) -> bool:
        return any(p.search(text) for p in _SKIP_PATTERNS)

    def match(self, text: str) -> FAQMatch | None:
        if not text.strip() or self.should_skip(text):
            return None
        return self._match_exact(text) or self._match_bm25(text) or self._match_embedding(text)

    def _hit(self, entry: FAQEntry, score: float, layer: str) -> FAQMatch:
        return FAQMatch(entry.id, entry.question, entry.answer, round(score, 3), layer)

    def _match_exact(self, text: str) -> FAQMatch | None:
        entry = self._exact.get(normalize(text))
        return self._hit(entry, 1.0, "exact") if entry else None

    def _match_bm25(self, text: str) -> FAQMatch | None:
        query = tokenize(text)
        if not query or self._bm25 is None:
            return None
        scores = self._bm25.get_scores(query)
        best = max(range(len(scores)), key=scores.__getitem__)
        if scores[best] <= 0:
            return None
        q, d = set(query), set(self._tokens[best])
        overlap = len(q & d)
        # Both directions must be covered, so neither side has many extra words.
        coverage = min(overlap / len(q), overlap / len(d)) if d else 0.0
        if coverage >= self.token_coverage:
            return self._hit(self._owners[best], coverage, "bm25")
        return None

    def _match_embedding(self, text: str) -> FAQMatch | None:
        if self.embeddings is None:
            return None
        try:
            if self._phrasing_vectors is None:
                self._phrasing_vectors = self.embeddings.embed_documents(self._phrasings)
            query = self.embeddings.embed_query(text)
        except Exception:  # The fast path must never break the conversation.
            log.exception("FAQ embedding lookup failed; falling through to the agent")
            return None
        sims = [_cosine(query, v) for v in self._phrasing_vectors]
        best = max(range(len(sims)), key=sims.__getitem__)
        if sims[best] >= self.embedding_threshold:
            return self._hit(self._owners[best], sims[best], "embedding")
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
