import logging

import chromadb
from chromadb.config import Settings as ChromaSettings

from urllib.parse import urlparse

from app.config import settings
from app.embeddings import embed_query
from app.llm import chat

logger = logging.getLogger("ppg-bot")

# A sentinel the model emits instead of prose, so the retry below can be
# detected exactly in whatever language the pilot happens to be using. It
# carries English search terms with it — see `_parse_retry`.
INSUFFICIENT = "INSUFFICIENT"

# The model declares which sources it actually drew on, so the header can say
# so accurately for a blended answer. Validated in `_classify_sources`.
SOURCES_LINE = "SOURCES:"

PROMPT_HEAD = """\
You are PPG Guru, a technical assistant for paramotor wings and motors, \
answering questions inside a LINE group chat for pilots.

You are given excerpts from TWO sources, clearly separated below:
1. MANUAL EXCERPTS — official manufacturer manuals. Authoritative.
2. WEBSITE EXCERPTS — pages from the {site} website ({base_url}), written \
by an experienced paramotor technician. Useful, practical, and often covers \
hands-on service work the manuals omit — but third-party, not official.

Rules:
- Answer using BOTH sources as needed. Use whichever actually answers the \
question; combining them is expected and good. Do not use outside knowledge \
about specific products, specs, or procedures.
- Where the two disagree, or where the website extends a manufacturer \
procedure, the MANUFACTURER'S MANUAL WINS. Give the manual's position, then \
note what the website adds or contradicts — never silently prefer the \
website over the manual.
- The excerpts are SEARCH RESULTS, not whole documents. Never state or \
imply that a manual "does not cover", "does not mention" or "is silent on" \
something — you cannot see the whole manual, only what the search returned, \
and the procedure may well be in a section you weren't shown. If the manual \
excerpts don't address the point, just answer from the website without \
characterising what the manual contains.
- Cite EVERY factual claim inline, right where you make it:
  * manual  -> (Dudek Hadron 3 (2024), Technical Data)
  * website -> ({site}: Clogged muffler — https://www.example.com/page.htm)
  Write URLs bare, as plain text. Never use Markdown link syntax.
- When a claim comes from the website and concerns maintenance, tuning or \
anything safety-critical, say briefly that it is the site author's guidance \
and that the manufacturer's manual takes precedence.
- Begin your reply with a single line naming the sources you actually drew \
on, exactly one of:
  {sources}: manuals
  {sources}: website
  {sources}: manuals+website
  Then continue with the answer on the following lines. Name a source only \
if you genuinely used it.
- A catalog of all manuals currently indexed (brand/model/year, grouped by \
category) is provided below, separate from the excerpts. Use it ONLY to \
answer questions about what products/manuals you cover (e.g. "list all \
motors", "do you have the X wing"). Never use it as a source of technical \
specs or facts — those must always come from the excerpts.
"""

# First pass: allowed to ask for a wider look rather than answer badly.
_RETRY_RULE = f"""\
- If neither source contains what's needed to answer, do NOT guess and do \
NOT apologise. Reply with exactly one line and nothing else:
{INSUFFICIENT}: <a short ENGLISH search query for what the pilot is asking>
A wider search will then be run automatically using those English terms. \
Write them in English even when the pilot asked in another language, and \
include the product/model name if one was mentioned. A Thai question about \
a soot-clogged Top 80 exhaust, for instance, becomes exactly:
{INSUFFICIENT}: Top 80 clogged muffler cleaning soot
(That is only about the search terms. It says nothing about which language \
to answer in — see the language rule below, which always wins.)
- Partial credit is worse than a wider look: if the excerpts cover the \
topic but not the specific fact asked for, ask for the wider look.
"""

# Final pass: no more rungs left, so explain rather than emit a sentinel.
_FINAL_RULE = """\
- This is the final search. If the excerpts still don't contain the answer, \
say so plainly IN THE PILOT'S OWN LANGUAGE — that neither the indexed \
manuals nor the website covers it — and suggest they check the \
manufacturer's manual or ask a certified instructor. Do not guess, and do \
not emit any sentinel. In that case omit the sources line entirely.
"""

_SHARED_RULES = """\
- For anything safety-critical (rigging, reserve deployment, engine \
maintenance, pre-flight checks, weight limits), be precise and add a brief \
reminder to follow the manufacturer's official procedure / a certified \
instructor before acting.
- Reply in the SAME language the pilot's question is written in — match \
their language, never the language of these instructions or of the \
excerpts. An English question gets an English answer even though the \
manuals and website are English; a Thai question gets a Thai answer.
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

# Prepended in code from the model's own declaration, cross-checked against
# the reply, so a pilot can tell manufacturer documentation from third-party
# web guidance at a glance without reading every inline citation.
MANUAL_SOURCE_HEADER = "**📘 Source: indexed manufacturer manuals**"
WEB_SOURCE_HEADER = "**🌐 Source: {site} website{asof} — not a manufacturer manual**"
BOTH_SOURCE_HEADER = (
    "**📘🌐 Sources: manufacturer manuals + {site} website{asof} (third-party)**"
)


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


def web_indexed_on() -> str:
    """Date of the last web crawl ("" if unknown), for the source header.

    A pilot who follows a cited URL sees the page as it is now, not as it
    was indexed, so the answer says which one it is speaking for. Read from
    a fresh collection handle rather than the cached one, because a refresh
    runs in a separate process and the cached handle's metadata is a
    snapshot from process start.
    """
    try:
        collection = _get_client().get_collection(settings.website_collection)
        return (collection.metadata or {}).get("last_crawl", "")[:10]
    except Exception:
        return ""


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


def _parse_retry(reply: str) -> str | None:
    """English search terms if `reply` asks for a wider look, else None.

    The model has already read the question, so producing these costs no
    extra call, and they are what make website retrieval work
    cross-lingually: the raw Thai question "ท่อไอเสีย Top 80 อุดตันเขม่า"
    ranks the right page 43rd in the web collection, while the model's
    English rendering of it ranks that page 1st.
    """
    head = reply.strip().lstrip("*_`\"' ").strip()
    if not head.upper().startswith(INSUFFICIENT):
        return None
    # The model may dress the sentinel up ("**INSUFFICIENT:**"), so strip
    # punctuation and emphasis from both ends rather than just the colon.
    return head[len(INSUFFICIENT) :].strip(":：-—.*_`\"' \t")


def _split_sources_line(reply: str) -> tuple[str, str]:
    """(declared sources, reply without the declaration line)."""
    first, _, rest = reply.strip().partition("\n")
    if first.strip().lstrip("*_`").upper().startswith(SOURCES_LINE):
        declared = first.split(":", 1)[1].strip().strip("*_`").lower()
        return declared, rest.strip()
    return "", reply.strip()


def _classify_sources(declared: str, body: str) -> str:
    """"manuals" | "website" | "both", from the model's claim plus evidence.

    The declaration is cross-checked against the body: website citations are
    required to carry a full URL, so the site's own domain appearing in the
    answer is hard evidence the website was used, whatever the model said.
    """
    host = urlparse(settings.website_base_url).netloc
    cites_web = bool(host) and host in body
    said_web = "website" in declared
    said_manual = "manual" in declared

    used_web = cites_web or said_web
    used_manual = said_manual or not used_web
    if used_web and used_manual:
        return "both"
    return "website" if used_web else "manuals"


async def _ask(
    query: str,
    manual_query: str,
    web_query: str,
    history: list[dict] | None,
    final: bool,
    manual_k: int | None = None,
) -> str:
    manual_hits = retrieve(manual_query, k=manual_k)
    web_hits = retrieve_website(web_query)

    rule = _FINAL_RULE if final else _RETRY_RULE
    system_prompt = (
        PROMPT_HEAD.format(
            site=settings.website_name,
            base_url=settings.website_base_url,
            sources=SOURCES_LINE.rstrip(":"),
        )
        + rule
        + _SHARED_RULES
        + "\n"
    )
    manual_context = build_context(manual_hits) if manual_hits else "(none)"
    web_context = build_web_context(web_hits) if web_hits else "(none)"
    user_prompt = (
        f"Catalog of indexed manuals:\n\n{list_catalog()}\n\n"
        f"=== MANUAL EXCERPTS (authoritative) ===\n\n{manual_context}\n\n"
        f"=== WEBSITE EXCERPTS ({settings.website_name}, third-party) ===\n\n"
        f"{web_context}\n\n"
        f"=== END OF EXCERPTS ===\n\nPilot's question: {query}"
    )
    return await chat(system_prompt, user_prompt, history=history)


async def answer_with_source(
    query: str, history: list[dict] | None = None
) -> tuple[str, str]:
    """Answer the question from both sources, and say which ones were used.

    Returns (reply, source) where source is "manuals", "website", "both" or
    "none".

    Both collections are searched on every question and both sets of
    excerpts go to the model together, which is free to blend them; the
    prompt makes the manufacturer's manual win any disagreement. There is
    one retry rung, and it exists for two measured reasons:

    * Spec tables rank poorly against natural-language questions and
      near-identical tables from sibling models crowd each other out — the
      Hadron 3's own take-off weight table sits at rank 20 for a question
      naming the Hadron 3, below the Hadron 4's. The retry widens the
      manual search to `MANUAL_RETRY_K`.
    * Website retrieval degrades cross-lingually far more than the manuals
      do. The retry re-searches it with English terms the model supplies,
      which is worth a rank-43 to rank-1 swing on a Thai question.

    A question the excerpts answer on the first pass still costs exactly one
    LLM call.
    """
    expanded = _expand_query(query, history)

    reply = await _ask(query, expanded, expanded, history, final=False)
    terms = _parse_retry(reply)
    if terms is None:
        declared, body = _split_sources_line(reply)
        source = _classify_sources(declared, body)
        if source != "website":
            return body, source
        # An answer built only from the website is the case where a missed
        # manual chunk does the most damage — it sends a pilot to third-party
        # guidance for something the manufacturer documents. That is not
        # hypothetical: "how to fix or replace the pull starter of Thor 100"
        # is answered by the website, while the Thor 100 manual's own
        # "Starter Rope Replacement" section ranks 18th and never made the
        # first pass. So widen the manual search and ask again before
        # serving it.
        logger.info("Website-only answer — re-checking manuals top-%d", settings.manual_retry_k)
        wider = await _ask(
            query, expanded, expanded, history, final=True,
            manual_k=settings.manual_retry_k,
        )
        declared, body = _split_sources_line(wider)
        return body, _classify_sources(declared, body)

    logger.info("First pass insufficient — retrying wider, searching %r", terms)
    reply = await _ask(
        query,
        expanded,
        terms or expanded,
        history,
        final=True,
        manual_k=settings.manual_retry_k,
    )
    declared, body = _split_sources_line(reply)
    if not declared and _looks_unanswered(body):
        return body, "none"
    return body, _classify_sources(declared, body)


def _looks_unanswered(body: str) -> bool:
    """Whether the final pass gave up rather than answered.

    The final rule tells the model to omit the sources line when it can't
    answer, so a missing declaration on the last rung is the signal. It is
    only ever consulted on that rung, where the alternative is mislabelling
    a non-answer with a source it never used.
    """
    host = urlparse(settings.website_base_url).netloc
    return not (host and host in body)


def with_source_header(reply: str, source: str) -> str:
    indexed_on = web_indexed_on()
    asof = f", indexed {indexed_on}" if indexed_on else ""
    if source == "manuals":
        return f"{MANUAL_SOURCE_HEADER}\n{reply}"
    if source == "website":
        header = WEB_SOURCE_HEADER.format(site=settings.website_name, asof=asof)
        return f"{header}\n{reply}"
    if source == "both":
        header = BOTH_SOURCE_HEADER.format(site=settings.website_name, asof=asof)
        return f"{header}\n{reply}"
    return reply


async def answer(query: str, history: list[dict] | None = None) -> str:
    reply, source = await answer_with_source(query, history=history)
    return with_source_header(reply, source)
