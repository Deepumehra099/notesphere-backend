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

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        user_connections = self.active.setdefault(user_id, set())
        was_offline = len(user_connections) == 0
        user_connections.add(ws)
        return was_offline

    def disconnect(self, user_id: str, ws: WebSocket):
        user_connections = self.active.get(user_id)
        if not user_connections:
            return False
        user_connections.discard(ws)
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


manager = ConnectionManager()


class SendMessageInput(BaseModel):
    task_id: str
    text: str


class EditMessageInput(BaseModel):
    text: str


class ReactionInput(BaseModel):
    emoji: str = Field(..., min_length=1, max_length=8)


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
        "createdAt": message.get("created_at").isoformat() if isinstance(message.get("created_at"), datetime) else message.get("created_at", ""),
        "status": message.get("status", "sent"),
        "isEdited": bool(message.get("is_edited", False)),
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
    else:
        last_message = ""
        last_message_at = datetime.now(timezone.utc)
    await db.chats.update_one({"_id": ObjectId(chat_id)}, {"$set": {"last_message": last_message, "last_message_at": last_message_at}})


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
        {"$set": {"last_message": get_chat_preview_text(msg_doc), "last_message_at": created_at}},
    )
    return msg_doc


async def broadcast_presence(user_id: str, is_online: bool):
    recipients = [active_user_id for active_user_id in manager.active_user_ids() if active_user_id != user_id]
    if recipients:
        await manager.send_to_many(recipients, {"type": "presence", "userId": user_id, "isOnline": is_online})


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

    became_online = await manager.connect(user_id, ws)
    if became_online:
        await broadcast_presence(user_id, True)
    await manager.send_to(user_id, {"type": "presence_snapshot", "onlineUserIds": manager.active_user_ids()})

    try:
        while True:
            payload = await ws.receive_json()
            if payload.get("type") == "typing":
                receiver_id = str(payload.get("receiverId", "")).strip()
                if receiver_id:
                    await manager.send_to(
                        receiver_id,
                        {
                            "type": "typing",
                            "chatId": payload.get("chatId", ""),
                            "userId": user_id,
                            "isTyping": bool(payload.get("isTyping", False)),
                        },
                    )
    except WebSocketDisconnect:
        became_offline = manager.disconnect(user_id, ws)
        if became_offline:
            await broadcast_presence(user_id, False)
    except Exception:
        became_offline = manager.disconnect(user_id, ws)
        if became_offline:
            await broadcast_presence(user_id, False)
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
    cursor = db.chats.find({"participants": current_user["id"], "task_id": {"$exists": True}}).sort("last_message_at", -1)
    rooms = []
    async for chat in cursor:
        task = await db.tasks.find_one({"_id": parse_object_id(chat.get("task_id", ""), "Task not found")})
        if not task or task.get("status") == "open" or current_user["id"] not in [task.get("created_by"), task.get("assigned_to")]:
            continue
        c = serialize_chat(chat)
        other_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task.get("created_by")
        if await is_blocked_between_users(db, current_user["id"], other_id):
            continue
        other_user = await db.users.find_one({"_id": ObjectId(other_id)}, {"name": 1, "avatar_url": 1, "uid": 1})
        c["task"] = {
            "id": str(task["_id"]),
            "title": task.get("title", "Task"),
            "deadline": task.get("deadline", ""),
            "status": task.get("status", "assigned"),
            "price": task.get("price", 0),
        }
        if other_user:
            c["other_user"] = {
                "id": other_id,
                "name": other_user.get("name", "User"),
                "avatar_url": other_user.get("avatar_url", ""),
                "uid": other_user.get("uid", ""),
                "isOnline": manager.is_online(other_id),
            }
        rooms.append(c)
    return {"rooms": rooms}


@router.get("/messages/{chat_id}")
async def get_messages(chat_id: str, page: int = 1, limit: int = 50, current_user=Depends(get_current_user)):
    db = get_db()
    chat = await db.chats.find_one({"_id": parse_object_id(chat_id)})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")
    await load_task_for_chat(db, chat.get("task_id", ""), current_user["id"])
    participants = [str(participant) for participant in chat.get("participants", [])]
    other_user_id = next((participant for participant in participants if participant != current_user["id"]), "")
    if other_user_id:
        await enforce_not_blocked(db, current_user["id"], other_user_id)

    skip = (page - 1) * limit
    cursor = (
        db.messages.find({"chat_id": chat_id, "hidden_for": {"$ne": current_user["id"]}})
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    messages = []
    async for msg in cursor:
        messages.append(serialize_message(msg, current_user["id"]))

    await db.messages.update_many(
        {
            "chat_id": chat_id,
            "sender_id": {"$ne": current_user["id"]},
            "status": {"$in": ["sent", "delivered"]},
            "hidden_for": {"$ne": current_user["id"]},
        },
        {"$set": {"status": "seen"}},
    )
    await manager.send_to_many(
        [str(participant) for participant in chat.get("participants", []) if str(participant) != current_user["id"]],
        {"type": "messages_seen", "chatId": chat_id, "seenBy": current_user["id"]},
    )
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
    task_id: str = Form(...),
    image: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    if image is None or not image.filename:
        raise HTTPException(status_code=400, detail="Image is required")
    content_type = (image.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    db = get_db()
    task = await load_task_for_chat(db, task_id, current_user["id"])
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
        logger.exception("Task chat image upload failed for user_id=%s: %s", current_user["id"], exc)
        raise HTTPException(status_code=500, detail="Image upload failed") from exc
    finally:
        await image.close()

    chat = await get_or_create_task_chat(db, task)
    receiver_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task.get("created_by")
    await enforce_not_blocked(db, current_user["id"], receiver_id)
    msg_doc = await create_message(
        db,
        chat_id=str(chat["_id"]),
        task_id=str(task["_id"]),
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
    task_id: str = Form(...),
    attachment: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    if attachment is None or not attachment.filename:
        raise HTTPException(status_code=400, detail="Attachment is required")

    db = get_db()
    task = await load_task_for_chat(db, task_id, current_user["id"])
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

    chat = await get_or_create_task_chat(db, task)
    receiver_id = task["assigned_to"] if task.get("created_by") == current_user["id"] else task.get("created_by")
    await enforce_not_blocked(db, current_user["id"], receiver_id)
    message_type = "image" if (attachment.content_type or "").lower().startswith("image/") else "file"
    msg_doc = await create_message(
        db,
        chat_id=str(chat["_id"]),
        task_id=str(task["_id"]),
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
    validate_task_message_text(data.text)
    await load_task_for_chat(db, message.get("task_id", ""), current_user["id"])
    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"], "Chat not found")})
    participants = [str(participant) for participant in (chat or {}).get("participants", [])]
    other_user_id = next((participant for participant in participants if participant != current_user["id"]), "")
    if other_user_id:
        await enforce_not_blocked(db, current_user["id"], other_user_id)

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
    await load_task_for_chat(db, message.get("task_id", ""), current_user["id"])

    participants = [str(participant) for participant in chat.get("participants", [])]
    if current_user["id"] not in participants:
        raise HTTPException(status_code=403, detail="Not authorized")
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
