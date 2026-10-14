from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth_utils import get_current_user
from utils.db import get_db

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class NotificationPreferencesInput(BaseModel):
    messages: bool | None = None
    follows: bool | None = None


class PushTokenInput(BaseModel):
    token: str = Field(..., min_length=20, max_length=256, pattern="^(Exponent|Expo)PushToken\\[.+\\]$")


def notification_preferences(user: dict) -> dict:
    saved = user.get("notification_preferences") or {}
    return {
        "messages": bool(saved.get("messages", True)),
        "follows": bool(saved.get("follows", True)),
    }


@router.get("/preferences")
async def get_notification_preferences(current_user=Depends(get_current_user)):
    user = await get_db().users.find_one({"_id": ObjectId(current_user["id"])}, {"notification_preferences": 1}) or {}
    return {"preferences": notification_preferences(user)}


@router.patch("/preferences")
async def update_notification_preferences(data: NotificationPreferencesInput, current_user=Depends(get_current_user)):
    updates = {f"notification_preferences.{key}": value for key, value in data.dict(exclude_unset=True).items() if value is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="At least one notification preference is required")
    db = get_db()
    await db.users.update_one({"_id": ObjectId(current_user["id"])}, {"$set": updates})
    user = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"notification_preferences": 1}) or {}
    return {"preferences": notification_preferences(user)}


@router.post("/device")
async def register_notification_device(data: PushTokenInput, current_user=Depends(get_current_user)):
    await get_db().users.update_one({"_id": ObjectId(current_user["id"])}, {"$addToSet": {"expo_push_tokens": data.token}})
    return {"registered": True}


@router.delete("/device")
async def unregister_notification_device(data: PushTokenInput, current_user=Depends(get_current_user)):
    await get_db().users.update_one({"_id": ObjectId(current_user["id"])}, {"$pull": {"expo_push_tokens": data.token}})
    return {"registered": False}


def serialize_notification(item: dict) -> dict:
    created_at = item.get("created_at")
    return {
        "id": str(item.get("_id")),
        "title": item.get("title", ""),
        "body": item.get("body") or item.get("message", ""),
        "read": bool(item.get("read", False)),
        "type": item.get("type", ""),
        "senderId": item.get("sender_id", ""),
        "senderAvatar": item.get("sender_avatar", ""),
        "referenceId": item.get("reference_id", ""),
        "createdAt": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


def parse_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Notification not found") from exc


@router.get("")
@router.get("/")
async def list_notifications(current_user=Depends(get_current_user)):
    db = get_db()
    items = [
        serialize_notification(item)
        async for item in db.notifications.find({"user_id": current_user["id"]}).sort("created_at", -1)
    ]
    return {"notifications": items}


@router.patch("/{notification_id}/read")
async def mark_notification_read(notification_id: str, current_user=Depends(get_current_user)):
    result = await get_db().notifications.update_one({"_id": parse_object_id(notification_id), "user_id": current_user["id"]}, {"$set": {"read": True}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"read": True}


@router.delete("/{notification_id}")
async def delete_notification(notification_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    result = await db.notifications.delete_one({"_id": parse_object_id(notification_id), "user_id": current_user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"message": "Notification deleted"}
