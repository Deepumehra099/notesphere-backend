from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from datetime import datetime, timezone
from bson import ObjectId
from utils.db import get_db
from utils.auth_utils import get_current_user
import jwt
import os
import json

router = APIRouter(prefix="/api/chat", tags=["chat"])

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active: dict[str, WebSocket] = {}

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        self.active[user_id] = ws

    def disconnect(self, user_id: str):
        self.active.pop(user_id, None)

    async def send_to(self, user_id: str, data: dict):
        ws = self.active.get(user_id)
        if ws:
            await ws.send_json(data)

manager = ConnectionManager()

class SendMessageInput(BaseModel):
    receiver_id: str
    text: str

def serialize_chat(chat):
    c = {**chat}
    c["id"] = str(c.pop("_id"))
    c["participants"] = [str(p) for p in c.get("participants", [])]
    return c

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
                {"name": 1, "avatar_url": 1}
            )
            if other_user:
                c["other_user"] = {"id": other_id[0], "name": other_user["name"], "avatar_url": other_user.get("avatar_url", "")}
        rooms.append(c)
    return {"rooms": rooms}

@router.get("/messages/{chat_id}")
async def get_messages(chat_id: str, page: int = 1, limit: int = 50, current_user=Depends(get_current_user)):
    db = get_db()
    chat = await db.chats.find_one({"_id": ObjectId(chat_id)})
    if not chat or current_user["id"] not in [str(p) for p in chat.get("participants", [])]:
        raise HTTPException(status_code=403, detail="Not authorized")
    skip = (page - 1) * limit
    cursor = db.messages.find({"chat_id": chat_id}, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
    messages = []
    async for msg in cursor:
        messages.append(msg)
    # Mark as read
    await db.messages.update_many(
        {"chat_id": chat_id, "sender_id": {"$ne": current_user["id"]}, "read": False},
        {"$set": {"read": True}}
    )
    return {"messages": list(reversed(messages))}

@router.post("/send")
async def send_message(data: SendMessageInput, current_user=Depends(get_current_user)):
    db = get_db()
    # Find or create chat room
    chat = await db.chats.find_one({
        "participants": {"$all": [current_user["id"], data.receiver_id]}
    })
    if not chat:
        result = await db.chats.insert_one({
            "participants": [current_user["id"], data.receiver_id],
            "last_message": data.text,
            "last_message_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        })
        chat_id = str(result.inserted_id)
    else:
        chat_id = str(chat["_id"])
        await db.chats.update_one(
            {"_id": chat["_id"]},
            {"$set": {"last_message": data.text, "last_message_at": datetime.now(timezone.utc)}}
        )
    msg_doc = {
        "chat_id": chat_id,
        "sender_id": current_user["id"],
        "sender_name": current_user["name"],
        "text": data.text,
        "read": False,
        "created_at": datetime.now(timezone.utc),
    }
    await db.messages.insert_one(msg_doc)
    msg_doc.pop("_id", None)
    # Send via WebSocket
    await manager.send_to(data.receiver_id, {
        "type": "new_message",
        "chat_id": chat_id,
        "message": {**msg_doc, "created_at": msg_doc["created_at"].isoformat()}
    })
    return {"message": "Sent", "chat_id": chat_id, "sent_message": {**msg_doc, "created_at": msg_doc["created_at"].isoformat()}}

@router.get("/users")
async def get_users_for_chat(current_user=Depends(get_current_user)):
    db = get_db()
    cursor = db.users.find(
        {"_id": {"$ne": ObjectId(current_user["id"])}},
        {"name": 1, "avatar_url": 1, "email": 1}
    ).limit(50)
    users = []
    async for u in cursor:
        users.append({"id": str(u["_id"]), "name": u["name"], "avatar_url": u.get("avatar_url", ""), "email": u.get("email", "")})
    return {"users": users}
