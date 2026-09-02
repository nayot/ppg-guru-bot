from functools import lru_cache

from sentence_transformers import SentenceTransformer

from app.config import settings


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    return SentenceTransformer(settings.embedding_model)


def embed_documents(texts: list[str]) -> list[list[float]]:
    # multilingual-e5 models expect a "passage: " prefix on indexed text.
    prefixed = [f"passage: {t}" for t in texts]
    return _model().encode(prefixed, normalize_embeddings=True).tolist()


def embed_query(text: str) -> list[float]:
    # ...and a "query: " prefix on search queries. This asymmetry is what
    # gives good retrieval quality, including cross-lingual (e.g. Thai
    # question -> English manual passage).
    return _model().encode([f"query: {text}"], normalize_embeddings=True).tolist()[0]
