# PPG Guru Bot

LINE group chatbot that answers technical questions about paramotor wings
and motors, grounded in the manuals under `manuals/`.

- Responds only when @mentioned in a group/room; always responds in 1:1 chat.
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

## Adding manuals (e.g. motors, later)

Only wings are indexed today (`manuals/wings/...`) — motors were skipped
for now. To add a category (motors, or a new wing brand) later:

1. Put the source PDF(s) in `pdf/`.
2. Convert each PDF to Markdown (e.g. with the `pdf-to-markdown` skill in
   Claude Code — same process used for the existing wing manuals) and save
   the result as:
   ```
   manuals/<wings|motors>/<Brand>/<Model>/<Year>/manual.md
   ```
   Example: `manuals/motors/Vittorazi/Moster-185/2023/manual.md`.
   The folder names (`Brand`/`Model`/`Year`) are read as metadata and shown
   in the bot's citations, so keep them accurate and consistent with the
   existing wing folders.
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

# also show which manual sections were retrieved (useful for debugging
# bad/irrelevant answers)
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

## Changing the model

Edit `OPENROUTER_MODEL` in `.env` on the production host, then:

```bash
docker compose up -d --force-recreate
```

## Notes / follow-ups

- Motor manuals aren't in `manuals/` yet (skipped for the initial launch)
  — the bot will answer wing questions well today but has nothing to draw
  on for motors until those are added. See "Adding manuals" above.
- Answers are generated only from retrieved manual excerpts and the model
  is instructed to say so when it can't find something — but this is not a
  substitute for the manufacturer's manual or a certified instructor,
  especially for safety-critical procedures. The system prompt already
  nudges toward that caveat; keep an eye on real answers early on.
