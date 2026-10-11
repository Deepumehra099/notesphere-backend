from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException

from utils.auth_utils import get_current_user
from utils.db import get_db

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def serialize_notification(item: dict) -> dict:
    created_at = item.get("created_at")
    return {
        "id": str(item.get("_id")),
        "title": item.get("title", ""),
        "body": item.get("body", ""),
        "read": bool(item.get("read", False)),
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


@router.delete("/{notification_id}")
async def delete_notification(notification_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    result = await db.notifications.delete_one({"_id": parse_object_id(notification_id), "user_id": current_user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"message": "Notification deleted"}
