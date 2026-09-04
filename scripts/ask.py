"""Quick manual test of the RAG+LLM pipeline, without going through LINE.

Usage (inside the container):
    docker compose exec ppg-bot python scripts/ask.py "What is the MTOW of the Hadron 3?"
    docker compose exec ppg-bot python scripts/ask.py --show-retrieval "..."
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rag import answer_with_source, retrieve, retrieve_website, with_source_header


async def main():
    args = sys.argv[1:]
    show_retrieval = "--show-retrieval" in args
    args = [a for a in args if a != "--show-retrieval"]

    if not args:
        print('Usage: python scripts/ask.py [--show-retrieval] "your question"')
        raise SystemExit(1)

    question = " ".join(args)

    if show_retrieval:
        print("--- retrieved manual chunks ---")
        for hit in retrieve(question):
            m = hit["meta"]
            print(f"[{m.get('brand')} {m.get('model')} {m.get('year')} - {m.get('section')}]")
        print()
        print("--- retrieved website chunks (fallback candidates) ---")
        for hit in retrieve_website(question):
            m = hit["meta"]
            print(f"[{m.get('title')}] {m.get('url')}")
        print()

    reply, source = await answer_with_source(question)
    print(f"--- answer (source: {source}) ---")
    print(with_source_header(reply, source))


if __name__ == "__main__":
    asyncio.run(main())
