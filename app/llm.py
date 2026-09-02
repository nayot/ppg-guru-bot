import httpx

from app.config import settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


async def chat(system_prompt: str, user_prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://eng-ai.buu.ac.th",
        "X-Title": "PPG Guru Bot",
    }
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"].strip()
