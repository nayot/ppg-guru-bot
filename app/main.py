import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, JoinEvent, MessageEvent, TextMessageContent

from app.config import settings
from app.ingest import build_index
from app.rag import answer
from app.richtext import build_reply_message

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

ALLOWED_SOURCE_IDS = {
    s.strip() for s in settings.allowed_source_ids.split(",") if s.strip()
}
if not ALLOWED_SOURCE_IDS:
    logger.warning(
        "ALLOWED_SOURCE_IDS is empty — the bot will respond to any user/group/room. "
        "Set it in .env to restrict access."
    )

ACCESS_DENIED_MESSAGE = (
    "Sorry, this bot is restricted to approved groups/users. "
    "Contact the administrator for access."
)


def get_source_id(source) -> str | None:
    """The LINE ID identifying who/where a message came from.

    A 1:1 chat is identified by user_id; a group or multi-person room by its
    own group_id/room_id (shared by everyone in it) — that's what gets
    whitelisted for group access, not individual members' user_ids.
    """
    source_type = getattr(source, "type", None)
    if source_type == "user":
        return getattr(source, "user_id", None)
    if source_type == "group":
        return getattr(source, "group_id", None)
    if source_type == "room":
        return getattr(source, "room_id", None)
    return None


def is_allowed(source) -> bool:
    if not ALLOWED_SOURCE_IDS:
        return True
    return get_source_id(source) in ALLOWED_SOURCE_IDS


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
    if not is_allowed(event.source):
        return False
    source_type = getattr(event.source, "type", None)
    if source_type in ("group", "room"):
        return mentioned
    return True  # direct 1:1 chat with the bot


def show_loading_animation(event: MessageEvent) -> None:
    """Show LINE's "..." typing indicator while we compute a reply.

    Only supported in 1:1 chats — LINE rejects this call for group/room chats.
    """
    if getattr(event.source, "type", None) != "user":
        return
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).show_loading_animation(
                ShowLoadingAnimationRequest(
                    chat_id=event.source.user_id, loading_seconds=60
                )
            )
    except Exception:
        logger.exception("Failed to show loading animation")


async def handle_event(event: MessageEvent) -> None:
    if not isinstance(event.message, TextMessageContent):
        return

    text, mentioned = strip_mentions(event)
    if not should_respond(event, mentioned):
        return

    show_loading_animation(event)

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
                    messages=[build_reply_message(reply_text)],
                )
            )
    except Exception:
        logger.exception("Failed to send LINE reply")


async def handle_join(event: JoinEvent) -> None:
    """Leave immediately if a non-whitelisted group/room adds the bot."""
    if is_allowed(event.source):
        return
    source_type = getattr(event.source, "type", None)
    logger.info(
        "Leaving non-whitelisted %s %s", source_type, get_source_id(event.source)
    )
    with ApiClient(line_config) as api_client:
        api = MessagingApi(api_client)
        try:
            api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=ACCESS_DENIED_MESSAGE)],
                )
            )
        except Exception:
            logger.exception("Failed to send access-denied message before leaving")
        try:
            if source_type == "group":
                api.leave_group(event.source.group_id)
            elif source_type == "room":
                api.leave_room(event.source.room_id)
        except Exception:
            logger.exception("Failed to leave non-whitelisted chat")


async def handle_follow(event: FollowEvent) -> None:
    """A user can't be un-friended via the API, so just explain why the bot
    won't otherwise respond (should_respond already blocks their messages)."""
    if is_allowed(event.source):
        return
    logger.info("Non-whitelisted user followed: %s", get_source_id(event.source))
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=ACCESS_DENIED_MESSAGE)],
                )
            )
    except Exception:
        logger.exception("Failed to send access-denied message")


async def process_event(event: MessageEvent) -> None:
    try:
        await handle_event(event)
    except Exception:
        logger.exception("Failed to handle event")


async def process_join(event: JoinEvent) -> None:
    try:
        await handle_join(event)
    except Exception:
        logger.exception("Failed to handle join event")


async def process_follow(event: FollowEvent) -> None:
    try:
        await handle_follow(event)
    except Exception:
        logger.exception("Failed to handle follow event")


@app.post("/line/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Ack immediately — LINE resends the event (and the reply_token gets
    # reused/invalidated) if we don't respond before it gives up waiting,
    # and RAG + LLM generation can easily take longer than that.
    for event in events:
        source_type = getattr(event.source, "type", None)
        logger.info(
            "Received %s event from %s %s",
            event.type, source_type, get_source_id(event.source),
        )
        if isinstance(event, MessageEvent):
            background_tasks.add_task(process_event, event)
        elif isinstance(event, JoinEvent):
            background_tasks.add_task(process_join, event)
        elif isinstance(event, FollowEvent):
            background_tasks.add_task(process_follow, event)

    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
