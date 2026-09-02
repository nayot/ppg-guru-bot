"""Build/rebuild the Chroma vector store from manuals/<category>/<brand>/<model>/<year>/manual.md"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import settings
from app.embeddings import embed_documents

HEADER_RE = re.compile(r"^(#{1,3})\s+(.*)$")
MAX_CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200


@dataclass
class Chunk:
    text: str
    heading_path: str
    metadata: dict = field(default_factory=dict)


def split_markdown(text: str) -> list[tuple[str, str]]:
    """Split into (heading_path, section_text) by headers up to depth 3."""
    sections: list[tuple[str, str]] = []
    stack: list[str] = []
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append((" > ".join(stack) if stack else "(intro)", body))
        buf.clear()

    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            flush()
            depth = len(m.group(1))
            title = m.group(2).strip()
            stack = stack[: depth - 1] + [title]
        else:
            buf.append(line)
    flush()
    return sections


def chunk_section(heading_path: str, body: str) -> list[str]:
    if len(body) <= MAX_CHUNK_CHARS:
        return [body]
    chunks = []
    start = 0
    while start < len(body):
        end = min(start + MAX_CHUNK_CHARS, len(body))
        chunks.append(body[start:end])
        if end == len(body):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def parse_path_metadata(md_path: Path, manuals_root: Path) -> dict:
    # manuals/<category>/<brand>/<model>/<year>/manual.md
    rel = md_path.relative_to(manuals_root)
    parts = rel.parts
    meta = {"source_file": str(rel)}
    if len(parts) >= 4:
        meta["category"] = parts[0]
        meta["brand"] = parts[1]
        meta["model"] = parts[2]
        meta["year"] = parts[3]
    return meta


def load_chunks(manuals_root: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for md_path in sorted(manuals_root.rglob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        base_meta = parse_path_metadata(md_path, manuals_root)
        for heading_path, body in split_markdown(text):
            for piece in chunk_section(heading_path, body):
                chunks.append(
                    Chunk(
                        text=piece,
                        heading_path=heading_path,
                        metadata={**base_meta, "section": heading_path},
                    )
                )
    return chunks


def build_index(rebuild: bool = False) -> int:
    manuals_root = Path(settings.manuals_dir)
    client = chromadb.PersistentClient(
        path=settings.data_dir,
        settings=ChromaSettings(anonymized_telemetry=False),
    )

    if rebuild:
        try:
            client.delete_collection("manuals")
        except Exception:
            pass

    collection = client.get_or_create_collection("manuals")

    if not rebuild and collection.count() > 0:
        return collection.count()

    chunks = load_chunks(manuals_root)
    if not chunks:
        print(f"WARNING: no .md files found under {manuals_root}", file=sys.stderr)
        return 0

    texts = [c.text for c in chunks]
    embeddings = embed_documents(texts)
    ids = [f"{c.metadata.get('source_file', 'unknown')}::{i}" for i, c in enumerate(chunks)]

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=[c.metadata for c in chunks],
    )
    return len(chunks)


if __name__ == "__main__":
    rebuild = "--rebuild" in sys.argv
    n = build_index(rebuild=rebuild)
    print(f"Indexed {n} chunks into {settings.data_dir}")
