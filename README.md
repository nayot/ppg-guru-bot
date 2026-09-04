# PPG Guru Bot

LINE group chatbot that answers technical questions about paramotor wings
and motors, grounded in the manuals under `manuals/`, with the Southwest
Airsports website as a clearly-labelled secondary source.

- Responds only when @mentioned in a group/room; always responds in 1:1 chat.
- **Two grounded sources, cited per claim.** Every question searches both
  the indexed manufacturer manuals and an indexed copy of
  https://www.southwestairsports.com/, and a single answer may draw on both.
  Each claim is cited inline to the manual section or website page it came
  from, and the reply is prefixed with which sources were used, so a pilot
  can tell manufacturer documentation from third-party web guidance — see
  "The two sources" below.
- Retrieval uses a multilingual embedding model, so a Thai question can
  still retrieve the right (English) manual passage; the LLM is instructed
  to reply in whatever language the question was asked in.
- LLM calls go through OpenRouter — the model is a config value
  (`OPENROUTER_MODEL` in `.env`), not hardcoded, so it can be swapped
  without a code change. Browse available slugs at
  https://openrouter.ai/models.
- Remembers the last `MEMORY_MAX_MESSAGES` (default 20) messages of each
  *individual* pilot's conversation, so follow-up questions work — even
  though everyone shares the same group chat, memory is keyed per LINE
  user, not per group, so one pilot's thread never leaks into another's.
  It's in-process only (cleared on restart), not a persisted chat log.

## Adding manuals

Both wings (`manuals/wings/...`) and motors (`manuals/motors/...`) are
indexed today. To add a category or a new brand/model:

1. Put the source PDF(s) in `pdf/`.
2. Convert each PDF to Markdown (e.g. with the `pdf-to-markdown` skill in
   Claude Code — same process used for the existing manuals) and save
   the result as:
   ```
   manuals/<wings|motors>/<Brand>/<Model>/<Year>/manual.md
   ```
   Example: `manuals/motors/Vittorazi/Moster-185-Plus/2025/manual.md`.
   The folder names (`Brand`/`Model`/`Year`) are read as metadata and shown
   in the bot's citations, so keep them accurate and consistent with the
   existing folders. `Year` should be the manual's real edition/print/
   revision year; use the literal folder name `undated` instead of
   guessing when neither the manual's own text nor its PDF metadata
   carries a trustworthy date (e.g. `manuals/motors/Polini/Thor-100/undated`,
   `manuals/motors/Simonini/Mini-2-Plus/undated` and
   `manuals/motors/Sky-Engines/Sky-110S/undated` — all sourced from a
   third-party PDF mirror (manualslib.com) whose file metadata only
   reflected when that mirror page was exported, not any real manufacturer
   edition date).
   `manuals/motors/Sky-Engines/Sky-150/2021` is the opposite case: no date
   anywhere in the manual text, but the PDF's own metadata is a single-pass
   Microsoft Word 2019 export authored 2021-02-03 by the manufacturer, which
   is trustworthy enough to date the folder — the `manual.md` header records
   where the year came from.
3. Rebuild the vector index so the bot picks up the new content:
   ```bash
   docker compose exec ppg-bot python -m app.ingest --rebuild
   ```
   Run this on whichever host is actually serving traffic (production,
   once deployed) — `data/` (the index) is local to each host/volume, it
   isn't synced automatically just by adding files.
4. Sanity-check retrieval before trusting it in the group, e.g.:
   ```bash
   docker compose exec ppg-bot python -c "
   import asyncio
   from app.rag import answer
   print(asyncio.run(answer('What oil mix ratio does the Moster 185 use?')))
   "
   ```

No code changes are needed to add a category — `app/ingest.py` walks all
of `manuals/` regardless of category name, and the bot's system prompt
already talks about "wings and motors" generically.

(On first-ever container startup with an empty `data/` volume, the index
is also built automatically — but only if it's still empty; once it has
been built once, you must use `--rebuild` to pick up new/changed files.)

## The two sources

`app/web_ingest.py` crawls
https://www.southwestairsports.com/ (Had Robinson's paramotor technical
site — Top 80 / Moster / Thor service notes, carburetor rebuilds, fuel
systems, weather) and indexes it into a **separate** Chroma collection
(`website`) in the same `data/` volume. Keeping it out of the `manuals`
collection is deliberate: the two are retrieved and labelled separately, so
a web page can never be silently presented as a manufacturer's manual.

It reads the site's `sitemap.xml`, indexes only HTML pages (images and PDFs
are skipped), honours `robots.txt`, and pauses `WEBSITE_CRAWL_DELAY` seconds
between fetches. Currently ~299 pages → ~1543 chunks.

```bash
docker compose exec ppg-bot python -m app.web_ingest            # incremental
docker compose exec ppg-bot python -m app.web_ingest --dry-run  # show the plan
docker compose exec ppg-bot python -m app.web_ingest --rebuild  # wipe + re-crawl
```

### Keeping it fresh

The index is a snapshot, and nothing re-crawls on its own — startup only
reads the count. Left alone it drifts: a changed page means the bot answers
from old text while citing the **live** URL, a deleted page means it cites a
404, and a new page stays invisible.

So a cron job refreshes it daily (installed on this host, `crontab -l`):

```cron
30 3 * * * /path/to/ppg-guru-bot/scripts/refresh-website.sh
```

That runs an **incremental** refresh. The sitemap gives a `lastmod` per URL
and every chunk records the `lastmod` it was built from, so only genuinely
changed pages are re-fetched: a no-change day is one sitemap fetch and ~6
seconds, versus ~4 minutes and 299 fetches for a full rebuild. Pages that
have left the sitemap are dropped, so the bot stops citing dead URLs. Pages
that are pure navigation stubs are remembered as such, so they aren't
re-fetched every night just because they hold no prose.

The script restarts the bot **only when the index actually changed** — the
server holds Chroma open from process start, so a restart is what makes it
serve the new content. That restart also clears in-process conversation
memory, which is why it doesn't happen on the many days nothing changes.
Logs go to `logs/website-refresh.log`.

Answers drawing on the website carry the crawl date, so a pilot who follows
a cited link knows which version the bot spoke for:

> 📘🌐 Sources: manufacturer manuals + Southwest Airsports website, indexed
> 2026-09-04 (third-party)

Two limits worth knowing: a sitemap entry with no `lastmod` can't be proven
unchanged, so it's re-fetched every run (this site declares `lastmod` on all
1702 entries, so it doesn't bite here); and a page edited *without* its
`lastmod` moving will be missed until the next `--rebuild`.

### How an answer is put together

Both collections are searched on every question, and both sets of excerpts
go to the model in a single call, clearly separated. It is free to blend
them, under two standing rules:

- **The manufacturer's manual wins.** Where the two disagree, or where the
  website extends a manufacturer procedure, the answer gives the manual's
  position and then notes what the website adds or contradicts.
- **Every claim is cited where it is made** — `(Dudek Hadron 3 (2024),
  Technical Data)` for a manual, `(Southwest Airsports: Clogged muffler —
  https://...)` for a page. Website claims touching maintenance or anything
  safety-critical also carry a note that it is the site author's guidance.

The model then declares which sources it actually used, and that drives the
header. The declaration is cross-checked against the answer: website
citations must carry a full URL, so the site's domain appearing in the body
is hard evidence the website was used, whatever the model claimed.

There is **one retry rung**, for when neither source answered on the first
pass. The model replies `INSUFFICIENT: <english search terms>` — a sentinel
rather than prose, so it is detected exactly whatever language the pilot
used — and the question is asked again with:

- **a much wider manual search** (`MANUAL_RETRY_K`, default 25). Spec tables
  rank badly against natural-language questions and sibling models crowd
  each other out: the Hadron 3's own take-off weight table sits at rank 20
  for "what is the max take-off weight of the Hadron 3", below the
  Hadron 4's.
- **the website re-searched with those English terms.** They cost nothing —
  the model has already read the question — and they are what makes web
  retrieval work cross-lingually: the raw Thai question "ท่อไอเสีย Top 80
  อุดตันเขม่า ทำความสะอาดอย่างไร" ranks the right page **43rd**, while the
  model's English rendering of it ranks that page **1st**.

There is a **second trigger for that wider manual search**: any answer that
came out website-only. That is the case where a missed manual chunk does the
most damage — it sends a pilot to third-party guidance for something the
manufacturer documents. Real example: "how to fix or replace the pull
starter of Thor 100" is answered well by the website, while the Thor 100
manual's own §9.4 "Starter Rope Replacement" ranks **18th** for that
phrasing and never reached the first pass. The bot then told the pilot the
manual didn't cover it and recommended a clone starter. With the re-check it
leads with the manual's official procedure and offers the third-party part
as the alternative.

Relatedly, the prompt forbids claiming a manual "doesn't cover" something.
Excerpts are search results, not whole documents, and absence from a search
is not absence from the manual.

If the second pass still can't answer, the bot says so plainly in the
pilot's language and attaches no source header — a non-answer is not
attributed to anyone.

Note what does *not* decide any of this: a similarity threshold. Those were
measured and don't work here. Every chunk in the index is paramotor prose,
so embedding distances land in a narrow band and rank nonsense above real
matches — "how do I fix a clogged muffler on a Top 80" (in no manual)
scores **0.88** against the manuals while a genuine Hadron 3 take-off
weight question scores **0.86**, and "how do I bake sourdough bread"
outscores a real Thai question.

A question answered on the first pass costs exactly one LLM call. Replies
are dispatched from a FastAPI background task, so the retry's latency never
risks LINE's reply-token timeout.

## Local dev (this machine)

This machine isn't public, so LINE can't reach its webhook directly — real
end-to-end testing (actually messaging the bot in a LINE group) only
happens after deploying to the production host, or with a temporary public
tunnel (e.g. `cloudflared tunnel --url http://127.0.0.1:8801`) pointed at a
webhook URL you set temporarily in the LINE console.

Until then, test the RAG + LLM pipeline directly, bypassing LINE:

```bash
docker compose up --build -d
curl http://127.0.0.1:8801/healthz

# ask it something
docker compose exec ppg-bot python scripts/ask.py "What is the MTOW of the Cosmos Power 2?"

# also show which manual sections AND website pages were retrieved, plus
# which source the answer ended up using (useful for debugging bad or
# wrongly-routed answers)
docker compose exec ppg-bot python scripts/ask.py --show-retrieval "your question"

docker compose logs -f
```

## Deploying to eng-ai.buu.ac.th (production)

1. Copy this project to the production host (rsync/git), including `.env`
   with real secrets (never commit `.env`).
2. On the production host:
   ```bash
   docker compose up --build -d
   ```
   This binds the app to `127.0.0.1:8801` only — not exposed publicly by
   itself.
3. Add this to the existing NGINX server block for `eng-ai.buu.ac.th`:
   ```nginx
   location /line/webhook {
       proxy_pass http://127.0.0.1:8801/line/webhook;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_read_timeout 60s;
   }
   ```
   Don't add anything that rewrites/buffers the request body — LINE signs
   the raw body and the webhook will reject mismatched signatures.
4. `sudo nginx -t && sudo systemctl reload nginx`
5. In the LINE Developers Console, set the webhook URL to
   `https://eng-ai.buu.ac.th/line/webhook` and click **Verify**.
6. Add the bot to the target LINE group and mention it to test.

## Upstream rate limits / transient failures

OpenRouter forwards the *provider's* own errors verbatim, so a 429 here
usually means the provider serving your chosen model is throttling or at
capacity — not that your account is out of credit. It's worth knowing which
providers serve a model, because a single-provider model has nothing to fail
over to:

```bash
curl -s https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints \
  | python3 -c "import json,sys; [print(e['provider_name']) for e in json.load(sys.stdin)['data']['endpoints']]"
```

`app/llm.py` retries these automatically: `LLM_MAX_ATTEMPTS` (default 3)
attempts on 429/500/502/503/504, with exponential backoff, jitter, and the
server's `Retry-After` honoured when sent (capped at `LLM_RETRY_MAX_DELAY`,
since a pilot is waiting on a reply). Errors that a retry can't fix — 400
malformed, 401 bad key, 402 out of credits — are raised immediately.

This matters more than it looks: a question that needs the retry rung makes
two LLM calls (see above), so without retries a single blip on either one
costs the whole answer.

To check the state of a key — credits, spend cap, whether it's free tier:

```bash
curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY"
```

## Changing the model

Edit `OPENROUTER_MODEL` in `.env` on the production host, then:

```bash
docker compose up -d --force-recreate
```

## Notes / follow-ups

- Motors currently covered: Ciscomotors C-Max, PAP Top 80, Polini
  Thor 100/Thor 200, Simonini Mini 2 Plus, Sky Engines SKY 110S/SKY 150,
  Vittorazi Atom 80, and Vittorazi Moster 185 Plus. See "Adding manuals"
  above to add more.
- The website index goes stale as the site changes; re-run
  `python -m app.web_ingest --rebuild` periodically to refresh it. The
  crawler currently indexes HTML pages only — the ~280 PDFs linked from the
  site (service bulletins, parts diagrams) are not indexed.
- Answers are generated only from retrieved manual/website excerpts and the model
  is instructed to say so when it can't find something — but this is not a
  substitute for the manufacturer's manual or a certified instructor,
  especially for safety-critical procedures. The system prompt already
  nudges toward that caveat; keep an eye on real answers early on.
