"""Crawl the configured website and index it into its own Chroma collection.

This is the bot's *fallback* source: the manuals under `manuals/` stay
primary, and this collection is only ever consulted when they don't answer
(see `app/rag.py`). It is kept as a separate collection — not extra rows in
`manuals` — so a web page can never be silently retrieved and cited as if it
were a manufacturer's manual.

Unlike the manuals index, this is NOT built automatically on startup: it
makes outbound HTTP requests, so it runs only when invoked explicitly.

    python -m app.web_ingest              # incremental refresh (cron-friendly)
    python -m app.web_ingest --rebuild    # wipe and re-crawl everything
    python -m app.web_ingest --dry-run    # report what would change, fetch nothing

The default is incremental: the sitemap gives a `lastmod` per URL, and each
indexed chunk records the `lastmod` it was built from, so a refresh only
re-fetches pages that actually changed, adds pages that appeared, and drops
pages that vanished from the sitemap. A no-op day costs one sitemap fetch
instead of ~300 page fetches, which is what makes a daily cron reasonable.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import chromadb
import httpx
from bs4 import BeautifulSoup
from chromadb.config import Settings as ChromaSettings

from app.config import settings
from app.embeddings import embed_documents
from app.ingest import chunk_section

PAGE_SUFFIXES = (".htm", ".html")
# The template wrapper every content page on the site is built from; the
# body text lives inside it, and everything else is nav/masthead chrome.
CONTENT_SELECTOR = {"class": "main-content"}
STRIP_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "form")
MIN_TEXT_CHARS = 200


def load_robots(base_url: str) -> tuple[RobotFileParser, list[str]]:
    """Robots rules for the site, plus any 'orphan' Disallow paths.

    A Disallow line that isn't preceded by a User-agent line is malformed,
    and `RobotFileParser` drops it silently — which is how this site's
    robots.txt is actually written. The site owner clearly meant those
    paths to be off-limits to everyone, so we collect them separately and
    honour them rather than crawling a page we were asked to skip.
    """
    robots_url = urljoin(base_url, "/robots.txt")
    rp = RobotFileParser()
    rp.set_url(robots_url)
    orphans: list[str] = []
    try:
        body = httpx.get(
            robots_url,
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": settings.website_user_agent},
        ).text
        rp.parse(body.splitlines())
        seen_user_agent = False
        for line in body.splitlines():
            directive, _, value = line.partition(":")
            directive = directive.strip().lower()
            if directive == "user-agent":
                seen_user_agent = True
            elif directive == "disallow" and not seen_user_agent and value.strip():
                orphans.append(value.strip())
    except Exception as exc:  # no robots.txt, or unreachable
        print(f"WARNING: could not read {robots_url}: {exc}", file=sys.stderr)
    return rp, orphans


def _allowed(url: str, rp: RobotFileParser, orphans: list[str]) -> bool:
    path = urlparse(url).path
    if any(path.startswith(prefix) for prefix in orphans):
        return False
    return rp.can_fetch(settings.website_user_agent, url)


URL_ENTRY_RE = re.compile(r"<url>(.*?)</url>", re.S)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>")


def discover_urls(base_url: str, verbose: bool = True) -> dict[str, str]:
    """{page URL: lastmod} from the site's sitemap.xml.

    The sitemap lists images and PDFs too; only HTML pages are indexed.
    `lastmod` is "" for entries that don't declare one — those can't be
    proven unchanged, so a refresh always re-fetches them.
    """
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    resp = httpx.get(
        sitemap_url,
        timeout=60.0,
        follow_redirects=True,
        headers={"User-Agent": settings.website_user_agent},
    )
    resp.raise_for_status()

    host = urlparse(base_url).netloc
    rp, orphans = load_robots(base_url)

    found: dict[str, str] = {}
    for entry in URL_ENTRY_RE.findall(resp.text):
        loc = LOC_RE.search(entry)
        if not loc:
            continue
        parsed = urlparse(loc.group(1))
        if parsed.netloc != host:
            continue
        if not parsed.path.lower().endswith(PAGE_SUFFIXES):
            continue
        clean = parsed._replace(fragment="", query="").geturl()
        if clean in found:
            continue
        if not _allowed(clean, rp, orphans):
            if verbose:
                print(f"  skipping (robots.txt): {clean}")
            continue
        lastmod = LASTMOD_RE.search(entry)
        found[clean] = lastmod.group(1).strip() if lastmod else ""
    return found


def extract_page(html: str) -> tuple[str, str]:
    """(title, readable text) for one page."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    root = soup.find("div", CONTENT_SELECTOR) or soup.body or soup
    text = root.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return title, text.strip()


def fetch_pages(targets: dict[str, str]) -> tuple[list[dict], dict[str, str], list[str]]:
    """Fetch each URL.

    Returns (pages, too_short, failed). `too_short` is {url: lastmod} for
    pages that are pure navigation/image stubs with no prose — they're
    remembered rather than silently dropped, so the next refresh doesn't
    re-fetch all of them again every single run.
    """
    pages: list[dict] = []
    too_short: dict[str, str] = {}
    failed: list[str] = []
    headers = {"User-Agent": settings.website_user_agent}
    total = len(targets)
    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        for i, (url, lastmod) in enumerate(targets.items(), 1):
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                print(f"  [{i}/{total}] FAILED {url}: {exc}", file=sys.stderr)
                failed.append(url)
                continue
            title, text = extract_page(resp.text)
            if len(text) < MIN_TEXT_CHARS:
                print(f"  [{i}/{total}] stub, not indexed: {url}")
                too_short[url] = lastmod
            else:
                pages.append(
                    {
                        "url": url,
                        "title": title or url,
                        "text": text,
                        "lastmod": lastmod,
                    }
                )
                print(f"  [{i}/{total}] {len(text):>6} chars  {title[:60]}")
            if settings.website_crawl_delay:
                time.sleep(settings.website_crawl_delay)
    return pages, too_short, failed


def _open_collection(client, rebuild: bool):
    if rebuild:
        try:
            client.delete_collection(settings.website_collection)
        except Exception:
            pass
    return client.get_or_create_collection(settings.website_collection)


def _read_state(collection) -> tuple[dict[str, str], dict[str, str]]:
    """(indexed {url: lastmod}, stub {url: lastmod}) currently on record.

    Indexed pages carry their lastmod on every chunk; stubs have no chunks
    to hang it on, so they live in the collection's own metadata.
    """
    indexed: dict[str, str] = {}
    if collection.count():
        for m in collection.get(include=["metadatas"])["metadatas"]:
            url = m.get("url")
            if url:
                indexed[url] = m.get("lastmod", "")
    stubs: dict[str, str] = {}
    raw = (collection.metadata or {}).get("stub_pages")
    if raw:
        try:
            stubs = json.loads(raw)
        except (ValueError, TypeError):
            pass
    return indexed, stubs


def plan_refresh(
    current: dict[str, str], indexed: dict[str, str], stubs: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """(pages to fetch, URLs to drop) for this refresh.

    A page is fetched when it's new, or when the sitemap's lastmod differs
    from the one it was indexed under. An entry with no lastmod can't be
    proven unchanged, so it is always re-fetched. Anything indexed that has
    left the sitemap — deleted, moved, or newly disallowed by robots.txt —
    is dropped, so the bot stops citing URLs that no longer resolve.
    """
    to_fetch = {
        url: lastmod
        for url, lastmod in current.items()
        if not lastmod
        or (url not in indexed and url not in stubs)
        or (url in indexed and indexed[url] != lastmod)
        or (url in stubs and stubs[url] != lastmod)
    }
    to_drop = [url for url in indexed if url not in current]
    return to_fetch, to_drop


def _chunk_page(page: dict, fetched_at: str) -> tuple[list[str], list[dict], list[str]]:
    texts, metadatas, ids = [], [], []
    for n, piece in enumerate(chunk_section(page["title"], page["text"])):
        # Prefixing the page title keeps the chunk self-describing: a chunk
        # from the middle of a long page otherwise loses all context of what
        # component or procedure it's about.
        texts.append(f"{page['title']}\n\n{piece}")
        metadatas.append(
            {
                "source": "website",
                "site": settings.website_name,
                "url": page["url"],
                "title": page["title"],
                "lastmod": page["lastmod"],
                "fetched_at": fetched_at,
            }
        )
        ids.append(f"{page['url']}::{n}")
    return texts, metadatas, ids


def refresh_web_index(rebuild: bool = False, dry_run: bool = False) -> int:
    """Bring the web collection in line with the site. Returns chunks written."""
    if not settings.website_enabled:
        print("website_enabled is false — nothing to do.")
        return 0

    client = chromadb.PersistentClient(
        path=settings.data_dir,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    collection = _open_collection(client, rebuild and not dry_run)
    indexed, stubs = ({}, {}) if rebuild else _read_state(collection)

    base = settings.website_base_url
    print(f"Reading sitemap for {base} ...")
    current = discover_urls(base, verbose=not dry_run)
    to_fetch, to_drop = plan_refresh(current, indexed, stubs)
    if settings.website_max_pages:
        to_fetch = dict(list(to_fetch.items())[: settings.website_max_pages])

    print(
        f"  {len(current)} pages in sitemap | {len(indexed)} indexed, "
        f"{len(stubs)} known stubs\n"
        f"  -> {len(to_fetch)} to fetch, {len(to_drop)} to drop"
    )
    if dry_run:
        for url in list(to_fetch)[:20]:
            print(f"     fetch: {url}")
        for url in to_drop[:20]:
            print(f"     drop:  {url}")
        return 0
    if not to_fetch and not to_drop:
        _stamp(collection, stubs)
        print("Already up to date.")
        return 0

    pages, too_short, failed = fetch_pages(to_fetch) if to_fetch else ([], {}, [])

    # Replace each refetched page wholesale rather than editing chunks in
    # place: a shorter new version would otherwise leave the tail of the old
    # one behind under ids that are never overwritten.
    for url in to_drop + [p["url"] for p in pages] + list(too_short):
        if url in indexed:
            collection.delete(where={"url": url})

    written = 0
    if pages:
        fetched_at = datetime.now(timezone.utc).date().isoformat()
        texts, metadatas, ids = [], [], []
        for page in pages:
            t, m, i = _chunk_page(page, fetched_at)
            texts += t
            metadatas += m
            ids += i
        print(f"Embedding {len(texts)} chunks ...")
        collection.add(
            ids=ids, embeddings=embed_documents(texts), documents=texts,
            metadatas=metadatas,
        )
        written = len(texts)

    stubs = {u: l for u, l in stubs.items() if u in current and u not in failed}
    stubs.update(too_short)
    _stamp(collection, stubs)
    if failed:
        print(f"WARNING: {len(failed)} page(s) failed; left as-is", file=sys.stderr)
    return written


def _stamp(collection, stubs: dict[str, str]) -> None:
    """Record when this refresh ran, and which pages were stubs.

    `last_crawl` is what the bot cites as its "indexed on" date, so it is
    stamped even on a no-op run — the index really was confirmed current.
    """
    collection.modify(
        metadata={
            "last_crawl": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stub_pages": json.dumps(stubs),
        }
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--rebuild", action="store_true", help="delete and re-crawl the whole site"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="report the plan without fetching"
    )
    args = ap.parse_args()
    n = refresh_web_index(rebuild=args.rebuild, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"Wrote {n} website chunks into {settings.data_dir}")
