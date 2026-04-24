from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from routes.auth import serialize_user
from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import credit_wallet, debit_wallet, ensure_wallet

router = APIRouter(prefix="/api/admin", tags=["admin"])


class WalletAdjustInput(BaseModel):
    amount: int = Field(..., description="Positive adds balance, negative removes balance")
    reason: str = Field(..., min_length=3, max_length=240)


class ReportActionInput(BaseModel):
    action: str = Field(..., min_length=3, max_length=40)
    note: str = Field("", max_length=500)


class BoostPricingInput(BaseModel):
    price: int = Field(..., ge=0)


class AdminUserUpdateInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: str = Field(..., min_length=3, max_length=160)
    phone: str = Field("", max_length=40)
    language: str = Field("en", pattern="^(en|hi)$")
    role: str = Field("user", min_length=3, max_length=40)


class SupportReplyInput(BaseModel):
    reply: str = Field(..., min_length=2, max_length=1200)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_object_id(value: str, detail: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail=detail) from exc


async def require_admin(current_user=Depends(get_current_user)):
    if not bool(current_user.get("is_admin", False) or current_user.get("role") == "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def serialize_note_admin(note: dict) -> dict:
    created_at = note.get("created_at")
    return {
        "id": str(note.get("_id")),
        "title": note.get("title", ""),
        "description": note.get("description", ""),
        "subject": note.get("subject", ""),
        "topic": note.get("topic", ""),
        "status": note.get("status", "pending"),
        "price": int(note.get("unlock_cost", 0) or 0),
        "uploaded_by": note.get("uploaded_by", ""),
        "uploader_name": note.get("uploader_name", ""),
        "file_name": note.get("file_name", ""),
        "views": int(note.get("views", 0) or 0),
        "downloads": int(note.get("downloads", 0) or 0),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


def serialize_task_admin(task: dict) -> dict:
    created_at = task.get("created_at")
    return {
        "id": str(task.get("_id")),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "price": int(task.get("price", 0) or 0),
        "deadline": task.get("deadline", ""),
        "status": task.get("status", "open"),
        "created_by": task.get("created_by", ""),
        "created_by_name": task.get("created_by_name", ""),
        "assigned_to": task.get("assigned_to"),
        "assigned_to_name": task.get("assigned_to_name", ""),
        "location": task.get("location", ""),
        "is_boosted": bool(task.get("is_boosted", False)),
        "is_urgent": bool(task.get("is_urgent", False)),
        "views": int(task.get("views", 0) or 0),
        "clicks": int(task.get("clicks", 0) or 0),
        "accepts": int(task.get("accepts", 0) or 0),
        "popularity_score": int(task.get("popularity_score", 0) or 0),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


def serialize_gig_admin(gig: dict) -> dict:
    created_at = gig.get("created_at")
    return {
        "id": str(gig.get("_id")),
        "title": gig.get("title", ""),
        "description": gig.get("description", ""),
        "price": int(gig.get("price", 0) or 0),
        "user_id": gig.get("user_id", ""),
        "seller_name": gig.get("seller_name", ""),
        "is_featured": bool(gig.get("is_featured", False)),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


def serialize_transaction(transaction: dict) -> dict:
    created_at = transaction.get("created_at")
    return {
        "id": str(transaction.get("_id")),
        "user_id": transaction.get("user_id", ""),
        "amount": int(transaction.get("amount", 0) or 0),
        "type": transaction.get("type", ""),
        "reason": transaction.get("reason", ""),
        "status": transaction.get("status", ""),
        "source_type": transaction.get("source_type", ""),
        "source_id": transaction.get("source_id", ""),
        "counterparty_user_id": transaction.get("counterparty_user_id", ""),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "metadata": transaction.get("metadata", {}),
    }


def serialize_chat_message(message: dict) -> dict:
    created_at = message.get("created_at")
    return {
        "id": str(message.get("_id")),
        "chat_id": message.get("chat_id", ""),
        "task_id": message.get("task_id", ""),
        "sender_id": message.get("sender_id", ""),
        "sender_name": message.get("sender_name", ""),
        "text": message.get("text", ""),
        "message_type": message.get("message_type", "text"),
        "status": message.get("status", ""),
        "is_flagged": bool(message.get("is_flagged", False)),
        "flag_reason": message.get("flag_reason", ""),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


def serialize_report(report: dict) -> dict:
    created_at = report.get("created_at")
    return {
        "id": str(report.get("_id")),
        "type": report.get("type", "user"),
        "reason": report.get("reason", ""),
        "reported_id": report.get("reported_id", ""),
        "reported_by": report.get("reported_by", ""),
        "reported_by_name": report.get("reported_by_name", ""),
        "status": report.get("status", "open"),
        "action_taken": report.get("action_taken", ""),
        "admin_note": report.get("admin_note", ""),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


def serialize_support_ticket(ticket: dict) -> dict:
    created_at = ticket.get("created_at")
    updated_at = ticket.get("updated_at")
    return {
        "id": str(ticket.get("_id")),
        "user_id": ticket.get("user_id", ""),
        "user_name": ticket.get("user_name", ""),
        "user_email": ticket.get("user_email", ""),
        "subject": ticket.get("subject", ""),
        "message": ticket.get("message", ""),
        "status": ticket.get("status", "open"),
        "reply": ticket.get("reply", ""),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


def serialize_subscription(subscription: dict) -> dict:
    created_at = subscription.get("created_at")
    paid_at = subscription.get("paid_at")
    return {
        "id": str(subscription.get("_id")),
        "order_id": subscription.get("order_id", ""),
        "user_id": subscription.get("user_id", ""),
        "plan_name": subscription.get("plan_name", ""),
        "role": subscription.get("role", ""),
        "status": subscription.get("status", ""),
        "approval_status": subscription.get("approval_status", ""),
        "amount": int(subscription.get("amount", 0) or 0),
        "currency": subscription.get("currency", "INR"),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "paid_at": paid_at.isoformat() if isinstance(paid_at, datetime) else paid_at,
    }


async def get_boost_price(db) -> int:
    settings = await db.admin_settings.find_one({"key": "boost_pricing"})
    return int((settings or {}).get("price", 100) or 100)


@router.get("/dashboard")
async def get_dashboard(admin=Depends(require_admin)):
    db = get_db()
    active_since = utc_now() - timedelta(days=7)
    total_users = await db.users.count_documents({})
    total_tasks = await db.tasks.count_documents({})
    total_notes = await db.notes.count_documents({})
    active_users = await db.users.count_documents({"$or": [{"last_login_at": {"$gte": active_since}}, {"created_at": {"$gte": active_since}}]})
    total_earnings_result = await db.transactions.aggregate([
        {"$match": {"amount": {"$gt": 0}, "type": {"$in": ["earn", "top_up"]}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]).to_list(1)
    total_earnings = int((total_earnings_result[0]["total"] if total_earnings_result else 0) or 0)

    latest_users_cursor = db.users.find({}, {"password_hash": 0, "password": 0}).sort("created_at", -1).limit(5)
    latest_tasks_cursor = db.tasks.find({}).sort("created_at", -1).limit(5)
    latest_notes_cursor = db.notes.find({}).sort("created_at", -1).limit(5)

    latest_users = []
    async for user in latest_users_cursor:
        wallet = await ensure_wallet(db, str(user["_id"]))
        latest_users.append(serialize_user(user, wallet))

    latest_tasks = [serialize_task_admin(task) async for task in latest_tasks_cursor]
    latest_notes = [serialize_note_admin(note) async for note in latest_notes_cursor]

    return {
        "total_users": total_users,
        "total_tasks": total_tasks,
        "total_notes": total_notes,
        "total_earnings": total_earnings,
        "active_users": active_users,
        "boost_price": await get_boost_price(db),
        "latest_users": latest_users,
        "latest_tasks": latest_tasks,
        "latest_notes": latest_notes,
    }


@router.get("/users")
async def get_users(
    q: str = Query("", min_length=0),
    admin=Depends(require_admin),
):
    db = get_db()
    query: dict = {}
    if q.strip():
        query["$or"] = [
            {"name": {"$regex": q.strip(), "$options": "i"}},
            {"email": {"$regex": q.strip(), "$options": "i"}},
            {"uid": {"$regex": q.strip(), "$options": "i"}},
        ]

    users = []
    cursor = db.users.find(query, {"password_hash": 0, "password": 0}).sort("created_at", -1)
    async for user in cursor:
        wallet = await ensure_wallet(db, str(user["_id"]))
        users.append(serialize_user(user, wallet))
    return {"users": users}


@router.post("/user/{user_id}/ban")
async def ban_or_unban_user(user_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(user_id, "User not found")
    user = await db.users.find_one({"_id": object_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    next_state = not bool(user.get("is_banned", False))
    await db.users.update_one(
        {"_id": object_id},
        {"$set": {"is_banned": next_state}},
    )
    return {"message": "User banned" if next_state else "User unbanned", "is_banned": next_state}


@router.post("/user/{user_id}/verify")
async def verify_user(user_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(user_id, "User not found")
    user = await db.users.find_one({"_id": object_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    next_state = not bool(user.get("is_verified", user.get("verified", False)))
    await db.users.update_one(
        {"_id": object_id},
        {"$set": {"is_verified": next_state, "verified": next_state}},
    )
    return {"message": "User verified" if next_state else "User verification removed", "is_verified": next_state}


@router.patch("/user/{user_id}")
async def update_user_profile(user_id: str, data: AdminUserUpdateInput, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(user_id, "User not found")
    existing = await db.users.find_one({"_id": object_id})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    email = data.email.strip().lower()
    duplicate = await db.users.find_one({"email": email, "_id": {"$ne": object_id}}, {"_id": 1})
    if duplicate:
        raise HTTPException(status_code=400, detail="Email already exists")

    updated = await db.users.find_one_and_update(
        {"_id": object_id},
        {
            "$set": {
                "name": data.name.strip(),
                "email": email,
                "phone": data.phone.strip(),
                "language": data.language,
                "role": data.role.strip(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    wallet = await ensure_wallet(db, user_id)
    return {"message": "User updated", "user": serialize_user(updated, wallet)}


@router.post("/user/{user_id}/approve-elite-seller")
async def approve_elite_seller(user_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(user_id, "User not found")
    updated = await db.users.find_one_and_update(
        {"_id": object_id},
        {"$set": {"role": "eliteSeller", "elite_seller_active": True, "elite_seller_status": "approved"}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    await db.subscription_orders.update_many(
        {"user_id": user_id, "role": "eliteSeller", "status": "paid"},
        {"$set": {"approval_status": "approved"}},
    )
    return {"message": "Elite seller approved"}


@router.get("/notes")
async def get_notes(status: Optional[str] = None, admin=Depends(require_admin)):
    db = get_db()
    query = {"status": status} if status else {}
    notes = [serialize_note_admin(note) async for note in db.notes.find(query).sort("created_at", -1)]
    return {"notes": notes}


@router.get("/pending-notes")
async def get_pending_notes(admin=Depends(require_admin)):
    return await get_notes(status="pending", admin=admin)


@router.get("/notes/pending")
async def get_pending_notes_compat(admin=Depends(require_admin)):
    return await get_notes(status="pending", admin=admin)


@router.post("/notes/{note_id}/approve")
async def approve_note(note_id: str, admin=Depends(require_admin)):
    db = get_db()
    result = await db.notes.update_one(
        {"_id": parse_object_id(note_id, "Note not found")},
        {"$set": {"status": "approved", "approved_at": utc_now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note approved"}


@router.put("/notes/{note_id}/approve")
async def approve_note_put_alias(note_id: str, admin=Depends(require_admin)):
    return await approve_note(note_id, admin)


@router.post("/notes/{note_id}/reject")
async def reject_note(note_id: str, admin=Depends(require_admin)):
    db = get_db()
    result = await db.notes.update_one(
        {"_id": parse_object_id(note_id, "Note not found")},
        {"$set": {"status": "rejected", "rejected_at": utc_now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note rejected"}


@router.put("/notes/{note_id}/reject")
async def reject_note_put_alias(note_id: str, admin=Depends(require_admin)):
    return await reject_note(note_id, admin)


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str, admin=Depends(require_admin)):
    db = get_db()
    result = await db.notes.delete_one({"_id": parse_object_id(note_id, "Note not found")})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note deleted"}


@router.get("/tasks")
async def get_tasks(admin=Depends(require_admin)):
    db = get_db()
    tasks = [serialize_task_admin(task) async for task in db.tasks.find({}).sort("created_at", -1)]
    return {"tasks": tasks}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, admin=Depends(require_admin)):
    db = get_db()
    result = await db.tasks.delete_one({"_id": parse_object_id(task_id, "Task not found")})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.chats.delete_many({"task_id": task_id})
    await db.messages.delete_many({"task_id": task_id})
    return {"message": "Task deleted"}


@router.post("/tasks/{task_id}/complete")
async def force_complete_task(task_id: str, admin=Depends(require_admin)):
    db = get_db()
    updated = await db.tasks.find_one_and_update(
        {"_id": parse_object_id(task_id, "Task not found")},
        {"$set": {"status": "completed", "completed_at": utc_now(), "escrow_status": "released"}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"message": "Task force completed", "task": serialize_task_admin(updated)}


@router.post("/tasks/{task_id}/boost")
async def admin_boost_task(task_id: str, admin=Depends(require_admin)):
    db = get_db()
    updated = await db.tasks.find_one_and_update(
        {"_id": parse_object_id(task_id, "Task not found")},
        {"$set": {"is_boosted": True, "boosted_at": utc_now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"message": "Task boost approved", "task": serialize_task_admin(updated)}


@router.post("/tasks/{task_id}/remove-boost")
async def admin_remove_boost(task_id: str, admin=Depends(require_admin)):
    db = get_db()
    updated = await db.tasks.find_one_and_update(
        {"_id": parse_object_id(task_id, "Task not found")},
        {"$set": {"is_boosted": False, "boosted_at": None}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"message": "Task boost removed", "task": serialize_task_admin(updated)}


@router.post("/settings/boost-pricing")
async def set_boost_pricing(data: BoostPricingInput, admin=Depends(require_admin)):
    db = get_db()
    await db.admin_settings.update_one(
        {"key": "boost_pricing"},
        {"$set": {"key": "boost_pricing", "price": data.price, "updated_at": utc_now()}},
        upsert=True,
    )
    return {"message": "Boost pricing updated", "price": data.price}


@router.get("/gigs")
async def get_gigs(admin=Depends(require_admin)):
    db = get_db()
    gigs = [serialize_gig_admin(gig) async for gig in db.gigs.find({}).sort("created_at", -1)]
    return {"gigs": gigs}


@router.delete("/gigs/{gig_id}")
async def delete_gig(gig_id: str, admin=Depends(require_admin)):
    db = get_db()
    result = await db.gigs.delete_one({"_id": parse_object_id(gig_id, "Gig not found")})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Gig not found")
    return {"message": "Gig deleted"}


@router.post("/gigs/{gig_id}/feature")
async def feature_gig(gig_id: str, admin=Depends(require_admin)):
    db = get_db()
    gig = await db.gigs.find_one({"_id": parse_object_id(gig_id, "Gig not found")})
    if not gig:
        raise HTTPException(status_code=404, detail="Gig not found")
    next_state = not bool(gig.get("is_featured", False))
    await db.gigs.update_one({"_id": gig["_id"]}, {"$set": {"is_featured": next_state}})
    return {"message": "Gig featured" if next_state else "Gig unfeatured", "is_featured": next_state}


@router.get("/chats")
async def get_chats(admin=Depends(require_admin)):
    db = get_db()
    chats = []
    async for chat in db.chats.find({}).sort("last_message_at", -1):
        participants = [str(participant) for participant in chat.get("participants", [])]
        suspicious_messages = await db.messages.count_documents({"chat_id": str(chat["_id"]), "is_flagged": True})
        chats.append(
            {
                "id": str(chat["_id"]),
                "task_id": chat.get("task_id", ""),
                "participants": participants,
                "last_message": chat.get("last_message", ""),
                "last_message_at": chat.get("last_message_at").isoformat() if isinstance(chat.get("last_message_at"), datetime) else chat.get("last_message_at"),
                "suspicious_messages": suspicious_messages,
                "messages": [
                    serialize_chat_message(message)
                    async for message in db.messages.find({"chat_id": str(chat["_id"])}).sort("created_at", -1).limit(20)
                ],
            }
        )
    return {"chats": chats}


@router.delete("/message/{message_id}")
async def admin_delete_message(message_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(message_id, "Message not found")
    message = await db.messages.find_one({"_id": object_id})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    await db.messages.update_one(
        {"_id": object_id},
        {
            "$set": {
                "deleted_for_everyone": True,
                "is_deleted": True,
                "text": "This message was removed by admin",
                "message_type": "text",
                "image_url": "",
                "attachment_url": "",
                "file_name": "",
                "mime_type": "",
                "deleted_at": utc_now(),
                "is_edited": False,
            }
        },
    )
    return {"message": "Message deleted"}


@router.get("/transactions")
async def get_transactions(admin=Depends(require_admin)):
    db = get_db()
    transactions = [serialize_transaction(tx) async for tx in db.transactions.find({}).sort("created_at", -1).limit(200)]
    return {"transactions": transactions}


@router.get("/subscriptions")
async def get_subscriptions(admin=Depends(require_admin)):
    db = get_db()
    subscriptions = [
        serialize_subscription(item)
        async for item in db.subscription_orders.find({}).sort("created_at", -1).limit(200)
    ]
    return {"subscriptions": subscriptions}


@router.get("/support-tickets")
async def get_support_tickets(admin=Depends(require_admin)):
    db = get_db()
    tickets = [
        serialize_support_ticket(ticket)
        async for ticket in db.support_tickets.find({}).sort("updated_at", -1).limit(200)
    ]
    return {"tickets": tickets}


@router.post("/support-tickets/{ticket_id}/reply")
async def reply_support_ticket(ticket_id: str, data: SupportReplyInput, admin=Depends(require_admin)):
    db = get_db()
    updated = await db.support_tickets.find_one_and_update(
        {"_id": parse_object_id(ticket_id, "Support ticket not found")},
        {
            "$set": {
                "reply": data.reply.strip(),
                "status": "resolved",
                "updated_at": utc_now(),
                "replied_by": admin["id"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Support ticket not found")
    return {"message": "Reply saved", "ticket": serialize_support_ticket(updated)}


@router.post("/withdraw/{transaction_id}/approve")
async def approve_withdrawal(transaction_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(transaction_id, "Transaction not found")
    transaction = await db.transactions.find_one({"_id": object_id})
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    await db.transactions.update_one(
        {"_id": object_id},
        {"$set": {"status": "approved", "approved_at": utc_now()}},
    )
    return {"message": "Withdrawal approved"}


@router.post("/wallet/{user_id}/adjust")
async def adjust_wallet(user_id: str, data: WalletAdjustInput, admin=Depends(require_admin)):
    db = get_db()
    parse_object_id(user_id, "User not found")
    existing_user = await db.users.find_one({"_id": ObjectId(user_id)}, {"_id": 1})
    if not existing_user:
        raise HTTPException(status_code=404, detail="User not found")
    if data.amount >= 0:
        wallet = await credit_wallet(
            db,
            user_id=user_id,
            amount=data.amount,
            reason=f"Admin wallet adjustment: {data.reason.strip()}",
            transaction_type="admin_adjustment",
            source_type="admin",
            source_id=admin["id"],
        )
    else:
        wallet = await debit_wallet(
            db,
            user_id=user_id,
            amount=abs(data.amount),
            reason=f"Admin wallet adjustment: {data.reason.strip()}",
            transaction_type="admin_adjustment",
            source_type="admin",
            source_id=admin["id"],
        )
        if not wallet:
            raise HTTPException(status_code=400, detail="Insufficient wallet balance for adjustment")
    normalized_wallet = await ensure_wallet(db, user_id)
    return {"message": "Wallet adjusted", "wallet": normalized_wallet}


@router.get("/reports")
async def get_reports(admin=Depends(require_admin)):
    db = get_db()
    reports = [serialize_report(report) async for report in db.reports.find({}).sort("created_at", -1)]
    return {"reports": reports}


@router.post("/reports/{report_id}/action")
async def take_report_action(report_id: str, data: ReportActionInput, admin=Depends(require_admin)):
    db = get_db()
    updated = await db.reports.find_one_and_update(
        {"_id": parse_object_id(report_id, "Report not found")},
        {
            "$set": {
                "status": "resolved",
                "action_taken": data.action.strip(),
                "admin_note": data.note.strip(),
                "resolved_at": utc_now(),
                "resolved_by": admin["id"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"message": "Report action saved", "report": serialize_report(updated)}


@router.get("/analytics")
async def get_analytics(admin=Depends(require_admin)):
    db = get_db()
    daily_usage_pipeline = [
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$updated_at"}},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    earnings_pipeline = [
        {"$match": {"amount": {"$gt": 0}, "type": {"$in": ["earn", "top_up"]}}},
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                "amount": {"$sum": "$amount"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    task_analytics = await db.task_analytics.find({}).sort("updated_at", -1).limit(25).to_list(25)
    daily_usage = await db.task_analytics.aggregate(daily_usage_pipeline).to_list(60)
    daily_earnings = await db.transactions.aggregate(earnings_pipeline).to_list(60)

    return {
        "total_users": await db.users.count_documents({}),
        "total_tasks": await db.tasks.count_documents({}),
        "total_notes": await db.notes.count_documents({}),
        "pending_notes": await db.notes.count_documents({"status": "pending"}),
        "approved_notes": await db.notes.count_documents({"status": "approved"}),
        "total_transactions": await db.transactions.count_documents({}),
        "daily_usage": [{"date": item["_id"], "count": item["count"]} for item in daily_usage],
        "daily_earnings": [{"date": item["_id"], "amount": item["amount"]} for item in daily_earnings],
        "task_popularity": task_analytics,
    }
