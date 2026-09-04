# PPG Guru Bot

LINE group chatbot that answers technical questions about paramotor wings
and motors, grounded in the manuals under `manuals/` and in an indexed copy
of the Southwest Airsports website. Both are searched on every question and
a single answer may cite both. Public repo: https://github.com/nayot/ppg-guru-bot.

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
                                                     both collections searched (app/rag.py):
                                                     manuals + website excerpts in ONE call,
                                                     blended, cited inline per claim
                                                     — one retry rung (wider manuals +
                                                       English web terms) if neither answered
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
- `app/rag.py` — retrieval from both collections, the combined prompt,
  source classification, and the OpenRouter call.
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

## Two sources, blended

Both collections are searched on every question and both sets of excerpts
go to the model in one call. Four things here are easy to get wrong:

- **They stay in separate Chroma collections** (`manuals` and `website`,
  both under `data/`), retrieved separately and labelled separately in the
  prompt. That's what keeps a web page from being presented as a
  manufacturer's manual, now that answers are allowed to blend the two.
- **The manual wins any disagreement.** The prompt requires the manual's
  position first, then what the website adds or contradicts — never the
  website silently preferred over the manufacturer.
- **A similarity threshold cannot tell you a source "has nothing."**
  Measured, and it doesn't work: every chunk is paramotor prose, so e5 packs
  the scores into a narrow band. "Clogged muffler on a Top 80" (in no
  manual) scores 0.88 against the manuals while a genuine Hadron 3 take-off
  weight question scores 0.86; "how do I bake sourdough bread" outscores a
  real Thai question. Don't reintroduce a score cutoff. The model reads the
  excerpts and emits the bare sentinel `INSUFFICIENT` instead — a sentinel
  rather than prose because the bot answers in whatever language it was
  asked in, so there is no phrase to match on.
- **The single retry rung carries two fixes; don't drop it to save a call.**
  It widens the manual search to `MANUAL_RETRY_K` (default 25), because spec
  tables rank poorly against natural-language questions and near-identical
  sibling tables crowd each other out — the Hadron 3's own take-off weight
  table sits at rank 20 for a question naming the Hadron 3, below the
  Hadron 4's. And it re-searches the website with English terms the model
  supplies alongside the sentinel, because web retrieval degrades
  cross-lingually far more than the manuals do: the Thai question
  "ท่อไอเสีย Top 80 อุดตันเขม่า" ranks the right page 43rd, its English
  rendering 1st.

- **A website-only answer always triggers a wider manual re-check** before
  it is served. The retry rung above only fires when *neither* source
  answered, which misses the more dangerous case: the website answers well
  while a relevant manual section was never retrieved. "How to fix or
  replace the pull starter of Thor 100" did exactly this — the Thor 100
  manual's §9.4 "Starter Rope Replacement" ranks 18th for that phrasing, so
  the bot claimed the manual didn't cover it and recommended a clone
  starter. Costs one extra call, only on website-only answers.
- **Never let the model assert what a manual lacks.** The prompt forbids
  "the manual does not cover/mention X": excerpts are search results, not
  whole documents, and the section may simply not have been retrieved.

The source header is built in code from the model's own `SOURCES:`
declaration, which is stripped from the reply and cross-checked against it:
website citations must carry a full URL, so the site's domain appearing in
the body proves the website was used whatever the model declared. When
neither source answers, no header is attached — a non-answer isn't
attributed to anyone.

Refreshing the web index is covered below; the manuals are unaffected by it.

## Key behaviors

- Responds only when @mentioned in a group/room; always responds in 1:1 chat
  (`should_respond` in `app/main.py`).
- `ALLOWED_SOURCE_IDS` (env, comma-separated LINE user/group/room IDs) gates
  access. Empty = unrestricted (logs a warning on startup). A non-whitelisted
  group/room that adds the bot gets auto-left (`handle_join`).
- Every answer is prefixed with a source line — manuals, the website
  (explicitly marked "not a manufacturer manual"), or both.
- The system prompt (`app/rag.py`) forbids answering from outside knowledge,
  requires citing every claim inline (manual section, or page title + full
  URL), and restricts LLM output formatting to
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
  retry: a question needing the retry rung makes two calls, so one
  transient blip would otherwise lose the whole answer.
- Never commit `.env` (has live LINE + OpenRouter secrets) or `pdf/` sources.
- `manuals/` (converted manual text) IS committed and public — a known,
  accepted copyright tradeoff, not an oversight. Don't "fix" this by adding
  `.gitignore` entries for it.
- Keep the LLM's allowed output formatting (bold + one table syntax) and
  `richtext.py`'s parsing in sync if either changes — they're coupled. Note
  this is why the web prompt asks for bare URLs as plain text: `richtext.py`
  has no Markdown-link handling, so `[text](url)` would render literally.
- The website may be cited alongside the manuals, but it is not co-equal:
  it is always labelled third-party, and the manual wins any conflict.
