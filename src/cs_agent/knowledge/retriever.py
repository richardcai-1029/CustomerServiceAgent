"""Knowledge-base search: vector recall, optional DashScope rerank, thresholds."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from langchain_chroma import Chroma

from cs_agent.config import Settings

log = logging.getLogger(__name__)

# (query, documents, top_n) -> [(index into documents, relevance score)], best first.
Reranker = Callable[[str, Sequence[str], int], list[tuple[int, float]]]


@dataclass
class Passage:
    text: str
    source: str
    title: str
    location: str  # "page 3", "Troubleshooting > ...", or ""
    score: float

    @property
    def citation(self) -> str:
        return f"{self.source}" + (f", {self.location}" if self.location else "")


def dashscope_reranker(settings: Settings) -> Reranker:
    import dashscope

    def rerank(query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        response = dashscope.TextReRank.call(
            model=settings.rerank_model,
            query=query,
            documents=list(documents),
            top_n=top_n,
            return_documents=False,
            api_key=settings.dashscope_api_key,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Rerank failed: {response.code} {response.message}")
        return [(r.index, r.relevance_score) for r in response.output.results]

    return rerank


class KnowledgeRetriever:
    def __init__(self, store: Chroma, settings: Settings, reranker: Reranker | None = None):
        self.store = store
        self.settings = settings
        self.reranker = reranker

    def search(self, query: str) -> list[Passage]:
        s = self.settings
        hits = self.store.similarity_search_with_relevance_scores(query, k=s.rag_fetch_k)
        candidates = [
            Passage(
                text=doc.page_content,
                source=doc.metadata.get("source", "unknown"),
                title=doc.metadata.get("title", ""),
                location=doc.metadata.get("section")
                or (f"page {doc.metadata['page']}" if "page" in doc.metadata else ""),
                score=round(score, 3),
            )
            for doc, score in hits
            if score >= s.rag_min_relevance
        ]
        if not candidates:
            return []
        if self.reranker is None:
            return candidates[: s.rag_top_n]
        try:
            ranked = self.reranker(query, [p.text for p in candidates], s.rag_top_n)
        except Exception:
            log.exception("Rerank failed; falling back to vector order")
            return candidates[: s.rag_top_n]
        result = []
        for index, score in ranked:
            if score >= s.rag_min_rerank_score:
                passage = candidates[index]
                passage.score = round(score, 3)
                result.append(passage)
        return result
