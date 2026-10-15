import shutil
from pathlib import Path

from langchain_core.embeddings import DeterministicFakeEmbedding

from cs_agent.config import PROJECT_ROOT
from cs_agent.knowledge.faq import FAQMatcher, load_faq
from cs_agent.knowledge.ingest import ingest, load_chunks, open_vectorstore
from cs_agent.knowledge.retriever import KnowledgeRetriever

FAQ_PATH = PROJECT_ROOT / "data" / "faq.yaml"
DOCS = PROJECT_ROOT / "data" / "docs"


def matcher(**kwargs) -> FAQMatcher:
    return FAQMatcher(load_faq(FAQ_PATH), **kwargs)


def test_faq_exact_match_ignores_case_and_punctuation():
    hit = matcher().match("what's your RETURN policy??")
    assert hit and hit.faq_id == "return-policy" and hit.layer == "exact"


def test_faq_bm25_matches_reordered_paraphrase():
    hit = matcher().match("shipping cost - how much is it?")
    assert hit and hit.faq_id == "shipping-cost" and hit.layer == "bm25"


def test_faq_misses_unrelated_question():
    assert matcher().match("Does the Aurora X2 support LDAC?") is None


def test_faq_skips_personal_and_action_requests():
    m = matcher()
    assert m.match("What is the return policy for ORD-1001?") is None
    assert m.match("I want to return my headphones") is None
    assert m.match("how long does my refund take") is None


class _OneHotEmbeddings:
    """Embeds texts mentioning 'weekend' onto one axis, everything else onto another."""

    def _vec(self, text: str) -> list[float]:
        return [1.0, 0.0] if "weekend" in text.lower() else [0.0, 1.0]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def test_faq_embedding_layer():
    hit = matcher(embeddings=_OneHotEmbeddings()).match("Is the support team around at the weekend")
    assert hit and hit.faq_id == "business-hours" and hit.layer == "embedding"


def test_pdf_chunks_cite_pages():
    chunks = load_chunks(DOCS / "aurora-x2-headphones-manual.pdf", "aurora-x2-headphones-manual.pdf")
    assert {c.metadata["page"] for c in chunks} == {1, 2, 3, 4, 5}
    assert chunks[0].page_content.startswith("[Aurora X2 Headphones Manual - page 1]")


def test_markdown_chunks_cite_sections():
    chunks = load_chunks(DOCS / "novabook-air-14-user-guide.md", "novabook-air-14-user-guide.md")
    sections = {c.metadata.get("section") for c in chunks}
    assert "Troubleshooting > The laptop won't turn on" in sections


def test_incremental_ingest(tmp_path: Path, settings):
    docs = tmp_path / "docs"
    docs.mkdir()
    shutil.copy(DOCS / "warranty-and-repair-policy.md", docs)
    shutil.copy(DOCS / "shipping-and-returns-guide.txt", docs)
    store = open_vectorstore(tmp_path / "chroma", DeterministicFakeEmbedding(size=32))
    manifest = tmp_path / "manifest.json"

    first = ingest(store, docs, manifest)
    assert sorted(first.added) == ["shipping-and-returns-guide.txt", "warranty-and-repair-policy.md"]

    second = ingest(store, docs, manifest)
    assert second.chunks_written == 0 and len(second.unchanged) == 2

    (docs / "shipping-and-returns-guide.txt").write_text("Short replacement text about shipping.")
    (docs / "warranty-and-repair-policy.md").unlink()
    third = ingest(store, docs, manifest)
    assert third.updated == ["shipping-and-returns-guide.txt"]
    assert third.removed == ["warranty-and-repair-policy.md"]
    assert store.get(where={"source": "warranty-and-repair-policy.md"})["ids"] == []
    assert len(store.get(where={"source": "shipping-and-returns-guide.txt"})["ids"]) == 1

    settings.rag_min_relevance = -1.0  # fake embeddings carry no meaning
    passages = KnowledgeRetriever(store, settings).search("shipping")
    assert passages and passages[0].source == "shipping-and-returns-guide.txt"
