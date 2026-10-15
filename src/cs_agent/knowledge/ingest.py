"""Incremental ingestion of data/docs into the Chroma vector store.

A JSON manifest maps each file's relative path to its SHA-256. On every run,
new or changed files are (re)indexed and deleted files are removed, so the
knowledge base always mirrors the docs folder.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from pypdf import PdfReader

log = logging.getLogger(__name__)

COLLECTION = "novatech_docs"
SUPPORTED_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}

_splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=120)
_md_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")], strip_headers=True
)


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    chunks_written: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def open_vectorstore(chroma_dir: Path, embeddings: Embeddings) -> Chroma:
    chroma_dir.mkdir(parents=True, exist_ok=True)
    return Chroma(
        collection_name=COLLECTION,
        embedding_function=embeddings,
        persist_directory=str(chroma_dir),
        collection_metadata={"hnsw:space": "cosine"},
        relevance_score_fn=lambda distance: 1.0 - distance,
    )


def title_from_filename(path: Path) -> str:
    words = path.stem.replace("_", "-").split("-")
    return " ".join(w.upper() if len(w) <= 2 and w.isalpha() else w.capitalize() for w in words)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_chunks(path: Path, rel: str) -> list[Document]:
    """Split one file into chunks carrying citation metadata (page or section)."""
    title = title_from_filename(path)
    pieces: list[tuple[str, dict]] = []  # (text, location metadata)

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        for number, page in enumerate(PdfReader(path).pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pieces.append((text, {"page": number}))
    elif suffix in {".md", ".markdown"}:
        for section in _md_splitter.split_text(path.read_text(encoding="utf-8")):
            headers = [section.metadata[h] for h in ("h1", "h2", "h3") if h in section.metadata]
            # Drop the document title (h1) from the citation unless it is all there is.
            location = " > ".join(headers[1:] or headers)
            pieces.append((section.page_content, {"section": location} if location else {}))
    else:
        pieces.append((path.read_text(encoding="utf-8"), {}))

    chunks = []
    for text, location in pieces:
        for part in _splitter.split_text(text):
            where = location.get("section") or (f"page {location['page']}" if "page" in location else "")
            # Prefix the chunk with where it comes from: it gives the embedding the
            # product context that a bare "Troubleshooting" paragraph lacks.
            header = f"[{title}{' - ' + where if where else ''}]"
            chunks.append(Document(page_content=f"{header}\n{part}",
                                   metadata={"source": rel, "title": title, **location}))
    return chunks


def _delete_source(store: Chroma, rel: str) -> None:
    ids = store.get(where={"source": rel}, include=[])["ids"]
    if ids:
        store.delete(ids=ids)


def ingest(store: Chroma, docs_dir: Path, manifest_path: Path) -> IngestReport:
    report = IngestReport()
    manifest: dict[str, str] = (
        json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    )
    current = {
        p.relative_to(docs_dir).as_posix(): p
        for p in sorted(docs_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    }

    for rel in sorted(set(manifest) - set(current)):
        _delete_source(store, rel)
        del manifest[rel]
        report.removed.append(rel)

    for rel, path in current.items():
        digest = _sha256(path)
        if manifest.get(rel) == digest:
            report.unchanged.append(rel)
            continue
        is_update = rel in manifest
        _delete_source(store, rel)
        chunks = load_chunks(path, rel)
        if chunks:
            store.add_documents(chunks, ids=[f"{rel}::{i}" for i in range(len(chunks))])
        manifest[rel] = digest
        report.chunks_written += len(chunks)
        (report.updated if is_update else report.added).append(rel)
        # Persist after each file so a crash mid-run doesn't redo finished work.
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log.info("Ingest finished: %s", report.as_dict())
    return report
