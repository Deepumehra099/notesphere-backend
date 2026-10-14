import asyncio
import json
import logging
from urllib.request import Request, urlopen

from bson import ObjectId

logger = logging.getLogger(__name__)
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _send_push_batch(messages: list[dict]) -> list[dict]:
    request = Request(
        EXPO_PUSH_URL,
        data=json.dumps(messages).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8")).get("data", [])


async def send_push_notification(db, user_id: str, title: str, body: str, data: dict) -> None:
    user = await db.users.find_one({"_id": ObjectId(user_id)}, {"expo_push_tokens": 1}) or {}
    tokens = user.get("expo_push_tokens", [])
    for start in range(0, len(tokens), 100):
        batch_tokens = tokens[start:start + 100]
        payloads = [
            {"to": token, "title": title, "body": body, "data": data, "sound": "default", "channelId": "messages"}
            for token in batch_tokens
        ]
        try:
            tickets = await asyncio.to_thread(_send_push_batch, payloads)
            stale_tokens = [
                batch_tokens[index]
                for index, ticket in enumerate(tickets)
                if ticket.get("status") == "error" and (ticket.get("details") or {}).get("error") == "DeviceNotRegistered"
            ]
            if stale_tokens:
                await db.users.update_one({"_id": ObjectId(user_id)}, {"$pull": {"expo_push_tokens": {"$in": stale_tokens}}})
        except Exception:
            logger.warning("Push notification delivery failed for user_id=%s", user_id, exc_info=True)
