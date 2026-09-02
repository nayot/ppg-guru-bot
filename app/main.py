import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from app.config import settings
from app.ingest import build_index
from app.rag import answer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ppg-bot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    count = build_index(rebuild=False)
    logger.info("Vector store ready with %d chunks", count)
    yield


app = FastAPI(lifespan=lifespan)

parser = WebhookParser(settings.line_channel_secret)
line_config = Configuration(access_token=settings.line_channel_access_token)


def strip_mentions(event: MessageEvent) -> tuple[str, bool]:
    """Remove @mention substrings from the message text.

    Returns (clean_text, bot_was_mentioned). LINE reports mention ranges as
    (index, length) over the raw text, so we cut them out back-to-front to
    keep earlier offsets valid.
    """
    msg: TextMessageContent = event.message
    text = msg.text
    mentioned = False
    mention = getattr(msg, "mention", None)
    mentionees = getattr(mention, "mentionees", None) if mention else None
    if mentionees:
        ranges = []
        for m in mentionees:
            if getattr(m, "is_self", False):
                mentioned = True
            ranges.append((m.index, m.length))
        for index, length in sorted(ranges, reverse=True):
            text = text[:index] + text[index + length:]
    return text.strip(), mentioned


def should_respond(event: MessageEvent, mentioned: bool) -> bool:
    source_type = getattr(event.source, "type", None)
    if source_type in ("group", "room"):
        return mentioned
    return True  # direct 1:1 chat with the bot


async def handle_event(event: MessageEvent) -> None:
    if not isinstance(event.message, TextMessageContent):
        return

    text, mentioned = strip_mentions(event)
    if not should_respond(event, mentioned):
        return

    if not text:
        reply_text = "Hi! Ask me a technical question about a paramotor wing or motor."
    else:
        try:
            reply_text = await answer(text)
        except Exception:
            logger.exception("RAG/LLM call failed")
            reply_text = "Sorry, something went wrong answering that. Please try again in a moment."

    reply_text = reply_text[:4999]

    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )
    except Exception:
        logger.exception("Failed to send LINE reply")


@app.post("/line/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if isinstance(event, MessageEvent):
            try:
                await handle_event(event)
            except Exception:
                logger.exception("Failed to handle event")

    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
