import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import settings
from app.embeddings import embed_query
from app.llm import chat

SYSTEM_PROMPT = """\
You are PPG Guru, a technical assistant for paramotor wings and motors, \
answering questions inside a LINE group chat for pilots.

Rules:
- Answer ONLY using the excerpts provided below. Do not use outside knowledge \
about specific products, specs, or procedures.
- If the excerpts don't contain the answer, say so plainly and suggest the \
pilot check the manufacturer's manual or ask a certified instructor. Do not guess.
- Always cite which manual(s) you used, e.g. "(Dudek Hadron 3, section 7.2)".
- For anything safety-critical (rigging, reserve deployment, engine \
maintenance, pre-flight checks, weight limits), be precise and add a brief \
reminder to follow the manufacturer's official procedure / a certified \
instructor before acting.
- Reply in the SAME language the user asked the question in.
- Earlier turns of this conversation may appear above the current question. \
Use them only to understand context (e.g. what "it" or "that wing" refers \
to) — never as a source of facts. Facts must always come from the manual \
excerpts provided below for THIS question.
- Keep answers concise and practical; this is a chat message, not a document.
- Formatting: use **bold** for key specs/values, and a Markdown table \
(header row, `---` separator row, data rows, all using `|`) when comparing \
several values (e.g. multiple sizes/models). Don't use other Markdown \
syntax (no headings, links, or code blocks) — only bold and tables render \
specially in this chat; everything else is shown as plain text.
"""

_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(
            path=settings.data_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        _collection = _client.get_or_create_collection("manuals")
    return _collection


def retrieve(query: str, k: int | None = None) -> list[dict]:
    collection = _get_collection()
    if collection.count() == 0:
        return []
    k = k or settings.top_k
    result = collection.query(query_embeddings=[embed_query(query)], n_results=k)
    hits = []
    for doc, meta in zip(result["documents"][0], result["metadatas"][0]):
        hits.append({"text": doc, "meta": meta})
    return hits


def build_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        m = h["meta"]
        label = f"{m.get('brand', '?')} {m.get('model', '?')} ({m.get('year', '?')}) - {m.get('section', '')}"
        blocks.append(f"[{label}]\n{h['text']}")
    return "\n\n---\n\n".join(blocks)


def _expand_query(query: str, history: list[dict] | None) -> str:
    """Fold the pilot's last question into the retrieval query.

    Retrieval only ever sees the current question's text. A follow-up like
    "what about its max wing loading?" has no wing name in it, so on its
    own it retrieves nothing useful — prepending the last thing they asked
    gives the embedding something concrete to match against.
    """
    if not history:
        return query
    last_user = next(
        (m["content"] for m in reversed(history) if m["role"] == "user"), None
    )
    return f"{last_user} {query}" if last_user else query


async def answer(query: str, history: list[dict] | None = None) -> str:
    hits = retrieve(_expand_query(query, history))
    if not hits:
        context = "(no manuals indexed yet)"
    else:
        context = build_context(hits)

    user_prompt = f"Manual excerpts:\n\n{context}\n\n---\n\nPilot's question: {query}"
    return await chat(SYSTEM_PROMPT, user_prompt, history=history)
