import logging

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import settings
from app.embeddings import embed_query
from app.llm import chat

logger = logging.getLogger("ppg-bot")

# Sentinels the model emits instead of prose so the two-stage handoff below
# can be detected exactly, in any language the pilot happens to be using.
NEED_WEB = "NEED_WEB"
NOT_FOUND = "NOT_FOUND"

_SHARED_RULES = """\
- For anything safety-critical (rigging, reserve deployment, engine \
maintenance, pre-flight checks, weight limits), be precise and add a brief \
reminder to follow the manufacturer's official procedure / a certified \
instructor before acting.
- Reply in the SAME language the user asked the question in.
- Earlier turns of this conversation may appear above the current question. \
Use them only to understand context (e.g. what "it" or "that wing" refers \
to) — never as a source of facts. Facts must always come from the excerpts \
provided below for THIS question.
- Keep answers concise and practical; this is a chat message, not a document.
- Formatting: use **bold** for key specs/values, and a Markdown table \
(header row, `---` separator row, data rows, all using `|`) when comparing \
several values (e.g. multiple sizes/models). Don't use other Markdown \
syntax (no headings, links, or code blocks) — only bold and tables render \
specially in this chat; everything else is shown as plain text.\
"""

MANUAL_PROMPT_HEAD = """\
You are PPG Guru, a technical assistant for paramotor wings and motors, \
answering questions inside a LINE group chat for pilots.

Rules:
- Answer ONLY using the manual excerpts provided below. Do not use outside \
knowledge about specific products, specs, or procedures.
- Always cite which manual(s) you used, e.g. "(Dudek Hadron 3, section 7.2)".
- A catalog of all manuals currently indexed (brand/model/year, grouped by \
category) is provided below, separate from the excerpts. Use it ONLY to \
answer questions about what products/manuals you cover (e.g. "list all \
motors", "do you have the X wing"). Never use it as a source of technical \
specs or facts — those must always come from the excerpts.
"""

# Used when there is no web index to fall back to: behave as before and just
# tell the pilot the manuals don't cover it.
_MANUAL_DEAD_END_RULE = """\
- If the excerpts don't contain the answer, say so plainly and suggest the \
pilot check the manufacturer's manual or ask a certified instructor. Do not \
guess.
"""

# Used when a web index exists. The manuals stay the primary source; this
# stage's only job when they fall short is to hand off, not to improvise.
_MANUAL_ESCALATE_RULE = f"""\
- If the manual excerpts (or, for a coverage question, the catalog) do not \
contain what's needed to answer, do NOT guess and do NOT apologise. Reply \
with exactly one line and nothing else:
{NEED_WEB}: <a short ENGLISH search query for what the pilot is asking>
A second source will then be searched automatically using those English \
terms. Write them in English even when the pilot asked in another \
language, and include the product/model name if one was mentioned — e.g. \
for "ท่อไอเสีย Top 80 อุดตันเขม่า ทำความสะอาดอย่างไร" reply exactly:
{NEED_WEB}: Top 80 clogged muffler cleaning soot
- Only answer from the excerpts. Partial credit is worse than a handoff: if \
they cover the topic but not the specific fact asked for, hand off.
"""

WEB_PROMPT = f"""\
You are PPG Guru, a technical assistant for paramotor wings and motors, \
answering questions inside a LINE group chat for pilots.

The indexed manufacturer manuals did not answer this question, so the \
excerpts below come from the {{site}} website ({{base_url}}) instead.

Rules:
- Answer ONLY using the website excerpts provided below. Do not use outside \
knowledge about specific products, specs, or procedures.
- Cite the specific page you used by its title AND its full URL, written as \
plain text (no Markdown link syntax), e.g. "Cylinder head temperature gauge \
— https://example.com/page.htm".
- This is a third-party website, not a manufacturer's manual. Where its \
advice concerns maintenance, tuning, or anything safety-critical, say \
briefly that it is the site author's guidance and that the manufacturer's \
manual takes precedence.
- If the excerpts below do not answer the question either, put \
{NOT_FOUND} alone on the first line. Then, on the following lines and \
WRITTEN IN THE PILOT'S OWN LANGUAGE (not English, unless they asked in \
English), tell them plainly that neither the indexed manuals nor the \
website covers this, and suggest they check the manufacturer's manual or \
ask a certified instructor. Do not copy this instruction's English wording.
{_SHARED_RULES}
"""

# Prepended in code rather than left to the model, so the pilot can always
# tell a manufacturer's manual from a third-party website at a glance.
MANUAL_SOURCE_HEADER = "**📘 Source: indexed manufacturer manuals**"
WEB_SOURCE_HEADER = "**🌐 Source: {site} website — not a manufacturer manual**"

_client = None
_collections: dict[str, object] = {}


def _get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(
            path=settings.data_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    return _client


def _get_collection(name: str = "manuals"):
    if name not in _collections:
        _collections[name] = _get_client().get_or_create_collection(name)
    return _collections[name]


def _query(collection, query: str, k: int) -> list[dict]:
    if collection.count() == 0:
        return []
    result = collection.query(query_embeddings=[embed_query(query)], n_results=k)
    return [
        {"text": doc, "meta": meta}
        for doc, meta in zip(result["documents"][0], result["metadatas"][0])
    ]


def retrieve(query: str, k: int | None = None) -> list[dict]:
    return _query(_get_collection(), query, k or settings.top_k)


def retrieve_website(query: str, k: int | None = None) -> list[dict]:
    if not settings.website_enabled:
        return []
    collection = _get_collection(settings.website_collection)
    return _query(collection, query, k or settings.website_top_k)


def web_index_size() -> int:
    """Chunk count in the fallback web collection (0 if never crawled)."""
    if not settings.website_enabled:
        return 0
    try:
        return _get_collection(settings.website_collection).count()
    except Exception:
        return 0


def list_catalog() -> str:
    """Every (category, brand, model, year) currently indexed, grouped by category.

    Vector search only ever returns the top-k semantically nearest chunks, so
    it can't answer "list all X" / "what do you cover" questions on its own —
    those need the full set, not a similarity match. This reads it straight
    from the collection's metadata instead of searching.
    """
    collection = _get_collection()
    if collection.count() == 0:
        return "(no manuals indexed yet)"
    got = collection.get(include=["metadatas"])
    entries = {
        (m.get("category"), m.get("brand"), m.get("model"), m.get("year"))
        for m in got["metadatas"]
    }
    by_category: dict[str, list[str]] = {}
    for category, brand, model, year in entries:
        by_category.setdefault(category or "?", []).append(f"{brand} {model} ({year})")
    lines = [
        f"{category}: {', '.join(sorted(items))}"
        for category, items in sorted(by_category.items())
    ]
    return "\n".join(lines)


def build_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        m = h["meta"]
        label = f"{m.get('brand', '?')} {m.get('model', '?')} ({m.get('year', '?')}) - {m.get('section', '')}"
        blocks.append(f"[{label}]\n{h['text']}")
    return "\n\n---\n\n".join(blocks)


def build_web_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        m = h["meta"]
        label = f"{m.get('title', '?')} — {m.get('url', '?')}"
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


def _parse_escalation(reply: str) -> str | None:
    """English search terms if `reply` is a NEED_WEB handoff, else None.

    Stage one is asked to append English search terms to the sentinel. It
    has already read the question, so this costs no extra call, and it is
    what makes the web fallback work cross-lingually: the raw Thai question
    "ท่อไอเสีย Top 80 อุดตันเขม่า ทำความสะอาดอย่างไร" ranks the right page
    43rd in the web collection, while the model's English rendering of the
    same question ranks it 1st. The manuals don't need this — they're
    chunked small and the e5 model handles them cross-lingually — but the
    web collection is twice the size and far more topically crowded.

    Returns "" for a bare sentinel with no terms, which the caller treats as
    "escalate, but search with the original question".
    """
    head = reply.strip().lstrip("*_`\"' ").strip()
    if not head.upper().startswith(NEED_WEB):
        return None
    return head[len(NEED_WEB) :].lstrip(":：-—. ").strip().strip("*_`\"'")


def _strip_sentinel(reply: str, sentinel: str) -> str:
    first, _, rest = reply.partition("\n")
    if sentinel in first.upper():
        return rest.strip() or first.strip()
    return reply.strip()


async def _answer_from_manuals(
    query: str,
    expanded: str,
    history: list[dict] | None,
    can_escalate: bool,
    k: int | None = None,
) -> str:
    hits = retrieve(expanded, k=k)
    context = build_context(hits) if hits else "(no manuals indexed yet)"
    rule = _MANUAL_ESCALATE_RULE if can_escalate else _MANUAL_DEAD_END_RULE
    system_prompt = f"{MANUAL_PROMPT_HEAD}{rule}{_SHARED_RULES}\n"
    user_prompt = (
        f"Catalog of indexed manuals:\n\n{list_catalog()}\n\n---\n\n"
        f"Manual excerpts:\n\n{context}\n\n---\n\nPilot's question: {query}"
    )
    return await chat(system_prompt, user_prompt, history=history)


async def _answer_from_website(
    query: str, search_query: str, history: list[dict] | None
) -> tuple[str, str] | None:
    """(reply, source) from the web index, or None if it has nothing to offer.

    `search_query` is the English rendering stage one produced, not the
    pilot's raw words — see `_parse_escalation`.
    """
    hits = retrieve_website(search_query)
    if not hits:
        return None
    system_prompt = WEB_PROMPT.format(
        site=settings.website_name, base_url=settings.website_base_url
    )
    user_prompt = (
        f"Website excerpts:\n\n{build_web_context(hits)}\n\n---\n\n"
        f"Pilot's question: {query}"
    )
    reply = await chat(system_prompt, user_prompt, history=history)
    if NOT_FOUND in reply.split("\n", 1)[0].upper():
        return _strip_sentinel(reply, NOT_FOUND), "none"
    return reply, "website"


async def answer_with_source(
    query: str, history: list[dict] | None = None
) -> tuple[str, str]:
    """Answer the question, preferring the manuals, and say which source won.

    Returns (reply, source) where source is "manuals", "website" or "none".

    The manuals are always tried first and the website is only consulted if
    they come up short. Deciding "come up short" is the subtle part: it is
    the *model* that decides, not a similarity threshold. Embedding distance
    turns out to be useless for this — an off-manual question like "clogged
    muffler on a Top 80" scores higher against the manuals than a genuine
    on-manual one, because every chunk in the index is paramotor prose and
    e5 packs it all into a narrow band. Whether the excerpts actually
    contain the answer is a reading-comprehension question, so we ask the
    reader: stage one replies with the NEED_WEB sentinel (plus English
    search terms) instead of an answer.

    A question the manuals answer still costs exactly one LLM call. Only
    the fallback path pays for the wider manual retry and the web call.
    """
    expanded = _expand_query(query, history)
    can_escalate = web_index_size() > 0

    reply = await _answer_from_manuals(query, expanded, history, can_escalate)
    terms = _parse_escalation(reply) if can_escalate else None
    if terms is None:
        return reply, "manuals"

    # Second look at the manuals with a much wider net before conceding.
    # Spec tables are dense rows of numbers that rank poorly against a
    # natural-language question, and near-identical tables from sibling
    # models crowd each other out: the Hadron 3's own take-off weight table
    # sits at rank 20 for "what is the max takeoff weight of the Hadron 3",
    # below the Hadron 4's. Escalating on that first miss would send the
    # pilot to a third-party website for a figure printed in the
    # manufacturer's own manual, so it is worth one wider pass first.
    logger.info("Manuals top-%d insufficient — retrying wider", settings.top_k)
    reply = await _answer_from_manuals(
        query, expanded, history, can_escalate, k=settings.manual_retry_k
    )
    retry_terms = _parse_escalation(reply)
    if retry_terms is None:
        return reply, "manuals"

    web_query = retry_terms or terms or expanded
    logger.info(
        "Manuals insufficient — falling back to %s, searching %r",
        settings.website_name,
        web_query,
    )
    web = await _answer_from_website(query, web_query, history)
    if web is None:
        # The web collection emptied out between the check and the query.
        return await _answer_from_manuals(query, expanded, history, False), "manuals"
    return web


def with_source_header(reply: str, source: str) -> str:
    if source == "manuals":
        return f"{MANUAL_SOURCE_HEADER}\n{reply}"
    if source == "website":
        header = WEB_SOURCE_HEADER.format(site=settings.website_name)
        return f"{header}\n{reply}"
    return reply


async def answer(query: str, history: list[dict] | None = None) -> str:
    reply, source = await answer_with_source(query, history=history)
    return with_source_header(reply, source)
