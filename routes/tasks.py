from datetime import datetime, timezone
from typing import Literal, Optional

from bson import ObjectId
from bson.errors import InvalidId
import logging
import os
from pathlib import Path

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import debit_wallet, ensure_wallet, hold_wallet_funds, refund_held_funds, release_held_funds

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

TaskStatus = Literal["open", "assigned", "completed"]
URGENT_TASK_FEE = 50


class CreateTaskInput(BaseModel):
    title: str = Field(..., min_length=3, max_length=160)
    description: str = Field("", max_length=2000)
    price: int = Field(..., ge=0)
    deadline: str = Field(..., min_length=1, max_length=120)
    location: str = Field("", max_length=160)
    required_skills: list[str] = []
    is_urgent: bool = False


class RateTaskInput(BaseModel):
    rating: int = Field(..., ge=1, le=5)


class ReportTaskInput(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


def get_popularity_score(task: dict) -> int:
    views = int(task.get("views", 0) or 0)
    clicks = int(task.get("clicks", 0) or 0)
    accepts = int(task.get("accepts", 0) or 0)
    return views + (clicks * 3) + (accepts * 8)


async def track_task_metric(
    db,
    task_id: ObjectId,
    *,
    event_type: Literal["view", "click", "accept"],
    user_id: str,
    task_snapshot: dict | None = None,
) -> None:
    counter_field_map = {
        "view": "views",
        "click": "clicks",
        "accept": "accepts",
    }
    counter_field = counter_field_map[event_type]

    task = task_snapshot or await db.tasks.find_one({"_id": task_id}, {"views": 1, "clicks": 1, "accepts": 1})
    if not task:
        return

    next_views = int(task.get("views", 0) or 0) + (1 if event_type == "view" else 0)
    next_clicks = int(task.get("clicks", 0) or 0) + (1 if event_type == "click" else 0)
    next_accepts = int(task.get("accepts", 0) or 0) + (1 if event_type == "accept" else 0)
    popularity_score = next_views + (next_clicks * 3) + (next_accepts * 8)
    now = datetime.now(timezone.utc)

    await db.tasks.update_one(
        {"_id": task_id},
        {
            "$inc": {counter_field: 1},
            "$set": {
                "popularity_score": popularity_score,
                "last_analytics_at": now,
            },
        },
    )
    await db.task_analytics.update_one(
        {"task_id": str(task_id)},
        {
            "$setOnInsert": {
                "task_id": str(task_id),
                "created_at": now,
            },
            "$inc": {
                counter_field: 1,
                "popularity_score": 1 if event_type == "view" else 3 if event_type == "click" else 8,
            },
            "$set": {
                "last_event_type": event_type,
                "last_event_at": now,
                "updated_at": now,
            },
            "$push": {
                "recent_events": {
                    "$each": [
                        {
                            "type": event_type,
                            "user_id": user_id,
                            "at": now,
                        }
                    ],
                    "$slice": -25,
                }
            },
        },
        upsert=True,
    )


def configure_cloudinary() -> None:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()
    if api_key.startswith("cloudinary://"):
        raise HTTPException(status_code=500, detail="Invalid Cloudinary configuration")
    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Cloudinary is not configured")
    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)


def parse_object_id(task_id: str) -> ObjectId:
    try:
        return ObjectId(task_id)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


def serialize_task(task: dict) -> dict:
    created_at = task.get("created_at")
    accepted_at = task.get("accepted_at")
    return {
        "id": str(task.get("_id", task.get("id", ""))),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "price": task.get("price", 0),
        "deadline": task.get("deadline", ""),
        "location": task.get("location", ""),
        "required_skills": task.get("required_skills", []),
        "is_urgent": bool(task.get("is_urgent", False)),
        "urgent_fee": int(task.get("urgent_fee", 0) or 0),
        "views": int(task.get("views", 0) or 0),
        "clicks": int(task.get("clicks", 0) or 0),
        "accepts": int(task.get("accepts", 0) or 0),
        "popularity_score": int(task.get("popularity_score", get_popularity_score(task)) or 0),
        "status": task.get("status", "open"),
        "created_by": task.get("created_by", ""),
        "created_by_name": task.get("created_by_name", ""),
        "created_by_verified": bool(task.get("created_by_verified", False)),
        "created_by_rating": round(float(task.get("created_by_rating", 0) or 0), 1),
        "created_by_completed_tasks": int(task.get("created_by_completed_tasks", 0) or 0),
        "assigned_to": task.get("assigned_to"),
        "assigned_to_name": task.get("assigned_to_name", ""),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "accepted_at": accepted_at.isoformat() if isinstance(accepted_at, datetime) else accepted_at,
        "completed_at": task.get("completed_at").isoformat() if isinstance(task.get("completed_at"), datetime) else task.get("completed_at"),
        "escrow_status": task.get("escrow_status", "none"),
        "escrow_amount": task.get("escrow_amount", task.get("price", 0)),
        "commission_amount": task.get("commission_amount", 0),
        "seller_payout": task.get("seller_payout", 0),
        "is_boosted": bool(task.get("is_boosted", False)),
        "boosted_at": task.get("boosted_at").isoformat() if isinstance(task.get("boosted_at"), datetime) else task.get("boosted_at"),
        "attachmentUrls": task.get("attachment_urls", []),
        "attachments": task.get("attachments", []),
        "buyer_rating": task.get("buyer_rating"),
        "buyer_rated_at": task.get("buyer_rated_at").isoformat() if isinstance(task.get("buyer_rated_at"), datetime) else task.get("buyer_rated_at"),
    }


def attach_creator_trust(task: dict, creator: dict | None) -> dict:
    if creator:
        task["created_by_verified"] = bool(creator.get("verified", False))
        task["created_by_rating"] = round(float(creator.get("task_rating", 0) or 0), 1)
        task["created_by_completed_tasks"] = int(creator.get("completed_tasks", 0) or 0)
    else:
        task["created_by_verified"] = bool(task.get("created_by_verified", False))
        task["created_by_rating"] = round(float(task.get("created_by_rating", 0) or 0), 1)
        task["created_by_completed_tasks"] = int(task.get("created_by_completed_tasks", 0) or 0)
    return task


def sanitize_skills(skills: list[str]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for skill in skills:
        value = (skill or "").strip()
        normalized = value.lower()
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        clean.append(value)
    return clean


def parse_form_skills(raw_value: str) -> list[str]:
    return sanitize_skills(raw_value.split(","))


async def hydrate_task_creators(db, tasks: list[dict]) -> list[dict]:
    creator_ids = []
    for task in tasks:
        creator_id = task.get("created_by")
        if creator_id:
            try:
                creator_ids.append(ObjectId(creator_id))
            except InvalidId:
                continue

    creator_map: dict[str, dict] = {}
    if creator_ids:
        async for user in db.users.find(
            {"_id": {"$in": creator_ids}},
            {"verified": 1, "task_rating": 1, "completed_tasks": 1},
        ):
            creator_map[str(user["_id"])] = user

    return [attach_creator_trust(task, creator_map.get(task.get("created_by", ""))) for task in tasks]


@router.post("")
async def create_task(data: CreateTaskInput, current_user=Depends(get_current_user)):
    db = get_db()
    urgent_fee = URGENT_TASK_FEE if data.is_urgent else 0
    if urgent_fee > 0:
        charged_wallet = await debit_wallet(
            db,
            user_id=current_user["id"],
            amount=urgent_fee,
            reason=f"Urgent task fee: {data.title.strip()}",
            transaction_type="spend",
            source_type="task_urgent",
            source_id=current_user["id"],
            metadata={"task_title": data.title.strip()},
        )
        if not charged_wallet:
            raise HTTPException(status_code=400, detail="Not enough wallet balance for urgent task fee")
    task_doc = {
        "title": data.title.strip(),
        "description": data.description.strip(),
        "price": data.price,
        "deadline": data.deadline.strip(),
        "location": data.location.strip(),
        "required_skills": sanitize_skills(data.required_skills),
        "is_urgent": bool(data.is_urgent),
        "urgent_fee": urgent_fee,
        "views": 0,
        "clicks": 0,
        "accepts": 0,
        "popularity_score": 0,
        "status": "open",
        "created_by": current_user["id"],
        "created_by_name": current_user.get("name", "Student"),
        "created_by_verified": bool(current_user.get("verified", False)),
        "created_by_rating": round(float(current_user.get("task_rating", 0) or 0), 1),
        "created_by_completed_tasks": int(current_user.get("completed_tasks", 0) or 0),
        "assigned_to": None,
        "assigned_to_name": "",
        "created_at": datetime.now(timezone.utc),
        "accepted_at": None,
        "completed_at": None,
        "escrow_status": "none" if data.price <= 0 else "pending",
        "escrow_amount": data.price,
        "commission_amount": 0,
        "seller_payout": 0,
        "is_boosted": False,
        "boosted_at": None,
        "attachment_urls": [],
        "attachments": [],
        "buyer_rating": None,
        "buyer_rated_at": None,
    }
    result = await db.tasks.insert_one(task_doc)
    task_doc["_id"] = result.inserted_id
    return {"message": "Task created", "task": serialize_task(task_doc)}


@router.post("/create")
async def create_task_alias(data: CreateTaskInput, current_user=Depends(get_current_user)):
    return await create_task(data, current_user)


@router.post("/create-with-attachments")
async def create_task_with_attachments(
    title: str = Form(...),
    description: str = Form(""),
    price: int = Form(...),
    deadline: str = Form(...),
    location: str = Form(""),
    required_skills: str = Form(""),
    is_urgent: bool = Form(False),
    attachments: list[UploadFile] = File(default=[]),
    current_user=Depends(get_current_user),
):
    configure_cloudinary()
    db = get_db()
    urgent_fee = URGENT_TASK_FEE if is_urgent else 0
    if urgent_fee > 0:
        charged_wallet = await debit_wallet(
            db,
            user_id=current_user["id"],
            amount=urgent_fee,
            reason=f"Urgent task fee: {title.strip()}",
            transaction_type="spend",
            source_type="task_urgent",
            source_id=current_user["id"],
            metadata={"task_title": title.strip()},
        )
        if not charged_wallet:
            raise HTTPException(status_code=400, detail="Not enough wallet balance for urgent task fee")
    uploaded_attachments = []
    for attachment in attachments:
        if not attachment.filename:
            continue
        try:
            attachment.file.seek(0)
            upload_result = cloudinary.uploader.upload(
                attachment.file,
                resource_type="auto",
                folder="notesphere/tasks",
            )
            secure_url = (upload_result.get("secure_url") or "").strip()
            if not secure_url:
                raise HTTPException(status_code=500, detail="Task attachment upload failed")
            uploaded_attachments.append(
                {
                    "url": secure_url,
                    "file_name": attachment.filename,
                    "mime_type": attachment.content_type or "",
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Task attachment upload failed for user_id=%s: %s", current_user["id"], exc)
            raise HTTPException(status_code=500, detail="Task attachment upload failed") from exc
        finally:
            await attachment.close()

    task_doc = {
        "title": title.strip(),
        "description": description.strip(),
        "price": price,
        "deadline": deadline.strip(),
        "location": location.strip(),
        "required_skills": parse_form_skills(required_skills),
        "is_urgent": bool(is_urgent),
        "urgent_fee": urgent_fee,
        "views": 0,
        "clicks": 0,
        "accepts": 0,
        "popularity_score": 0,
        "status": "open",
        "created_by": current_user["id"],
        "created_by_name": current_user.get("name", "Student"),
        "created_by_verified": bool(current_user.get("verified", False)),
        "created_by_rating": round(float(current_user.get("task_rating", 0) or 0), 1),
        "created_by_completed_tasks": int(current_user.get("completed_tasks", 0) or 0),
        "assigned_to": None,
        "assigned_to_name": "",
        "created_at": datetime.now(timezone.utc),
        "accepted_at": None,
        "completed_at": None,
        "escrow_status": "none" if price <= 0 else "pending",
        "escrow_amount": price,
        "commission_amount": 0,
        "seller_payout": 0,
        "is_boosted": False,
        "boosted_at": None,
        "attachment_urls": [item["url"] for item in uploaded_attachments],
        "attachments": uploaded_attachments,
        "buyer_rating": None,
        "buyer_rated_at": None,
    }
    result = await db.tasks.insert_one(task_doc)
    task_doc["_id"] = result.inserted_id
    return {"message": "Task created", "task": serialize_task(task_doc)}


@router.post("/{task_id}/attachments")
async def upload_task_attachment(
    task_id: str,
    attachment: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    if attachment is None or not attachment.filename:
        raise HTTPException(status_code=400, detail="Attachment is required")

    db = get_db()
    object_id = parse_object_id(task_id)
    task = await db.tasks.find_one({"_id": object_id})
    if not task or task.get("created_by") != current_user["id"]:
        raise HTTPException(status_code=404, detail="Task not found")

    configure_cloudinary()
    try:
        attachment.file.seek(0)
        upload_result = cloudinary.uploader.upload(
            attachment.file,
            resource_type="auto",
            folder="notesphere/tasks",
        )
        attachment_url = (upload_result.get("secure_url") or "").strip()
        if not attachment_url:
            raise HTTPException(status_code=500, detail="Task attachment upload failed")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Task attachment upload failed for task_id=%s: %s", task_id, exc)
        raise HTTPException(status_code=500, detail="Task attachment upload failed") from exc
    finally:
        await attachment.close()

    attachment_payload = {
        "url": attachment_url,
        "file_name": attachment.filename,
        "mime_type": attachment.content_type or "",
    }
    updated_task = await db.tasks.find_one_and_update(
        {"_id": object_id, "created_by": current_user["id"]},
        {
            "$push": {
                "attachment_urls": attachment_url,
                "attachments": attachment_payload,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {"message": "Task attachment uploaded", "task": serialize_task(updated_task), "attachment_url": attachment_url}


@router.get("")
async def get_tasks(
    page: int = 1,
    limit: int = 10,
    current_user=Depends(get_current_user),
):
    db = get_db()
    page = max(page, 1)
    limit = max(1, min(limit, 10))
    query = {
        "status": "open",
        "created_by": {"$ne": current_user["id"]},
        "$or": [{"assigned_to": None}, {"assigned_to": {"$exists": False}}],
    }
    projection = {
        "title": 1,
        "description": 1,
        "price": 1,
        "deadline": 1,
        "location": 1,
        "required_skills": 1,
        "is_urgent": 1,
        "urgent_fee": 1,
        "views": 1,
        "clicks": 1,
        "accepts": 1,
        "popularity_score": 1,
        "status": 1,
        "created_by": 1,
        "created_by_name": 1,
        "assigned_to": 1,
        "assigned_to_name": 1,
        "created_at": 1,
        "accepted_at": 1,
        "completed_at": 1,
        "escrow_status": 1,
        "escrow_amount": 1,
        "commission_amount": 1,
        "seller_payout": 1,
        "is_boosted": 1,
        "boosted_at": 1,
        "buyer_rating": 1,
        "buyer_rated_at": 1,
    }
    skip = (page - 1) * limit
    cursor = (
        db.tasks.find(query, projection)
        .sort([("is_urgent", -1), ("is_boosted", -1), ("popularity_score", -1), ("boosted_at", -1), ("created_at", -1)])
        .skip(skip)
        .limit(limit)
    )

    tasks = []
    raw_tasks = []
    async for task in cursor:
        raw_tasks.append(task)
    if raw_tasks:
        for task in raw_tasks:
            await track_task_metric(
                db,
                task["_id"],
                event_type="view",
                user_id=current_user["id"],
                task_snapshot=task,
            )
            task["views"] = int(task.get("views", 0) or 0) + 1
            task["popularity_score"] = get_popularity_score(task)
    hydrated_tasks = await hydrate_task_creators(db, raw_tasks)
    for task in hydrated_tasks:
        tasks.append(serialize_task(task))
    total = await db.tasks.count_documents(query)
    return {
        "tasks": tasks,
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit,
        "has_more": skip + len(tasks) < total,
    }


@router.get("/trending")
async def get_trending_tasks(
    limit: int = 10,
    current_user=Depends(get_current_user),
):
    db = get_db()
    limit = max(1, min(limit, 20))
    query = {
        "status": "open",
        "created_by": {"$ne": current_user["id"]},
        "$or": [{"assigned_to": None}, {"assigned_to": {"$exists": False}}],
    }
    cursor = db.tasks.find(query).sort(
        [("is_boosted", -1), ("is_urgent", -1), ("popularity_score", -1), ("created_at", -1)]
    ).limit(limit)
    raw_tasks = []
    async for task in cursor:
        raw_tasks.append(task)
    hydrated_tasks = await hydrate_task_creators(db, raw_tasks)
    return {"tasks": [serialize_task(task) for task in hydrated_tasks]}


@router.post("/{task_id}/accept")
async def accept_task(task_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)

    existing = await db.tasks.find_one({"_id": object_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")
    if existing.get("created_by") == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot accept your own task")

    price = int(existing.get("price", 0) or 0)
    buyer_wallet = await ensure_wallet(db, existing["created_by"])
    if price > 0 and int(buyer_wallet.get("available_balance", 0) or 0) < price:
        raise HTTPException(status_code=400, detail="Buyer has insufficient wallet balance")

    held_wallet = None
    if price > 0:
        held_wallet = await hold_wallet_funds(
            db,
            user_id=existing["created_by"],
            amount=price,
            reason=f"Funds held for task: {existing['title']}",
            source_type="task",
            source_id=task_id,
            counterparty_user_id=current_user["id"],
            metadata={"task_title": existing["title"]},
        )
        if not held_wallet:
            raise HTTPException(status_code=400, detail="Buyer has insufficient wallet balance")

    updated_task = await db.tasks.find_one_and_update(
        {
            "_id": object_id,
            "status": "open",
            "$or": [{"assigned_to": None}, {"assigned_to": {"$exists": False}}],
        },
        {
            "$set": {
                "status": "assigned",
                "assigned_to": current_user["id"],
                "assigned_to_name": current_user.get("name", "Student"),
                "accepted_at": datetime.now(timezone.utc),
                "escrow_status": "held" if price > 0 else "none",
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_task:
        if held_wallet and price > 0:
            await refund_held_funds(
                db,
                user_id=existing["created_by"],
                amount=price,
                reason=f"Task acceptance rolled back: {existing['title']}",
                source_type="task",
                source_id=task_id,
                metadata={"task_title": existing["title"]},
            )
        raise HTTPException(status_code=409, detail="Task already accepted")

    await db.chats.update_one(
        {"task_id": task_id},
        {
            "$setOnInsert": {
                "task_id": task_id,
                "participants": [updated_task["created_by"], current_user["id"]],
                "last_message": "",
                "last_message_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )

    await track_task_metric(
        db,
        object_id,
        event_type="accept",
        user_id=current_user["id"],
        task_snapshot=updated_task,
    )
    updated_task["accepts"] = int(updated_task.get("accepts", 0) or 0) + 1
    updated_task["popularity_score"] = get_popularity_score(updated_task)

    return {"message": "Task accepted", "task": serialize_task(updated_task)}


@router.post("/{task_id}/click")
async def track_task_click(task_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)
    task = await db.tasks.find_one({"_id": object_id}, {"status": 1, "created_by": 1, "views": 1, "clicks": 1, "accepts": 1})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await track_task_metric(
        db,
        object_id,
        event_type="click",
        user_id=current_user["id"],
        task_snapshot=task,
    )

    return {
        "message": "Task click tracked",
        "task_id": task_id,
        "clicks": int(task.get("clicks", 0) or 0) + 1,
        "popularity_score": get_popularity_score(
            {
                **task,
                "clicks": int(task.get("clicks", 0) or 0) + 1,
            }
        ),
    }


@router.post("/{task_id}/boost")
async def boost_task(task_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)
    updated_task = await db.tasks.find_one_and_update(
        {
            "_id": object_id,
            "created_by": current_user["id"],
            "status": "open",
        },
        {
            "$set": {
                "is_boosted": True,
                "boosted_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_task:
        raise HTTPException(status_code=404, detail="Open task not found for boost")

    return {"message": "Task boosted", "task": serialize_task(updated_task)}


@router.post("/{task_id}/mark-urgent")
async def mark_task_urgent(task_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)
    task = await db.tasks.find_one({"_id": object_id})
    if not task or task.get("created_by") != current_user["id"] or task.get("status") != "open":
        raise HTTPException(status_code=404, detail="Open task not found")
    if task.get("is_urgent"):
        return {"message": "Task already marked urgent", "task": serialize_task(task)}

    charged_wallet = await debit_wallet(
        db,
        user_id=current_user["id"],
        amount=URGENT_TASK_FEE,
        reason=f"Urgent task fee: {task.get('title', 'Task')}",
        transaction_type="spend",
        source_type="task_urgent",
        source_id=task_id,
        metadata={"task_title": task.get("title", "")},
    )
    if not charged_wallet:
        raise HTTPException(status_code=400, detail="Not enough wallet balance for urgent task fee")

    updated_task = await db.tasks.find_one_and_update(
        {"_id": object_id, "created_by": current_user["id"], "status": "open"},
        {"$set": {"is_urgent": True, "urgent_fee": URGENT_TASK_FEE}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_task:
        raise HTTPException(status_code=409, detail="Task changed before urgent update")

    return {"message": "Task marked urgent", "task": serialize_task(updated_task), "urgent_fee": URGENT_TASK_FEE}


@router.post("/{task_id}/complete")
async def complete_task(task_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)
    task = await db.tasks.find_one({"_id": object_id})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("created_by") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the task buyer can mark it completed")
    if task.get("status") != "assigned":
        raise HTTPException(status_code=400, detail="Task is not ready for completion")
    if not task.get("assigned_to"):
        raise HTTPException(status_code=400, detail="Task has no assigned seller")

    price = int(task.get("price", 0) or 0)
    settlement = {"commission_amount": 0, "seller_payout": 0}
    if price > 0:
        try:
            settlement = await release_held_funds(
                db,
                buyer_user_id=task["created_by"],
                seller_user_id=task["assigned_to"],
                amount=price,
                reason=f"Task completed: {task['title']}",
                source_type="task",
                source_id=task_id,
                metadata={"task_title": task["title"]},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated_task = await db.tasks.find_one_and_update(
        {"_id": object_id, "status": "assigned"},
        {
            "$set": {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc),
                "escrow_status": "released" if price > 0 else "none",
                "commission_amount": settlement["commission_amount"],
                "seller_payout": settlement["seller_payout"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_task:
        raise HTTPException(status_code=409, detail="Task status changed before completion")

    await db.users.update_one(
        {"_id": ObjectId(task["assigned_to"])},
        {"$inc": {"completed_tasks": 1, "tasks_completed": 1}},
    )

    return {
        "message": "Task completed and payout released",
        "task": serialize_task(updated_task),
        "commission_amount": settlement["commission_amount"],
        "seller_payout": settlement["seller_payout"],
    }


@router.post("/{task_id}/rate")
async def rate_task(task_id: str, data: RateTaskInput, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)
    task = await db.tasks.find_one({"_id": object_id})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("created_by") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the task buyer can rate this task")
    if task.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Rating is available only after task completion")
    if not task.get("assigned_to"):
        raise HTTPException(status_code=400, detail="Task has no assigned seller")
    if task.get("buyer_rating") is not None:
        raise HTTPException(status_code=400, detail="Task already rated")

    updated_task = await db.tasks.find_one_and_update(
        {
            "_id": object_id,
            "status": "completed",
            "buyer_rating": None,
        },
        {
            "$set": {
                "buyer_rating": data.rating,
                "buyer_rated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_task:
        raise HTTPException(status_code=409, detail="Task rating was already submitted")

    seller_id = updated_task["assigned_to"]
    seller = await db.users.find_one({"_id": ObjectId(seller_id)}, {"task_rating": 1, "task_rating_count": 1})
    current_average = float((seller or {}).get("task_rating", 0) or 0)
    current_count = int((seller or {}).get("task_rating_count", 0) or 0)
    new_count = current_count + 1
    new_average = round(((current_average * current_count) + data.rating) / new_count, 1)

    await db.users.update_one(
        {"_id": ObjectId(seller_id)},
        {
            "$set": {"task_rating": new_average},
            "$inc": {"task_rating_count": 1},
        },
    )

    return {
        "message": "Task rated successfully",
        "task": serialize_task(updated_task),
        "rating": data.rating,
        "seller_rating": new_average,
        "seller_rating_count": new_count,
    }


@router.get("/my-tasks")
async def get_my_tasks(
    page: int = 1,
    limit: int = 10,
    status: Optional[TaskStatus] = None,
    current_user=Depends(get_current_user),
):
    db = get_db()
    page = max(page, 1)
    limit = max(1, min(limit, 10))
    query: dict = {
        "$or": [
            {"created_by": current_user["id"]},
            {"assigned_to": current_user["id"]},
        ]
    }
    if status:
        query["status"] = status

    projection = {
        "title": 1,
        "description": 1,
        "price": 1,
        "deadline": 1,
        "location": 1,
        "required_skills": 1,
        "is_urgent": 1,
        "urgent_fee": 1,
        "views": 1,
        "clicks": 1,
        "accepts": 1,
        "popularity_score": 1,
        "status": 1,
        "created_by": 1,
        "created_by_name": 1,
        "assigned_to": 1,
        "assigned_to_name": 1,
        "created_at": 1,
        "accepted_at": 1,
        "completed_at": 1,
        "escrow_status": 1,
        "escrow_amount": 1,
        "commission_amount": 1,
        "seller_payout": 1,
        "is_boosted": 1,
        "boosted_at": 1,
        "buyer_rating": 1,
        "buyer_rated_at": 1,
    }
    skip = (page - 1) * limit
    cursor = (
        db.tasks.find(query, projection)
        .sort([("is_urgent", -1), ("is_boosted", -1), ("popularity_score", -1), ("boosted_at", -1), ("created_at", -1)])
        .skip(skip)
        .limit(limit)
    )
    tasks = []
    raw_tasks = []
    async for task in cursor:
        raw_tasks.append(task)
    hydrated_tasks = await hydrate_task_creators(db, raw_tasks)
    for task in hydrated_tasks:
        tasks.append(serialize_task(task))
    total = await db.tasks.count_documents(query)
    return {
        "tasks": tasks,
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit,
        "has_more": skip + len(tasks) < total,
    }


@router.get("/{task_id}")
async def get_task(task_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)
    task = await db.tasks.find_one({"_id": object_id})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.get("created_by") != current_user["id"]:
        await track_task_metric(
            db,
            object_id,
            event_type="click",
            user_id=current_user["id"],
            task_snapshot=task,
        )
        task["clicks"] = int(task.get("clicks", 0) or 0) + 1
        task["popularity_score"] = get_popularity_score(task)

    hydrated = await hydrate_task_creators(db, [task])
    return {"task": serialize_task(hydrated[0])}


@router.post("/{task_id}/report")
async def report_task(task_id: str, data: ReportTaskInput, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(task_id)
    task = await db.tasks.find_one({"_id": object_id}, {"title": 1, "created_by": 1})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await db.reports.insert_one(
        {
            "type": "task",
            "reported_id": task_id,
            "reported_name": task.get("title", "Task"),
            "reported_by": current_user["id"],
            "reported_by_name": current_user.get("name", "Student"),
            "reason": data.reason.strip(),
            "status": "open",
            "created_at": datetime.now(timezone.utc),
        }
    )
    return {"message": "Task reported successfully"}
