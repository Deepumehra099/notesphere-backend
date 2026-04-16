import logging
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
    receiver_id: str
    text: str


class EditMessageInput(BaseModel):
    text: str


class ReactionInput(BaseModel):
    emoji: str = Field(..., min_length=1, max_length=8)


def parse_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Message not found") from exc


def serialize_chat(chat: dict):
    c = {**chat}
    c["id"] = str(c.pop("_id"))
    c["participants"] = [str(p) for p in c.get("participants", [])]
    return c


async def broadcast_presence(user_id: str, is_online: bool):
    recipients = [active_user_id for active_user_id in manager.active_user_ids() if active_user_id != user_id]
    if not recipients:
        return

    await manager.send_to_many(
        recipients,
        {
            "type": "presence",
            "userId": user_id,
            "isOnline": is_online,
        },
    )


def serialize_message(message: dict, current_user_id: str | None = None) -> dict:
    hidden_for = message.get("hidden_for", [])
    current_user_deleted = current_user_id in hidden_for if current_user_id else False
    deleted_for_everyone = bool(message.get("deleted_for_everyone", message.get("is_deleted", False)))

    return {
        "id": str(message.get("_id", message.get("id", ""))),
        "chatId": message.get("chat_id", ""),
        "text": message.get("text", ""),
        "messageType": message.get("message_type", "text"),
        "imageUrl": message.get("image_url", ""),
        "senderId": message.get("sender_id", ""),
        "senderName": message.get("sender_name", ""),
        "createdAt": message.get("created_at").isoformat() if isinstance(message.get("created_at"), datetime) else message.get("created_at", ""),
        "status": message.get("status", "sent"),
        "isEdited": bool(message.get("is_edited", False)),
        "isDeleted": deleted_for_everyone,
        "deleted_for_everyone": deleted_for_everyone,
        "deletedForCurrentUser": current_user_deleted,
        "reactions": message.get("reactions", []),
    }


def configure_cloudinary() -> None:
    import os

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()

    if api_key.startswith("cloudinary://"):
        raise HTTPException(status_code=500, detail="Invalid Cloudinary configuration")

    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Cloudinary is not configured")

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )


def get_chat_preview_text(message: dict) -> str:
    if message.get("deleted_for_everyone") or message.get("is_deleted"):
        return message.get("text", "This message was deleted")
    if message.get("message_type") == "image":
        return "Photo"
    return message.get("text", "")


async def update_chat_preview(db, chat_id: str):
    latest_visible = await db.messages.find_one(
        {"chat_id": chat_id},
        sort=[("created_at", -1)],
    )

    if latest_visible:
        last_message = get_chat_preview_text(latest_visible)
        last_message_at = latest_visible.get("created_at", datetime.now(timezone.utc))
    else:
        last_message = ""
        last_message_at = datetime.now(timezone.utc)

    await db.chats.update_one(
        {"_id": ObjectId(chat_id)},
        {"$set": {"last_message": last_message, "last_message_at": last_message_at}},
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


async def get_or_create_chat(db, sender_id: str, receiver_id: str, last_message: str, created_at: datetime):
    chat = await db.chats.find_one({
        "participants": {"$all": [sender_id, receiver_id]},
    })

    if not chat:
        result = await db.chats.insert_one({
            "participants": [sender_id, receiver_id],
            "last_message": last_message,
            "last_message_at": created_at,
            "created_at": created_at,
        })
        chat_id = str(result.inserted_id)
        chat = await db.chats.find_one({"_id": result.inserted_id})
    else:
        chat_id = str(chat["_id"])
        await db.chats.update_one(
            {"_id": chat["_id"]},
            {"$set": {"last_message": last_message, "last_message_at": created_at}},
        )

    return chat, chat_id


async def create_message(
    db,
    *,
    chat_id: str,
    sender_id: str,
    sender_name: str,
    receiver_id: str,
    text: str,
    message_type: str = "text",
    image_url: str = "",
):
    created_at = datetime.now(timezone.utc)
    status = "delivered" if receiver_id in manager.active else "sent"
    msg_doc = {
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "text": text,
        "message_type": message_type,
        "image_url": image_url,
        "created_at": created_at,
        "status": status,
        "is_edited": False,
        "is_deleted": False,
        "deleted_for_everyone": False,
        "hidden_for": [],
        "reactions": [],
    }
    result = await db.messages.insert_one(msg_doc)
    msg_doc["_id"] = result.inserted_id
    return msg_doc


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

    await manager.send_to(
        user_id,
        {
            "type": "presence_snapshot",
            "onlineUserIds": manager.active_user_ids(),
        },
    )

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
    cursor = db.chats.find({"participants": current_user["id"]}).sort("last_message_at", -1)
    rooms = []
    async for chat in cursor:
        c = serialize_chat(chat)
        other_id = [p for p in c["participants"] if p != current_user["id"]]
        if other_id:
            other_user = await db.users.find_one(
                {"_id": ObjectId(other_id[0])},
                {"name": 1, "avatar_url": 1, "uid": 1},
            )
            if other_user:
                c["other_user"] = {
                    "id": other_id[0],
                    "name": other_user["name"],
                    "avatar_url": other_user.get("avatar_url", ""),
                    "uid": other_user.get("uid", ""),
                    "isOnline": manager.is_online(other_id[0]),
                }
        rooms.append(c)
    return {"rooms": rooms}


@router.get("/messages/{chat_id}")
async def get_messages(chat_id: str, page: int = 1, limit: int = 50, current_user=Depends(get_current_user)):
    db = get_db()
    chat = await db.chats.find_one({"_id": parse_object_id(chat_id)})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")

    skip = (page - 1) * limit
    cursor = db.messages.find(
        {
            "chat_id": chat_id,
            "hidden_for": {"$ne": current_user["id"]},
        }
    ).sort("created_at", -1).skip(skip).limit(limit)

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
        {
            "type": "messages_seen",
            "chatId": chat_id,
            "seenBy": current_user["id"],
        },
    )

    return {"messages": list(reversed(messages))}


@router.post("/send")
async def send_message(data: SendMessageInput, current_user=Depends(get_current_user)):
    db = get_db()
    clean_text = data.text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    chat, chat_id = await get_or_create_chat(
        db,
        sender_id=current_user["id"],
        receiver_id=data.receiver_id,
        last_message=clean_text,
        created_at=datetime.now(timezone.utc),
    )
    msg_doc = await create_message(
        db,
        chat_id=chat_id,
        sender_id=current_user["id"],
        sender_name=current_user["name"],
        receiver_id=data.receiver_id,
        text=clean_text,
    )

    await broadcast_message_event(chat, "message_created", msg_doc)

    return {
        "message": "Sent",
        "chat_id": chat_id,
        "sent_message": serialize_message(msg_doc, current_user["id"]),
    }


@router.post("/send-image")
async def send_image_message(
    receiver_id: str = Form(...),
    image: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    if image is None or not image.filename:
        raise HTTPException(status_code=400, detail="Image is required")

    content_type = (image.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    configure_cloudinary()

    try:
        image.file.seek(0)
        upload_result = cloudinary.uploader.upload(
            image.file,
            resource_type="image",
            folder="notesphere/chat",
        )
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

    db = get_db()
    chat, chat_id = await get_or_create_chat(
        db,
        sender_id=current_user["id"],
        receiver_id=receiver_id,
        last_message="Photo",
        created_at=datetime.now(timezone.utc),
    )
    msg_doc = await create_message(
        db,
        chat_id=chat_id,
        sender_id=current_user["id"],
        sender_name=current_user["name"],
        receiver_id=receiver_id,
        text="",
        message_type="image",
        image_url=image_url,
    )

    await broadcast_message_event(chat, "message_created", msg_doc)

    return {
        "message": "Sent",
        "chat_id": chat_id,
        "sent_message": serialize_message(msg_doc, current_user["id"]),
    }


@router.patch("/messages/{message_id}")
async def edit_message(message_id: str, data: EditMessageInput, current_user=Depends(get_current_user)):
    db = get_db()
    message = await db.messages.find_one({"_id": parse_object_id(message_id)})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.get("sender_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only sender can edit this message")
    if message.get("is_deleted"):
        raise HTTPException(status_code=400, detail="Deleted message cannot be edited")
    if message.get("message_type") == "image":
        raise HTTPException(status_code=400, detail="Image messages cannot be edited")

    updated_message = await db.messages.find_one_and_update(
        {"_id": parse_object_id(message_id)},
        {"$set": {"text": data.text.strip(), "is_edited": True}},
        return_document=ReturnDocument.AFTER,
    )

    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"])})
    await update_chat_preview(db, message["chat_id"])
    await broadcast_message_event(chat, "message_updated", updated_message)

    return {"message": serialize_message(updated_message, current_user["id"])}


@router.delete("/messages/{message_id}")
async def delete_message(message_id: str, scope: str = "everyone", current_user=Depends(get_current_user)):
    try:
        db = get_db()
        object_id = parse_object_id(message_id)
        message = await db.messages.find_one({"_id": object_id})
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"])})
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")

        participants = [str(participant) for participant in chat.get("participants", [])]
        if current_user["id"] not in participants:
            raise HTTPException(status_code=403, detail="Not authorized")

        if scope == "me":
            await db.messages.update_one(
                {"_id": object_id},
                {"$addToSet": {"hidden_for": current_user["id"]}},
            )
            await manager.send_to(
                current_user["id"],
                {"type": "message_hidden", "chatId": message["chat_id"], "messageId": message_id},
            )
            return {"success": True}

        if message["sender_id"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="Not allowed")

        created_at = message.get("created_at")
        if isinstance(created_at, datetime):
            if datetime.now(timezone.utc) - created_at > timedelta(minutes=DELETE_FOR_EVERYONE_WINDOW_MINUTES):
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
                    "deleted_at": datetime.now(timezone.utc),
                    "is_edited": False,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

        await update_chat_preview(db, message["chat_id"])
        await broadcast_message_event(chat, "message_deleted", updated_message)

        return {"success": True, "message": serialize_message(updated_message, current_user["id"])}
    except HTTPException:
        raise
    except Exception as exc:
        print(exc)
        raise HTTPException(status_code=500, detail="Server error") from exc


@router.get("/users")
async def get_users_for_chat(current_user=Depends(get_current_user)):
    db = get_db()
    cursor = db.users.find(
        {"_id": {"$ne": ObjectId(current_user["id"])}},
        {"name": 1, "avatar_url": 1, "email": 1, "uid": 1},
    ).limit(50)
    users = []
    async for u in cursor:
        users.append({
            "id": str(u["_id"]),
            "name": u["name"],
            "avatar_url": u.get("avatar_url", ""),
            "email": u.get("email", ""),
            "uid": u.get("uid", ""),
            "isOnline": manager.is_online(str(u["_id"])),
        })
    return {"users": users}


@router.post("/messages/{message_id}/reactions")
async def add_or_replace_reaction(message_id: str, data: ReactionInput, current_user=Depends(get_current_user)):
    db = get_db()
    message = await db.messages.find_one({"_id": parse_object_id(message_id)})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"])})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")

    emoji = data.emoji.strip()
    if not emoji:
        raise HTTPException(status_code=400, detail="Reaction cannot be empty")

    reactions = [
        reaction for reaction in message.get("reactions", [])
        if reaction.get("userId") != current_user["id"]
    ]
    reactions.append({"userId": current_user["id"], "emoji": emoji})

    updated_message = await db.messages.find_one_and_update(
        {"_id": parse_object_id(message_id)},
        {"$set": {"reactions": reactions}},
        return_document=ReturnDocument.AFTER,
    )

    await broadcast_message_event(chat, "message_updated", updated_message)
    return {"message": serialize_message(updated_message, current_user["id"])}


@router.delete("/messages/{message_id}/reactions")
async def remove_reaction(message_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    message = await db.messages.find_one({"_id": parse_object_id(message_id)})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    chat = await db.chats.find_one({"_id": parse_object_id(message["chat_id"])})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")

    reactions = [
        reaction for reaction in message.get("reactions", [])
        if reaction.get("userId") != current_user["id"]
    ]

    updated_message = await db.messages.find_one_and_update(
        {"_id": parse_object_id(message_id)},
        {"$set": {"reactions": reactions}},
        return_document=ReturnDocument.AFTER,
    )

    await broadcast_message_event(chat, "message_updated", updated_message)
    return {"message": serialize_message(updated_message, current_user["id"])}
