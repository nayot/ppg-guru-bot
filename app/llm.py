import asyncio
import logging
import random

import httpx

from app.config import settings

logger = logging.getLogger("ppg-bot")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Worth another attempt: 429 is usually the upstream provider throttling us
# (OpenRouter forwards the provider's own 429 verbatim, and a model served by
# a single provider has nothing to fail over to), and 5xx is a transient
# server or gateway fault. Everything else in the 4xx range — malformed
# request, bad key, 402 out of credits — fails identically on a retry, so it
# is raised immediately rather than delaying the pilot's reply three times
# over to reach the same outcome.
RETRY_STATUS = {429, 500, 502, 503, 504}


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """Seconds to wait before attempt number `attempt + 1`.

    Prefers the server's own Retry-After when it sends one, but caps it: a
    pilot is waiting on a LINE reply, so a provider asking for a 60-second
    pause is better answered with an error than with a silent stall.
    """
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(float(raw), settings.llm_retry_max_delay)
            except ValueError:
                pass  # HTTP-date form — fall through to the backoff below
    delay = min(
        settings.llm_retry_base_delay * (2 ** (attempt - 1)),
        settings.llm_retry_max_delay,
    )
    # Jitter, so the answer ladder's two or three calls don't line up and
    # retry in lockstep into the same throttle.
    return delay * (0.5 + random.random() / 2)


async def chat(
    system_prompt: str, user_prompt: str, history: list[dict] | None = None
) -> str:
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://eng-ai.buu.ac.th",
        "X-Title": "PPG Guru Bot",
    }
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})
    payload = {
        "model": settings.openrouter_model,
        "messages": messages,
        "temperature": 0.2,
    }

    last_exc: Exception
    for attempt in range(1, settings.llm_max_attempts + 1):
        response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(OPENROUTER_URL, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in RETRY_STATUS:
                raise
            last_exc = exc
            response = exc.response
            detail = f"HTTP {exc.response.status_code}"
        except httpx.TransportError as exc:  # timeout, connection reset, DNS
            last_exc = exc
            detail = type(exc).__name__

        if attempt == settings.llm_max_attempts:
            break
        delay = _retry_delay(attempt, response)
        logger.warning(
            "OpenRouter %s — retrying in %.1fs (attempt %d/%d)",
            detail,
            delay,
            attempt,
            settings.llm_max_attempts,
        )
        await asyncio.sleep(delay)

    logger.error(
        "OpenRouter call failed after %d attempts: %s",
        settings.llm_max_attempts,
        last_exc,
    )
    raise last_exc
