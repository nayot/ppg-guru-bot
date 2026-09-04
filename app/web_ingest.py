"""Crawl the configured website and index it into its own Chroma collection.

This is the bot's *fallback* source: the manuals under `manuals/` stay
primary, and this collection is only ever consulted when they don't answer
(see `app/rag.py`). It is kept as a separate collection — not extra rows in
`manuals` — so a web page can never be silently retrieved and cited as if it
were a manufacturer's manual.

Unlike the manuals index, this is NOT built automatically on startup: it
makes a few hundred outbound HTTP requests, so it runs only when invoked
explicitly.

    python -m app.web_ingest --rebuild
"""

import argparse
import re
import sys
import time
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


def discover_urls(base_url: str) -> list[str]:
    """Page URLs to index, taken from the site's sitemap.xml.

    The sitemap lists images and PDFs too; only HTML pages are indexed.
    """
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    resp = httpx.get(
        sitemap_url,
        timeout=60.0,
        follow_redirects=True,
        headers={"User-Agent": settings.website_user_agent},
    )
    resp.raise_for_status()
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text)

    host = urlparse(base_url).netloc
    rp, orphans = load_robots(base_url)

    urls, seen = [], set()
    for loc in locs:
        parsed = urlparse(loc)
        if parsed.netloc != host:
            continue
        if not parsed.path.lower().endswith(PAGE_SUFFIXES):
            continue
        clean = parsed._replace(fragment="", query="").geturl()
        if clean in seen:
            continue
        seen.add(clean)
        if not _allowed(clean, rp, orphans):
            print(f"  skipping (robots.txt): {clean}")
            continue
        urls.append(clean)
    return urls


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


def fetch_pages(urls: list[str]) -> list[dict]:
    pages = []
    headers = {"User-Agent": settings.website_user_agent}
    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        for i, url in enumerate(urls, 1):
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                print(f"  [{i}/{len(urls)}] FAILED {url}: {exc}", file=sys.stderr)
                continue
            title, text = extract_page(resp.text)
            if len(text) < MIN_TEXT_CHARS:
                print(f"  [{i}/{len(urls)}] skipped (too short) {url}")
                continue
            pages.append({"url": url, "title": title or url, "text": text})
            print(f"  [{i}/{len(urls)}] {len(text):>6} chars  {title[:60]}")
            if settings.website_crawl_delay:
                time.sleep(settings.website_crawl_delay)
    return pages


def build_web_index(rebuild: bool = False) -> int:
    if not settings.website_enabled:
        print("website_enabled is false — nothing to do.")
        return 0

    client = chromadb.PersistentClient(
        path=settings.data_dir,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    if rebuild:
        try:
            client.delete_collection(settings.website_collection)
        except Exception:
            pass
    collection = client.get_or_create_collection(settings.website_collection)
    if not rebuild and collection.count() > 0:
        return collection.count()

    base = settings.website_base_url
    print(f"Discovering pages from {base} ...")
    urls = discover_urls(base)
    if settings.website_max_pages:
        urls = urls[: settings.website_max_pages]
    print(f"Fetching {len(urls)} pages ...")
    pages = fetch_pages(urls)
    if not pages:
        print("WARNING: no pages fetched", file=sys.stderr)
        return 0

    texts, metadatas, ids = [], [], []
    for page in pages:
        # Prefixing the page title keeps the chunk self-describing: a chunk
        # from the middle of a long page otherwise loses all context of what
        # component or procedure it's about.
        for n, piece in enumerate(chunk_section(page["title"], page["text"])):
            texts.append(f"{page['title']}\n\n{piece}")
            metadatas.append(
                {
                    "source": "website",
                    "site": settings.website_name,
                    "url": page["url"],
                    "title": page["title"],
                }
            )
            ids.append(f"{page['url']}::{n}")

    print(f"Embedding {len(texts)} chunks ...")
    collection.add(
        ids=ids,
        embeddings=embed_documents(texts),
        documents=texts,
        metadatas=metadatas,
    )
    return len(texts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--rebuild", action="store_true", help="delete and re-crawl the whole site"
    )
    args = ap.parse_args()
    n = build_web_index(rebuild=args.rebuild)
    print(f"Indexed {n} website chunks into {settings.data_dir}")
