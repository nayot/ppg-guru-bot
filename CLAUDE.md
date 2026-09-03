# PPG Guru Bot

LINE group chatbot that answers technical questions about paramotor wings
and motors, grounded only in the manuals under `manuals/`. Public repo:
https://github.com/nayot/ppg-guru-bot.

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
- `app/rag.py` — retrieval + system prompt + OpenRouter call.
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

## Key behaviors

- Responds only when @mentioned in a group/room; always responds in 1:1 chat
  (`should_respond` in `app/main.py`).
- `ALLOWED_SOURCE_IDS` (env, comma-separated LINE user/group/room IDs) gates
  access. Empty = unrestricted (logs a warning on startup). A non-whitelisted
  group/room that adds the bot gets auto-left (`handle_join`).
- The system prompt (`app/rag.py`) forbids answering from outside knowledge,
  requires citing the manual used, and restricts LLM output formatting to
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
Vittorazi Atom 80, Vittorazi Moster 185 Plus) are both populated. To add
another category, brand, or model:

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
- Never commit `.env` (has live LINE + OpenRouter secrets) or `pdf/` sources.
- `manuals/` (converted manual text) IS committed and public — a known,
  accepted copyright tradeoff, not an oversight. Don't "fix" this by adding
  `.gitignore` entries for it.
- Keep the LLM's allowed output formatting (bold + one table syntax) and
  `richtext.py`'s parsing in sync if either changes — they're coupled.
