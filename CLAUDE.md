# PPG Guru Bot

LINE group chatbot that answers technical questions about paramotor wings
and motors, grounded in the manuals under `manuals/` first and, only when
those don't cover the question, in an indexed copy of the Southwest
Airsports website. Public repo: https://github.com/nayot/ppg-guru-bot.

**Note on history:** this project was originally planned as a Hermes Agent
profile (`ppg_guru_bot`, see `~/.hermes/profiles/ppg_guru_bot`) with a
17-task rollout plan. That plan was abandoned before deployment — Nayot
decided not to route this through Hermes at all — and the bot here was
built from scratch in a single session as a plain, self-contained service
instead. It's much simpler than that plan assumed: no gateway/systemd
layer, no bundled LINE adapter, no catalog.yaml, no retrieval skill layer.
If you find references to the Hermes plan elsewhere (e.g. an Obsidian
note), they're historical/superseded — this repo is the actual design.

## Architecture

```
LINE group ─▶ LINE Messaging API ─▶ NGINX (eng-ai.buu.ac.th) ─▶ FastAPI webhook (app/main.py)
                                                                       │
                                                     mention-only filter (groups/rooms)
                                                     access whitelist (ALLOWED_SOURCE_IDS)
                                                                       │
                                                     RAG: Chroma + multilingual-e5-small
                                                     embeddings (app/rag.py, app/embeddings.py)
                                                     — Thai query → English manual passage
                                                       retrieval works cross-lingually
                                                                       │
                                                     source ladder (app/rag.py):
                                                     manuals top-k → manuals wide retry →
                                                     Southwest Airsports website collection
                                                     — the model, not a score threshold,
                                                       decides when to move down a rung
                                                                       │
                                                     OpenRouter chat completion (app/llm.py)
                                                     — model is a config value, swappable
                                                       via .env without a code change
                                                                       │
                                                     Markdown → Flex Message (app/richtext.py)
                                                     — bold/tables render properly in LINE
                                                                       │
                                                     LINE reply, via a FastAPI background task
                                                     (webhook acks immediately — avoids the
                                                     reply-token timeout/retry storm)
```

- `app/main.py` — FastAPI webhook: signature verification, mention gating,
  access whitelist, join/follow handling, background-task dispatch.
- `app/rag.py` — retrieval, the manuals-first source ladder, system
  prompts, and the OpenRouter call.
- `app/web_ingest.py` — crawls the site in `WEBSITE_BASE_URL` from its
  `sitemap.xml` and indexes the HTML pages into the separate `website`
  Chroma collection. Run explicitly; never on startup.
- `app/ingest.py` — walks `manuals/<category>/<brand>/<model>/<year>/manual.md`,
  splits by Markdown headers (depth ≤3), chunks (1500 chars, 200 overlap),
  embeds, and writes to the Chroma collection `manuals` under `data/`.
- `app/embeddings.py` — `sentence-transformers` wrapper for
  `intfloat/multilingual-e5-small`.
- `app/memory.py` — rolling per-*user* conversation history (last
  `MEMORY_MAX_MESSAGES`, default 20), in-process only (lost on restart).
  Keyed by LINE `user_id`, deliberately not by group/room id — the bot
  runs in a shared group, so memory must stay per-pilot or two people's
  threads would interleave into one shared context.
- `app/richtext.py` — converts the LLM's constrained Markdown (bold + one
  table syntax only, enforced by the system prompt) into a LINE Flex Message.
- `scripts/ask.py` — CLI to test the RAG/LLM pipeline directly, bypassing LINE.

## Conversation memory

Each pilot's last `MEMORY_MAX_MESSAGES` messages (default 20) are kept
in-process (`app/memory.py`) and passed to the LLM as prior turns, so
follow-ups work (e.g. "what about its minimum weight?" after asking about
a specific wing). Two things to keep in mind if you touch this:

- **Keyed by `user_id`, not by group/room id.** `get_user_id()` in
  `app/main.py` extracts the sender's own id even inside a group message
  (distinct from `get_source_id()`, which returns the shared group/room id
  used for the access whitelist). Keying by group would merge every
  member's questions into one shared thread — wrong, since the bot is
  meant for group chat with multiple pilots.
- **Retrieval also needs the last question, not just the LLM.** `_expand_query()`
  in `app/rag.py` prepends the pilot's previous question to the current one
  before embedding, because the vector search only ever sees the current
  question's raw text — a pronoun-only follow-up ("its minimum weight?")
  wouldn't otherwise retrieve the right manual on its own.
- Memory resets on container restart (no volume/persistence) — an accepted
  tradeoff for a lightweight continuity feature, not a durable chat log.

## Two sources, and the ladder between them

The manuals are primary; the website is a fallback. Three things about this
are easy to get wrong if you touch it:

- **The two live in separate Chroma collections** (`manuals` and `website`,
  both under `data/`), never mixed into one. That's what makes it
  structurally impossible for a web page to be retrieved and cited as if it
  were a manufacturer's manual.
- **A similarity threshold cannot decide "the manuals don't cover this."**
  It was measured and it doesn't work: every chunk in the index is
  paramotor prose, so e5 packs the scores into a narrow band. "Clogged
  muffler on a Top 80" (in no manual) scores 0.88 against the manuals while
  a genuine Hadron 3 take-off weight question scores 0.86; "how do I bake
  sourdough bread" outscores a real Thai question. Don't reintroduce a
  score cutoff here. Instead the LLM reads the excerpts and replies with the
  bare sentinel `NEED_WEB` when they don't answer — a sentinel rather than
  prose because the bot answers in whatever language it was asked in, so
  there is no phrase to match on.
- **There's a wider second pass over the manuals before the website is
  consulted** (`MANUAL_RETRY_K`, default 25). It exists because spec tables
  are dense numeric rows that rank poorly against natural-language
  questions, and near-identical tables from sibling models crowd each other
  out — the Hadron 3's own take-off weight table sits at rank 20 for a
  question naming the Hadron 3, below the Hadron 4's. Without this rung the
  bot cites a third-party website for figures printed in the manufacturer's
  own manual. Don't drop it to save a call.

Both source headers (`MANUAL_SOURCE_HEADER` / `WEB_SOURCE_HEADER` in
`app/rag.py`) are prepended **in code**, not requested from the model, so
the source label can't be forgotten or hallucinated. When neither source
answers, no header is attached — a non-answer is not attributed to anyone.

The web index is refreshed by cron, not on startup (a few hundred outbound
requests per restart would be wrong); `app/main.py` only logs a warning when
it's missing, and with no web index the bot degrades to manuals-only.

`scripts/refresh-website.sh` runs nightly and does an **incremental**
refresh: the sitemap's per-URL `lastmod` is stored on every chunk, so only
changed pages are re-fetched (a no-change night is one sitemap fetch, ~6s,
versus ~4min for a full crawl), URLs that left the sitemap are dropped, and
navigation stubs with no prose are remembered in the collection's own
metadata so they aren't re-fetched nightly. `--rebuild` still forces a full
wipe and re-crawl; `--dry-run` reports the plan without fetching.

The script restarts the container only when something changed — the server
holds Chroma open from process start, so the restart is what makes it serve
new content, and it also wipes conversation memory, so it shouldn't happen
on quiet nights. If you change the ingest metadata shape, run `--rebuild`
once: chunks written by an older version have no `lastmod` and will look
changed on every incremental run until they're rewritten.

The crawl date lives in the collection's `last_crawl` metadata and is shown
in the web source header ("indexed 2026-09-04"). `web_indexed_on()` reads it
from a *fresh* collection handle on purpose — the refresh runs in a separate
process, so the cached handle's metadata is a snapshot from process start.

## Key behaviors

- Responds only when @mentioned in a group/room; always responds in 1:1 chat
  (`should_respond` in `app/main.py`).
- `ALLOWED_SOURCE_IDS` (env, comma-separated LINE user/group/room IDs) gates
  access. Empty = unrestricted (logs a warning on startup). A non-whitelisted
  group/room that adds the bot gets auto-left (`handle_join`).
- Every answer is prefixed with a source line — indexed manufacturer
  manuals, or the website (explicitly marked "not a manufacturer manual").
- The system prompts (`app/rag.py`) forbid answering from outside knowledge,
  require citing the manual used (or the page title + full URL on the web
  path), and restrict LLM output formatting to
  **bold** and Markdown tables only — `richtext.py` only knows how to render
  those two constructs specially.
- Vector index (`data/`, a Docker volume) is built automatically on first
  startup only if empty. Adding/changing manuals requires an explicit
  `docker compose exec ppg-bot python -m app.ingest --rebuild` — it does not
  happen automatically, and `data/` is local per host (not synced by git).

## Adding manuals

`manuals/wings/...` (Dudek Hadron 3/4 & Nucleon 4, Flow Cosmos Power 2,
ITV Piper 2, MacPara Colorado 2) and `manuals/motors/...` (Ciscomotors
C-Max, PAP Top 80, Polini Thor 100/Thor 200, Simonini Mini 2 Plus,
Sky Engines SKY 110S/SKY 150, Vittorazi Atom 80, Vittorazi Moster 185
Plus) are both populated. To add another category, brand, or model:

1. Source PDF → `pdf/`, convert to Markdown (e.g. `pdf-to-markdown` skill),
   save as `manuals/<wings|motors>/<Brand>/<Model>/<Year>/manual.md`. The
   path components are read as metadata and shown in citations — keep them
   accurate/consistent. `Year` must be a real edition/print/revision year
   found in the manual's own text or trustworthy file metadata — use the
   literal folder name `undated` instead of guessing when neither exists.
   (Two of the motor manuals hit this: `Polini/Thor-100/undated` and
   `Simonini/Mini-2-Plus/undated` were both sourced from a manualslib.com
   PDF export whose only embedded date was when that export was rendered,
   not any real manufacturer edition date — not usable evidence.)
   `Sky-Engines/Sky-110S/undated` is a third, same reason and verified
   directly: that PDF's own metadata names its manualslib.com source page
   and a wkhtmltopdf render date, and the manual text carries no date.
   Conversely, `Sky-Engines/Sky-150/2021` is the one folder dated purely
   from file metadata: that manual's text carries no date at all, but the
   PDF is a single-pass Microsoft Word 2019 export authored 2021-02-03 by
   the manufacturer itself, which is the trustworthy-metadata case the rule
   allows. The `manual.md` provenance line says so explicitly, since the
   citation asserts a year the manual never prints.
2. `docker compose exec ppg-bot python -m app.ingest --rebuild` on whichever
   host serves traffic.
3. Sanity-check with `scripts/ask.py --show-retrieval "<question>"` before
   trusting it in the group.

No code changes are needed — `ingest.py` walks all of `manuals/` regardless
of category name.

## Local dev vs. production

This dev machine isn't publicly reachable, so real LINE end-to-end testing
only happens on the production host (`eng-ai.buu.ac.th`) or via a temporary
tunnel. Locally, test the RAG+LLM pipeline directly with `scripts/ask.py`,
bypassing LINE entirely (see README.md for exact commands).

Production binds to `127.0.0.1:8801` only; NGINX terminates TLS and proxies
`/line/webhook` — the proxy config must not rewrite/buffer the body, since
LINE signs the raw request body. Full deploy steps are in `README.md`.

## Conventions

- Model selection lives entirely in `.env` (`OPENROUTER_MODEL`) — never
  hardcode a model slug in code.
- `app/llm.py` retries 429/5xx with backoff + jitter and honours
  `Retry-After`; auth/quota/malformed errors are raised immediately. A 429
  from OpenRouter is normally the *provider* throttling (it forwards their
  error verbatim), not an account problem — some models, including
  `qwen/qwen3.7-flash`, have a single provider and so no failover. Keep the
  retry: the answer ladder makes up to three calls per question, so one
  transient blip would otherwise lose the whole answer.
- Never commit `.env` (has live LINE + OpenRouter secrets) or `pdf/` sources.
- `manuals/` (converted manual text) IS committed and public — a known,
  accepted copyright tradeoff, not an oversight. Don't "fix" this by adding
  `.gitignore` entries for it.
- Keep the LLM's allowed output formatting (bold + one table syntax) and
  `richtext.py`'s parsing in sync if either changes — they're coupled. Note
  this is why the web prompt asks for bare URLs as plain text: `richtext.py`
  has no Markdown-link handling, so `[text](url)` would render literally.
- Never let the website become a co-equal source of specs. It's a fallback,
  it is labelled as third-party, and the manuals are always tried first.
