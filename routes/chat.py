import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import cloudinary
import cloudinary.uploader
import jwt
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user, get_jwt_secret
from utils.db import get_db
from utils.push_notifications import send_push_notification

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

router = APIRouter(prefix="/api/chat", tags=["chat"])
ws_router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)

DELETE_FOR_EVERYONE_WINDOW_MINUTES = 10
PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s\-()]{8,}\d)")
BLOCKED_TEXT_PATTERNS = [
    re.compile(r"\bwhatsapp\b", re.IGNORECASE),
    re.compile(r"\bcontact\b", re.IGNORECASE),
    re.compile(r"\bphone\s*number\b", re.IGNORECASE),
    re.compile(r"\bcall\s*me\b", re.IGNORECASE),
    re.compile(r"\bprice\b", re.IGNORECASE),
    re.compile(r"\bbudget\b", re.IGNORECASE),
    re.compile(r"\bdiscount\b", re.IGNORECASE),
    re.compile(r"\bnegotiat", re.IGNORECASE),
    re.compile(r"\bpayment\b", re.IGNORECASE),
    re.compile(r"\bpay\b", re.IGNORECASE),
    re.compile(r"\bcost\b", re.IGNORECASE),
    re.compile(r"\brs\b", re.IGNORECASE),
    re.compile(r"\brupees?\b", re.IGNORECASE),
    re.compile(r"\bcredits?\b", re.IGNORECASE),
]
ABUSIVE_TEXT_PATTERNS = [
    re.compile(r"\bidiot\b", re.IGNORECASE),
    re.compile(r"\bstupid\b", re.IGNORECASE),
    re.compile(r"\bdumb\b", re.IGNORECASE),
    re.compile(r"\bfool\b", re.IGNORECASE),
    re.compile(r"\bshit\b", re.IGNORECASE),
    re.compile(r"\bbitch\b", re.IGNORECASE),
    re.compile(r"\bbastard\b", re.IGNORECASE),
    re.compile(r"\bharass", re.IGNORECASE),
    re.compile(r"\babuse", re.IGNORECASE),
]
SPAM_TEXT_PATTERNS = [
    re.compile(r"\btelegram\b", re.IGNORECASE),
    re.compile(r"\blink in bio\b", re.IGNORECASE),
    re.compile(r"\bclick here\b", re.IGNORECASE),
    re.compile(r"\bfree money\b", re.IGNORECASE),
    re.compile(r"\bearn fast\b", re.IGNORECASE),
    re.compile(r"\bsubscribe\b", re.IGNORECASE),
    re.compile(r"\bpromo\b", re.IGNORECASE),
]
SUSPICIOUS_TEXT_PATTERNS = [
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\binstagram\b", re.IGNORECASE),
    re.compile(r"\btelegram\b", re.IGNORECASE),
    re.compile(r"\bemail me\b", re.IGNORECASE),
]


class ConnectionManager:
    def __init__(self):
        self.active: dict[str, set[WebSocket]] = {}
        self.active_chats: dict[str, dict[WebSocket, str | None]] = {}

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        user_connections = self.active.setdefault(user_id, set())
        was_offline = len(user_connections) == 0
        user_connections.add(ws)
        self.active_chats.setdefault(user_id, {})[ws] = None
        return was_offline

    def disconnect(self, user_id: str, ws: WebSocket):
        user_connections = self.active.get(user_id)
        if not user_connections:
            return False
        user_connections.discard(ws)
        chat_connections = self.active_chats.get(user_id, {})
        chat_connections.pop(ws, None)
        if not chat_connections:
            self.active_chats.pop(user_id, None)
        if user_connections:
            return False
        self.active.pop(user_id, None)
        return True

    async def send_to(self, user_id: str, data: dict):
        sockets = list(self.active.get(user_id, set()))
        stale: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(data)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(user_id, ws)

    async def send_to_many(self, user_ids: list[str], data: dict):
        for user_id in set(user_ids):
            await self.send_to(user_id, data)

    def is_online(self, user_id: str) -> bool:
        return bool(self.active.get(user_id))

    def active_user_ids(self) -> list[str]:
        return list(self.active.keys())

    def set_active_chat(self, user_id: str, ws: WebSocket, chat_id: str | None):
        if ws in self.active_chats.get(user_id, {}):
            self.active_chats[user_id][ws] = chat_id

    def is_viewing_chat(self, user_id: str, chat_id: str) -> bool:
        return chat_id in self.active_chats.get(user_id, {}).values()


manager = ConnectionManager()


class SendMessageInput(BaseModel):
    task_id: str
    text: str


class EditMessageInput(BaseModel):
    text: str


class ReactionInput(BaseModel):
    emoji: str = Field(..., min_length=1, max_length=8)


class ChatThemeInput(BaseModel):
    theme: str = Field(..., pattern="^(default|ocean|purple|sunset|dark|amoled)$")


class ChatMuteInput(BaseModel):
    muted: bool


class DisappearingMessagesInput(BaseModel):
    seconds: int


DISAPPEARING_DURATIONS = {0, 86_400, 604_800, 7_776_000}


def parse_object_id(value: str, detail: str = "Chat not found") -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail=detail) from exc


def validate_task_message_text(text: str) -> str:
    clean_text = text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if PHONE_PATTERN.search(clean_text):
        raise HTTPException(status_code=400, detail="Phone numbers are not allowed in task chat")
    for pattern in BLOCKED_TEXT_PATTERNS:
        if pattern.search(clean_text):
            raise HTTPException(status_code=400, detail="Contact details and price discussion are not allowed in task chat")
    for pattern in ABUSIVE_TEXT_PATTERNS:
        if pattern.search(clean_text):
            raise HTTPException(status_code=400, detail="Abusive messages are not allowed")
    for pattern in SPAM_TEXT_PATTERNS:
        if pattern.search(clean_text):
            raise HTTPException(status_code=400, detail="Spam-like messages are not allowed")
    return clean_text


def get_suspicious_flag_reason(text: str) -> str:
    clean_text = (text or "").strip()
    if not clean_text:
        return ""
    for pattern in SUSPICIOUS_TEXT_PATTERNS:
        if pattern.search(clean_text):
            return "Suspicious external contact or link pattern"
    if clean_text.count("!") >= 4:
        return "Aggressive promotional formatting"
    return ""


async def is_blocked_between_users(db, first_user_id: str, second_user_id: str) -> bool:
    users = []
    async for user in db.users.find(
        {"_id": {"$in": [ObjectId(first_user_id), ObjectId(second_user_id)]}},
        {"blocked_user_ids": 1},
    ):
        users.append(user)
    for user in users:
        blocked_user_ids = [str(item) for item in user.get("blocked_user_ids", [])]
        if str(user["_id"]) == first_user_id and second_user_id in blocked_user_ids:
            return True
        if str(user["_id"]) == second_user_id and first_user_id in blocked_user_ids:
            return True
    return False


async def enforce_not_blocked(db, first_user_id: str, second_user_id: str) -> None:
    if await is_blocked_between_users(db, first_user_id, second_user_id):
        raise HTTPException(status_code=403, detail="This conversation is unavailable because one of the users has been blocked")


def serialize_chat(chat: dict):
    c = {**chat}
    c["id"] = str(c.pop("_id"))
    c["participants"] = [str(p) for p in c.get("participants", [])]
    return c


def serialize_datetime(value):
    if not isinstance(value, datetime):
        return value or ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_message(message: dict, current_user_id: str | None = None) -> dict:
    hidden_for = message.get("hidden_for", [])
    current_user_deleted = current_user_id in hidden_for if current_user_id else False
    deleted_for_everyone = bool(message.get("deleted_for_everyone", message.get("is_deleted", False)))
    return {
        "id": str(message.get("_id", message.get("id", ""))),
        "chatId": message.get("chat_id", ""),
        "taskId": message.get("task_id", ""),
        "text": message.get("text", ""),
        "messageType": message.get("message_type", "text"),
        "imageUrl": message.get("image_url", ""),
        "attachmentUrl": message.get("attachment_url", message.get("image_url", "")),
        "fileName": message.get("file_name", ""),
        "mimeType": message.get("mime_type", ""),
        "senderId": message.get("sender_id", ""),
        "senderName": message.get("sender_name", ""),
        "createdAt": serialize_datetime(message.get("created_at")),
        "status": message.get("status", "sent"),
        "isEdited": bool(message.get("is_edited", False)),
        "expiresAt": serialize_datetime(message.get("expires_at")),
        "isDeleted": deleted_for_everyone,
        "deleted_for_everyone": deleted_for_everyone,
        "deletedForCurrentUser": current_user_deleted,
        "isFlagged": bool(message.get("is_flagged", False)),
        "flagReason": message.get("flag_reason", ""),
        "reactions": message.get("reactions", []),
    }


def configure_cloudinary() -> None:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()
    if api_key.startswith("cloudinary://"):
        raise HTTPException(status_code=500, detail="Invalid Cloudinary configuration")
    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Cloudinary is not configured")
    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)


def get_chat_preview_text(message: dict) -> str:
    if message.get("deleted_for_everyone") or message.get("is_deleted"):
        return message.get("text", "This message was deleted")
    if message.get("message_type") == "image":
        return "Photo"
    if message.get("message_type") == "file":
        return message.get("file_name") or "Attachment"
    return message.get("text", "")


async def load_task_for_chat(db, task_id: str, current_user_id: str):
    task = await db.tasks.find_one({"_id": parse_object_id(task_id, "Task not found")})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    participants = {task.get("created_by"), task.get("assigned_to")}
    if current_user_id not in participants:
        raise HTTPException(status_code=403, detail="Not authorized for this task chat")
    if task.get("status") == "open" or not task.get("assigned_to"):
        raise HTTPException(status_code=403, detail="Task chat is available only after task acceptance")
    return task


async def get_or_create_task_chat(db, task: dict):
    task_id = str(task["_id"])
    participants = [task["created_by"], task["assigned_to"]]
    chat = await db.chats.find_one({"task_id": task_id})
    if not chat:
        result = await db.chats.insert_one(
            {
                "task_id": task_id,
                "participants": participants,
                "last_message": "",
                "last_message_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
            }
        )
        chat = await db.chats.find_one({"_id": result.inserted_id})
    return chat


async def update_chat_preview(db, chat_id: str):
    latest_visible = await db.messages.find_one({"chat_id": chat_id}, sort=[("created_at", -1)])
    if latest_visible:
        last_message = get_chat_preview_text(latest_visible)
        last_message_at = latest_visible.get("created_at", datetime.now(timezone.utc))
        last_message_sender_id = latest_visible.get("sender_id", "")
    else:
        last_message = ""
        last_message_at = datetime.now(timezone.utc)
        last_message_sender_id = ""
    await db.chats.update_one(
        {"_id": ObjectId(chat_id)},
        {"$set": {"last_message": last_message, "last_message_at": last_message_at, "last_message_sender_id": last_message_sender_id}},
    )


async def broadcast_message_event(chat: dict, event_type: str, message: dict):
    participants = [str(participant) for participant in chat.get("participants", [])]
    await manager.send_to_many(
        participants,
        {
            "type": event_type,
            "chatId": str(chat["_id"]),
            "message": serialize_message(message),
        },
    )


async def create_message(
    db,
    *,
    chat_id: str,
    task_id: str,
    sender_id: str,
    sender_name: str,
    receiver_id: str,
    text: str,
    message_type: str = "text",
    image_url: str = "",
    attachment_url: str = "",
    file_name: str = "",
    mime_type: str = "",
):
    created_at = datetime.now(timezone.utc)
    status = "delivered" if receiver_id in manager.active else "sent"
    chat_settings = await db.chats.find_one({"_id": ObjectId(chat_id)}, {"disappearing_seconds": 1}) or {}
    disappearing_seconds = int(chat_settings.get("disappearing_seconds", 0) or 0)
    flag_reason = get_suspicious_flag_reason(text)
    msg_doc = {
        "chat_id": chat_id,
        "task_id": task_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "text": text,
        "message_type": message_type,
        "image_url": image_url,
        "attachment_url": attachment_url,
        "file_name": file_name,
        "mime_type": mime_type,
        "created_at": created_at,
        **({"expires_at": created_at + timedelta(seconds=disappearing_seconds)} if disappearing_seconds > 0 else {}),
        "status": status,
        "is_edited": False,
        "is_deleted": False,
        "deleted_for_everyone": False,
        "hidden_for": [],
        "is_flagged": bool(flag_reason),
        "flag_reason": flag_reason,
        "reactions": [],
    }
    result = await db.messages.insert_one(msg_doc)
    msg_doc["_id"] = result.inserted_id
    await db.chats.update_one(
        {"_id": ObjectId(chat_id)},
        {"$set": {"last_message": get_chat_preview_text(msg_doc), "last_message_at": created_at, "last_message_sender_id": sender_id}},
    )
    chat = await db.chats.find_one({"_id": ObjectId(chat_id)}, {"muted_for": 1}) or {}
    receiver = await db.users.find_one({"_id": ObjectId(receiver_id)}, {"notification_preferences.messages": 1}) or {}
    muted = receiver_id in [str(item) for item in chat.get("muted_for", [])]
    notifications_enabled = (receiver.get("notification_preferences") or {}).get("messages", True)
    if notifications_enabled and not muted and not manager.is_viewing_chat(receiver_id, chat_id):
        sender = await db.users.find_one({"_id": ObjectId(sender_id)}, {"avatar_url": 1, "avatar": 1})
        preview = get_chat_preview_text(msg_doc)[:240]
        await db.notifications.insert_one({"user_id": receiver_id, "sender_id": sender_id, "sender_avatar": (sender or {}).get("avatar_url") or (sender or {}).get("avatar", ""), "type": "message", "title": sender_name, "message": preview, "reference_id": chat_id, "read": False, "created_at": created_at})
        await send_push_notification(db, receiver_id, sender_name, preview, {"type": "message", "chatId": chat_id})
    return msg_doc


async def broadcast_presence(user_id: str, is_online: bool, last_seen_at: datetime | None = None):
    recipients = [active_user_id for active_user_id in manager.active_user_ids() if active_user_id != user_id]
    if recipients:
        await manager.send_to_many(
            recipients,
            {
                "type": "presence",
                "userId": user_id,
                "isOnline": is_online,
                "lastSeenAt": serialize_datetime(last_seen_at) if last_seen_at else None,
            },
        )


async def handle_chat_socket(ws: WebSocket):
    token = ws.query_params.get("token", "").strip()
    if not token:
        await ws.close(code=4401)
        return
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        user_id = payload.get("sub", "")
        if not user_id:
            await ws.close(code=4401)
            return
    except jwt.InvalidTokenError:
        await ws.close(code=4401)
        return

    db = get_db()
    became_online = await manager.connect(user_id, ws)
    if became_online:
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"last_seen_at": datetime.now(timezone.utc)}},
        )
        # Reconnect delivers messages queued while this user was offline.
        user_chats = db.chats.find({"participants": user_id}, {"_id": 1})
        chat_ids = [str(chat["_id"]) async for chat in user_chats]
        pending = db.messages.find(
            {
                "chat_id": {"$in": chat_ids},
                "sender_id": {"$ne": user_id},
                "status": "sent",
                "hidden_for": {"$ne": user_id},
            },
            {"_id": 1, "chat_id": 1, "sender_id": 1},
        )
        deliveries: dict[tuple[str, str], list[str]] = {}
        async for message in pending:
            await db.messages.update_one(
                {"_id": message["_id"], "status": "sent"},
                {"$set": {"status": "delivered"}},
            )
            key = (str(message["chat_id"]), str(message["sender_id"]))
            deliveries.setdefault(key, []).append(str(message["_id"]))
        for (chat_id, sender_id), message_ids in deliveries.items():
            await manager.send_to(
                sender_id,
                {"type": "messages_delivered", "chatId": chat_id, "messageIds": message_ids},
            )
        await broadcast_presence(user_id, True)
    await manager.send_to(user_id, {"type": "presence_snapshot", "onlineUserIds": manager.active_user_ids()})

    try:
        while True:
            payload = await ws.receive_json()
            if payload.get("type") == "active_chat":
                chat_id = str(payload.get("chatId", "")).strip()
                if not chat_id:
                    manager.set_active_chat(user_id, ws, None)
                    continue
                try:
                    chat = await db.chats.find_one({"_id": ObjectId(chat_id), "participants": user_id})
                except Exception:
                    chat = None
                manager.set_active_chat(user_id, ws, chat_id if chat else None)
                continue
            if payload.get("type") == "call_signal":
                chat_id = str(payload.get("chatId", "")).strip()
                receiver_id = str(payload.get("receiverId", "")).strip()
                signal_type = str(payload.get("signalType", "")).strip()
                call_type = str(payload.get("callType", "audio")).strip()
                signal = payload.get("signal")
                if signal_type not in {"offer", "answer", "ice", "end", "reject"} or call_type not in {"audio", "video"}:
                    continue
                try:
                    chat = await db.chats.find_one({"_id": ObjectId(chat_id), "context_type": "direct"})
                except Exception:
                    chat = None
                participants = [str(item) for item in (chat or {}).get("participants", [])]
                if not chat or user_id not in participants or receiver_id not in participants or receiver_id == user_id:
                    continue
                if await is_blocked_between_users(db, user_id, receiver_id):
                    continue
                await manager.send_to(receiver_id, {
                    "type": "call_signal",
                    "chatId": chat_id,
                    "fromUserId": user_id,
                    "signalType": signal_type,
                    "callType": call_type,
                    "signal": signal if isinstance(signal, dict) else None,
                })
                continue
            if payload.get("type") == "typing":
                receiver_id = str(payload.get("receiverId", "")).strip()
                chat_id = str(payload.get("chatId", "")).strip()
                try:
                    chat = await db.chats.find_one({"_id": ObjectId(chat_id), "participants": user_id})
                except Exception:
                    chat = None
                participants = [str(item) for item in (chat or {}).get("participants", [])]
                if receiver_id in participants and receiver_id != user_id and not await is_blocked_between_users(db, user_id, receiver_id):
                    await manager.send_to(
                        receiver_id,
                        {
                            "type": "typing",
                            "chatId": chat_id,
                            "userId": user_id,
                            "isTyping": bool(payload.get("isTyping", False)),
                        },
                    )
    except WebSocketDisconnect:
        became_offline = manager.disconnect(user_id, ws)
        if became_offline:
            last_seen_at = datetime.now(timezone.utc)
            await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"last_seen_at": last_seen_at}})
            await broadcast_presence(user_id, False, last_seen_at)
    except Exception:
        became_offline = manager.disconnect(user_id, ws)
        if became_offline:
            last_seen_at = datetime.now(timezone.utc)
            await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"last_seen_at": last_seen_at}})
            await broadcast_presence(user_id, False, last_seen_at)
        await ws.close(code=1011)


@router.websocket("/ws")
async def chat_socket_api(ws: WebSocket):
    await handle_chat_socket(ws)


@ws_router.websocket("/chat/ws")
async def chat_socket_alias(ws: WebSocket):
    await handle_chat_socket(ws)


@router.get("/rooms")
async def get_chat_rooms(current_user=Depends(get_current_user)):
    db = get_db()
    now = datetime.now(timezone.utc)
    viewer = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"blocked_user_ids": 1}) or {}
    blocked_by_me = {str(item) for item in viewer.get("blocked_user_ids", [])}
    cursor = db.chats.find({"participants": current_user["id"], "$or": [{"context_type": "direct"}, {"task_id": {"$regex": "^dm_"}}, {"task_id": {"$exists": True}}], "hidden_for": {"$ne": current_user["id"]}}).sort("last_message_at", -1)
    rooms = []
    async for chat in cursor:
        latest_message = await db.messages.find_one(
            {
                "chat_id": str(chat["_id"]),
                "hidden_for": {"$ne": current_user["id"]},
                "$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$gt": now}}],
            },
            {"sender_id": 1, "status": 1, "text": 1, "message_type": 1, "is_deleted": 1, "deleted_for_everyone": 1, "created_at": 1},
            sort=[("created_at", -1)],
        )
        last_message_sender_id = (latest_message or {}).get("sender_id", "")
        last_message_status = (latest_message or {}).get("status", "sent")
        last_message_id = str((latest_message or {}).get("_id", ""))
        last_message = get_chat_preview_text(latest_message) if latest_message else ""
        last_message_at = (latest_message or {}).get("created_at", chat.get("last_message_at", now))
        task_id = str(chat.get("task_id", ""))
        if chat.get("context_type") == "direct" or task_id.startswith("dm_"):
            other_id = next((str(p) for p in chat.get("participants", []) if str(p) != current_user["id"]), "")
            if not other_id:
                continue
            other_user = await db.users.find_one({"_id": ObjectId(other_id)}, {"name": 1, "avatar_url": 1, "avatar": 1, "uid": 1, "username": 1, "last_seen_at": 1, "blocked_user_ids": 1})
            their_blocks = {str(item) for item in (other_user or {}).get("blocked_user_ids", [])}
            if current_user["id"] in their_blocks:
                continue
            blocked = other_id in blocked_by_me
            c = serialize_chat(chat); c["direct"] = True
            c["blockedByMe"] = blocked
            c["last_message_sender_id"] = last_message_sender_id
            c["last_message_status"] = last_message_status
            c["last_message_id"] = last_message_id
            c["last_message"] = last_message
            c["last_message_at"] = serialize_datetime(last_message_at)
            c["muted"] = current_user["id"] in [str(item) for item in chat.get("muted_for", [])]
            c["other_user"] = {"id": other_id, "name": (other_user or {}).get("name", "User"), "avatar_url": (other_user or {}).get("avatar_url") or (other_user or {}).get("avatar", ""), "uid": (other_user or {}).get("uid", ""), "username": (other_user or {}).get("username", ""), "isOnline": manager.is_online(other_id), "lastSeenAt": serialize_datetime((other_user or {}).get("last_seen_at"))}
            c["unread_count"] = 0 if blocked else await db.messages.count_documents({"chat_id": c["id"], "sender_id": {"$ne": current_user["id"]}, "status": {"$in": ["sent", "delivered"]}, "hidden_for": {"$ne": current_user["id"]}, "$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$gt": now}}]})
            rooms.append(c)
            continue
        try:
            task_object_id = ObjectId(task_id)
        except InvalidId:
            continue
        task = await db.tasks.find_one({"_id": task_object_id})
        if not task or task.get("status") == "open" or current_user["id"] not in [task.get("created_by"), task.get("assigned_to")]:
            continue
        c = serialize_chat(chat)
        c["last_message_sender_id"] = last_message_sender_id
        c["last_message_status"] = last_message_status
        c["last_message_id"] = last_message_id
        c["last_message"] = last_message
        c["last_message_at"] = serialize_datetime(last_message_at)
        c["muted"] = current_user["id"] in [str(item) for item in chat.get("muted_for", [])]
        other_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task.get("created_by")
        other_user = await db.users.find_one(
            {"_id": ObjectId(other_id)}, {"name": 1, "avatar_url": 1, "uid": 1, "last_seen_at": 1, "blocked_user_ids": 1}
        )
        if current_user["id"] in {str(item) for item in (other_user or {}).get("blocked_user_ids", [])}:
            continue
        blocked = other_id in blocked_by_me
        c["blockedByMe"] = blocked
        c["task"] = {
            "id": str(task["_id"]),
            "title": task.get("title", "Task"),
            "deadline": task.get("deadline", ""),
            "status": task.get("status", "assigned"),
            "price": task.get("price", 0),
        }
        context = task.get("chat_context") or task.get("chatContext")
        if isinstance(context, dict):
            c["context"] = context
        if other_user:
            c["other_user"] = {
                "id": other_id,
                "name": other_user.get("name", "User"),
                "avatar_url": other_user.get("avatar_url", ""),
                "uid": other_user.get("uid", ""),
                "isOnline": manager.is_online(other_id),
                "lastSeenAt": serialize_datetime(other_user.get("last_seen_at")),
            }
        c["unread_count"] = 0 if blocked else await db.messages.count_documents(
            {
                "chat_id": c["id"],
                "sender_id": {"$ne": current_user["id"]},
                "status": {"$in": ["sent", "delivered"]},
                "hidden_for": {"$ne": current_user["id"]},
                "$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$gt": now}}],
            }
        )
        rooms.append(c)
    return {"rooms": rooms}


@router.get("/ice-servers")
async def get_ice_servers(current_user=Depends(get_current_user)):
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    turn_urls = []
    for raw_url in os.getenv("TURN_URLS", "").split(","):
        url = raw_url.strip().lower()
        if not url:
            continue
        if not re.fullmatch(r"(?:stun|stuns|turn|turns):[^\s,]+", url):
            logger.warning("Ignoring malformed ICE server URL from TURN_URLS")
            continue
        turn_urls.append(url)
    if turn_urls:
        turn_server = {"urls": turn_urls}
        username = os.getenv("TURN_USERNAME", "").strip()
        credential = os.getenv("TURN_CREDENTIAL", "").strip()
        if username and credential:
            turn_server.update({"username": username, "credential": credential})
        servers.append(turn_server)
    return {"iceServers": servers}


@router.post("/direct/{user_id}")
async def open_direct_chat(user_id: str, current_user=Depends(get_current_user)):
    db = get_db(); target = parse_object_id(user_id, "User not found"); viewer = current_user["id"]
    target_user = await db.users.find_one({"_id": target, "is_banned": {"$ne": True}}, {"name": 1, "blocked_user_ids": 1})
    if not target_user: raise HTTPException(status_code=404, detail="User not found")
    other_id = str(target)
    if viewer == other_id: raise HTTPException(status_code=400, detail="You cannot message yourself")
    await enforce_not_blocked(db, viewer, other_id)
    participants = sorted([viewer, other_id]); key = "dm_" + "_".join(participants)
    chat = await db.chats.find_one_and_update({"task_id": key}, {"$setOnInsert": {"task_id": key, "context_type": "direct", "participants": participants, "last_message": "", "last_message_at": datetime.now(timezone.utc), "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}}, upsert=True, return_document=ReturnDocument.AFTER)
    await db.chats.update_one({"_id": chat["_id"]}, {"$pull": {"hidden_for": viewer}})
    return {"chat_id": str(chat["_id"])}


@router.delete("/rooms/{chat_id}")
async def hide_chat(chat_id: str, current_user=Depends(get_current_user)):
    db = get_db(); chat = await db.chats.find_one({"_id": parse_object_id(chat_id), "participants": current_user["id"]})
    if not chat: raise HTTPException(status_code=404, detail="Chat not found")
    await db.chats.update_one({"_id": chat["_id"]}, {"$addToSet": {"hidden_for": current_user["id"]}})
    return {"hidden": True}


@router.patch("/rooms/{chat_id}/theme")
async def update_chat_theme(chat_id: str, data: ChatThemeInput, current_user=Depends(get_current_user)):
    chat = await get_db().chats.find_one_and_update({"_id": parse_object_id(chat_id), "participants": current_user["id"]}, {"$set": {"theme": data.theme, "updated_at": datetime.now(timezone.utc)}}, return_document=ReturnDocument.AFTER)
    if not chat: raise HTTPException(status_code=404, detail="Chat not found")
    return {"theme": chat.get("theme", "default")}


@router.patch("/rooms/{chat_id}/mute")
async def update_chat_mute(chat_id: str, data: ChatMuteInput, current_user=Depends(get_current_user)):
    db = get_db()
    chat = await db.chats.find_one({"_id": parse_object_id(chat_id), "participants": current_user["id"]})
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    update = {"$addToSet": {"muted_for": current_user["id"]}} if data.muted else {"$pull": {"muted_for": current_user["id"]}}
    await db.chats.update_one({"_id": chat["_id"]}, update)
    return {"muted": data.muted}


@router.patch("/rooms/{chat_id}/disappearing")
async def update_disappearing_messages(chat_id: str, data: DisappearingMessagesInput, current_user=Depends(get_current_user)):
    if data.seconds not in DISAPPEARING_DURATIONS:
        raise HTTPException(status_code=400, detail="Choose off, 24 hours, 7 days, or 90 days")
    chat = await get_db().chats.find_one_and_update(
        {"_id": parse_object_id(chat_id), "participants": current_user["id"]},
        {"$set": {"disappearing_seconds": data.seconds, "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"seconds": int(chat.get("disappearing_seconds", 0) or 0)}


@router.delete("/rooms/{chat_id}/messages")
async def clear_chat_messages(chat_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    chat = await db.chats.find_one({"_id": parse_object_id(chat_id), "participants": current_user["id"]})
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    result = await db.messages.update_many(
        {"chat_id": chat_id, "hidden_for": {"$ne": current_user["id"]}},
        {"$addToSet": {"hidden_for": current_user["id"]}},
    )
    await manager.send_to(current_user["id"], {"type": "chat_cleared", "chatId": chat_id})
    return {"cleared": result.modified_count}


@router.post("/direct/{chat_id}/send")
async def send_direct_message(chat_id: str, data: EditMessageInput, current_user=Depends(get_current_user)):
    db = get_db(); chat = await db.chats.find_one({"_id": parse_object_id(chat_id), "context_type": "direct", "participants": current_user["id"]})
    if not chat: raise HTTPException(status_code=404, detail="Conversation not found")
    receiver = next((str(p) for p in chat.get("participants", []) if str(p) != current_user["id"]), "")
    await enforce_not_blocked(db, current_user["id"], receiver)
    text = data.text.strip()
    if not text or len(text) > 5000: raise HTTPException(status_code=400, detail="Message must contain 1 to 5000 characters")
    msg = await create_message(db, chat_id=str(chat["_id"]), task_id="", sender_id=current_user["id"], sender_name=current_user.get("name", "User"), receiver_id=receiver, text=text)
    await broadcast_message_event(chat, "message_created", msg)
    await db.chats.update_one({"_id": chat["_id"]}, {"$set": {"updated_at": datetime.now(timezone.utc)}, "$pull": {"hidden_for": current_user["id"]}})
    return {"message": "Sent", "chat_id": str(chat["_id"]), "sent_message": serialize_message(msg, current_user["id"])}


@router.get("/messages/{chat_id}")
async def get_messages(chat_id: str, page: int = 1, limit: int = 50, current_user=Depends(get_current_user)):
    db = get_db()
    chat = await db.chats.find_one({"_id": parse_object_id(chat_id)})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")
    participants = [str(participant) for participant in chat.get("participants", [])]
    other_user_id = next((participant for participant in participants if participant != current_user["id"]), "")
    if other_user_id:
        await enforce_not_blocked(db, current_user["id"], other_user_id)
    if chat.get("context_type") != "direct":
        await load_task_for_chat(db, chat.get("task_id", ""), current_user["id"])

    skip = (page - 1) * limit
    newly_seen = []
    now = datetime.now(timezone.utc)
    async for message in db.messages.find(
        {
            "chat_id": chat_id,
            "sender_id": {"$ne": current_user["id"]},
            "status": {"$in": ["sent", "delivered"]},
            "hidden_for": {"$ne": current_user["id"]},
            "$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$gt": now}}],
        }, {"_id": 1, "sender_id": 1}
    ):
        newly_seen.append(message)
    if newly_seen:
        await db.messages.update_many(
            {"_id": {"$in": [message["_id"] for message in newly_seen]}},
            {"$set": {"status": "seen"}},
        )
    if newly_seen:
        await manager.send_to_many(
            [str(participant) for participant in chat.get("participants", []) if str(participant) != current_user["id"]],
            {
                "type": "messages_seen",
                "chatId": chat_id,
                "seenBy": current_user["id"],
                "messageIds": [str(message["_id"]) for message in newly_seen],
            },
        )
    # Return updated statuses so the opened conversation shows read receipts immediately.
    cursor = (
        db.messages.find({
            "chat_id": chat_id,
            "hidden_for": {"$ne": current_user["id"]},
            "$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$gt": now}}],
        })
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    messages = []
    async for msg in cursor:
        messages.append(serialize_message(msg, current_user["id"]))
    return {"messages": list(reversed(messages))}


@router.post("/send")
async def send_message(data: SendMessageInput, current_user=Depends(get_current_user)):
    db = get_db()
    clean_text = validate_task_message_text(data.text)
    task = await load_task_for_chat(db, data.task_id, current_user["id"])
    chat = await get_or_create_task_chat(db, task)
    task_id = str(task["_id"])
    receiver_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task.get("created_by")
    await enforce_not_blocked(db, current_user["id"], receiver_id)
    msg_doc = await create_message(
        db,
        chat_id=str(chat["_id"]),
        task_id=task_id,
        sender_id=current_user["id"],
        sender_name=current_user["name"],
        receiver_id=receiver_id,
        text=clean_text,
    )
    await broadcast_message_event(chat, "message_created", msg_doc)
    return {"message": "Sent", "chat_id": str(chat["_id"]), "sent_message": serialize_message(msg_doc, current_user["id"])}


@router.post("/send-image")
async def send_image_message(
    task_id: Optional[str] = Form(None),
    chat_id: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    if image is None or not image.filename:
        raise HTTPException(status_code=400, detail="Image is required")
    content_type = (image.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    db = get_db()
    task = None
    chat = None
    if chat_id:
        chat = await db.chats.find_one({"_id": parse_object_id(chat_id, "Conversation not found"), "context_type": "direct", "participants": current_user["id"]})
        if not chat:
            raise HTTPException(status_code=404, detail="Conversation not found")
        receiver_id = next((str(participant) for participant in chat.get("participants", []) if str(participant) != current_user["id"]), "")
    elif task_id:
        task = await load_task_for_chat(db, task_id, current_user["id"])
        receiver_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task["created_by"]
    else:
        raise HTTPException(status_code=400, detail="A conversation is required")
    await enforce_not_blocked(db, current_user["id"], receiver_id)
    configure_cloudinary()
    try:
        image.file.seek(0)
        upload_result = cloudinary.uploader.upload(image.file, resource_type="image", folder="notesphere/chat")
        image_url = (upload_result.get("secure_url") or "").strip()
        if not image_url:
            raise HTTPException(status_code=500, detail="Image upload failed")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Chat image upload failed for user_id=%s: %s", current_user["id"], exc)
        raise HTTPException(status_code=500, detail="Image upload failed") from exc
    finally:
        await image.close()

    if task:
        chat = await get_or_create_task_chat(db, task)
        receiver_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task["created_by"]
        task_id_value = str(task["_id"])
    else:
        task_id_value = ""
    msg_doc = await create_message(
        db,
        chat_id=str(chat["_id"]),
        task_id=task_id_value,
        sender_id=current_user["id"],
        sender_name=current_user["name"],
        receiver_id=receiver_id,
        text="",
        message_type="image",
        image_url=image_url,
    )
    await broadcast_message_event(chat, "message_created", msg_doc)
    return {"message": "Sent", "chat_id": str(chat["_id"]), "sent_message": serialize_message(msg_doc, current_user["id"])}


@router.post("/send-attachment")
async def send_attachment_message(
    task_id: Optional[str] = Form(None),
    chat_id: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    if attachment is None or not attachment.filename:
        raise HTTPException(status_code=400, detail="Attachment is required")

    db = get_db()
    task = None
    chat = None
    if chat_id:
        chat = await db.chats.find_one({"_id": parse_object_id(chat_id, "Conversation not found"), "context_type": "direct", "participants": current_user["id"]})
        if not chat:
            raise HTTPException(status_code=404, detail="Conversation not found")
        receiver_id = next((str(participant) for participant in chat.get("participants", []) if str(participant) != current_user["id"]), "")
    elif task_id:
        task = await load_task_for_chat(db, task_id, current_user["id"])
        receiver_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task["created_by"]
    else:
        raise HTTPException(status_code=400, detail="A conversation is required")
    await enforce_not_blocked(db, current_user["id"], receiver_id)
    configure_cloudinary()
    try:
        attachment.file.seek(0)
        upload_result = cloudinary.uploader.upload(
            attachment.file,
            resource_type="auto",
            folder="notesphere/chat",
            public_id=os.path.splitext(attachment.filename)[0],
        )
        attachment_url = (upload_result.get("secure_url") or "").strip()
        if not attachment_url:
            raise HTTPException(status_code=500, detail="Attachment upload failed")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Task chat attachment upload failed for user_id=%s: %s", current_user["id"], exc)
        raise HTTPException(status_code=500, detail="Attachment upload failed") from exc
    finally:
        await attachment.close()

    if task:
        chat = await get_or_create_task_chat(db, task)
        task_id_value = str(task["_id"])
    else:
        task_id_value = ""
    message_type = "image" if (attachment.content_type or "").lower().startswith("image/") else "file"
    msg_doc = await create_message(
        db,
        chat_id=str(chat["_id"]),
        task_id=task_id_value,
        sender_id=current_user["id"],
        sender_name=current_user["name"],
        receiver_id=receiver_id,
        text="",
        message_type=message_type,
        image_url=attachment_url if message_type == "image" else "",
        attachment_url=attachment_url,
        file_name=attachment.filename,
        mime_type=attachment.content_type or "",
    )
    await broadcast_message_event(chat, "message_created", msg_doc)
    return {"message": "Sent", "chat_id": str(chat["_id"]), "sent_message": serialize_message(msg_doc, current_user["id"])}


@router.patch("/messages/{message_id}")
async def edit_message(message_id: str, data: EditMessageInput, current_user=Depends(get_current_user)):
    db = get_db()
    message = await db.messages.find_one({"_id": parse_object_id(message_id, "Message not found")})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.get("sender_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only sender can edit this message")
    if message.get("is_deleted"):
        raise HTTPException(status_code=400, detail="Deleted message cannot be edited")
    if message.get("message_type") == "image":
        raise HTTPException(status_code=400, detail="Attachment messages cannot be edited")
    if message.get("message_type") == "file":
        raise HTTPException(status_code=400, detail="Attachment messages cannot be edited")
    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"], "Chat not found")})
    participants = [str(participant) for participant in (chat or {}).get("participants", [])]
    if not chat or current_user["id"] not in participants:
        raise HTTPException(status_code=403, detail="Not authorized")
    other_user_id = next((participant for participant in participants if participant != current_user["id"]), "")
    if other_user_id:
        await enforce_not_blocked(db, current_user["id"], other_user_id)
    if chat.get("context_type") == "direct":
        if not data.text.strip() or len(data.text.strip()) > 5000:
            raise HTTPException(status_code=400, detail="Message must contain 1 to 5000 characters")
    else:
        await load_task_for_chat(db, message.get("task_id", ""), current_user["id"])
        validate_task_message_text(data.text)

    updated_message = await db.messages.find_one_and_update(
        {"_id": parse_object_id(message_id, "Message not found")},
        {
            "$set": {
                "text": data.text.strip(),
                "is_edited": True,
                "is_flagged": bool(get_suspicious_flag_reason(data.text.strip())),
                "flag_reason": get_suspicious_flag_reason(data.text.strip()),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    await update_chat_preview(db, message["chat_id"])
    await broadcast_message_event(chat, "message_updated", updated_message)
    return {"message": serialize_message(updated_message, current_user["id"])}


@router.delete("/messages/{message_id}")
async def delete_message(message_id: str, scope: str = "everyone", current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(message_id, "Message not found")
    message = await db.messages.find_one({"_id": object_id})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"], "Chat not found")})
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    participants = [str(participant) for participant in chat.get("participants", [])]
    if current_user["id"] not in participants:
        raise HTTPException(status_code=403, detail="Not authorized")
    other_user_id = next((participant for participant in participants if participant != current_user["id"]), "")
    if other_user_id:
        await enforce_not_blocked(db, current_user["id"], other_user_id)
    if chat.get("context_type") != "direct":
        await load_task_for_chat(db, message.get("task_id", ""), current_user["id"])
    if scope == "me":
        await db.messages.update_one({"_id": object_id}, {"$addToSet": {"hidden_for": current_user["id"]}})
        await manager.send_to(current_user["id"], {"type": "message_hidden", "chatId": message["chat_id"], "messageId": message_id})
        return {"success": True}

    if message["sender_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not allowed")
    created_at = message.get("created_at")
    if isinstance(created_at, datetime) and datetime.now(timezone.utc) - created_at > timedelta(minutes=DELETE_FOR_EVERYONE_WINDOW_MINUTES):
        raise HTTPException(status_code=400, detail="Delete for everyone window expired")

    updated_message = await db.messages.find_one_and_update(
        {"_id": object_id},
        {
            "$set": {
                "deleted_for_everyone": True,
                "is_deleted": True,
                "text": "This message was deleted",
                "message_type": "text",
                "image_url": "",
                "attachment_url": "",
                "file_name": "",
                "mime_type": "",
                "deleted_at": datetime.now(timezone.utc),
                "is_edited": False,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    await update_chat_preview(db, message["chat_id"])
    await broadcast_message_event(chat, "message_deleted", updated_message)
    return {"success": True, "message": serialize_message(updated_message, current_user["id"])}


@router.get("/users")
async def get_users_for_chat(current_user=Depends(get_current_user)):
    return {"users": []}


@router.post("/messages/{message_id}/reactions")
async def add_or_replace_reaction(message_id: str, data: ReactionInput, current_user=Depends(get_current_user)):
    db = get_db()
    message = await db.messages.find_one({"_id": parse_object_id(message_id, "Message not found")})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    await load_task_for_chat(db, message.get("task_id", ""), current_user["id"])
    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"], "Chat not found")})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")
    emoji = data.emoji.strip()
    if not emoji:
        raise HTTPException(status_code=400, detail="Reaction cannot be empty")
    reactions = [reaction for reaction in message.get("reactions", []) if reaction.get("userId") != current_user["id"]]
    reactions.append({"userId": current_user["id"], "emoji": emoji})
    updated_message = await db.messages.find_one_and_update(
        {"_id": parse_object_id(message_id, "Message not found")},
        {"$set": {"reactions": reactions}},
        return_document=ReturnDocument.AFTER,
    )
    await broadcast_message_event(chat, "message_updated", updated_message)
    return {"message": serialize_message(updated_message, current_user["id"])}


@router.delete("/messages/{message_id}/reactions")
async def remove_reaction(message_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    message = await db.messages.find_one({"_id": parse_object_id(message_id, "Message not found")})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    await load_task_for_chat(db, message.get("task_id", ""), current_user["id"])
    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"], "Chat not found")})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")
    reactions = [reaction for reaction in message.get("reactions", []) if reaction.get("userId") != current_user["id"]]
    updated_message = await db.messages.find_one_and_update(
        {"_id": parse_object_id(message_id, "Message not found")},
        {"$set": {"reactions": reactions}},
        return_document=ReturnDocument.AFTER,
    )
    await broadcast_message_event(chat, "message_updated", updated_message)
    return {"message": serialize_message(updated_message, current_user["id"])}
