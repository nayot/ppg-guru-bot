"""Per-user rolling conversation memory, for chat continuity.

Keyed by LINE user_id, not group_id/room_id. The bot runs in a shared
group chat where several pilots can each be mid-conversation with it at
once — keying by group would interleave everyone's questions and answers
into one thread and confuse the model about who asked what. Keying by
user_id gives each pilot their own thread even when they share a group.

In-process only: a container restart clears it. That's an accepted
tradeoff for a lightweight continuity feature, not a durable chat log.
"""

from collections import defaultdict, deque

from app.config import settings

_history: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=settings.memory_max_messages)
)


def get_history(user_id: str) -> list[dict]:
    return list(_history[user_id])


def append(user_id: str, role: str, content: str) -> None:
    _history[user_id].append({"role": role, "content": content})
