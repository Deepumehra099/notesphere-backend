from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user
from utils.credits import (
    InsufficientCredits,
    apply_credit_change,
    ensure_referral_code,
    get_credit_settings,
    utc_now,
)
from utils.db import get_client, get_db

router = APIRouter(prefix="/api/credits", tags=["credits"])
admin_router = APIRouter(prefix="/api/admin/credits", tags=["admin-credits"])


class CreditAdjustmentInput(BaseModel):
    user_id: str = Field(alias="userId")
    amount: int = Field(..., gt=0)
    operation: str = Field(pattern="^(add|remove)$")
    reason: str = Field(..., min_length=3, max_length=240)
    request_id: str = Field(alias="requestId", min_length=8, max_length=100)


class CreditSettingsInput(BaseModel):
    signup_reward: int = Field(..., ge=0, le=100000, alias="signupReward")
    approved_note_reward: int = Field(..., ge=0, le=100000, alias="approvedNoteReward")
    referral_reward: int = Field(..., ge=0, le=100000, alias="referralReward")
    referral_signup_reward: int = Field(..., ge=0, le=100000, alias="referralSignupReward")
    premium_note_price: int = Field(0, ge=0, le=100000, alias="premiumNotePrice")


class CreditRewardInput(BaseModel):
    user_id: str = Field(alias="userId")
    amount: int = Field(..., gt=0, le=100000)
    reason: str = Field(..., min_length=3, max_length=240)
    request_id: str = Field(alias="requestId", min_length=8, max_length=100)


def serialize_credit_transaction(item: dict) -> dict:
    created_at = item.get("created_at")
    return {
        "id": str(item.get("_id", "")),
        "userId": str(item.get("user_id", "")),
        "type": item.get("type", ""),
        "amount": int(item.get("amount", 0) or 0),
        "balanceBefore": int(item.get("balance_before", 0) or 0),
        "balanceAfter": int(item.get("balance_after", 0) or 0),
        "referenceId": str(item.get("reference_id", "") or ""),
        "description": item.get("description", ""),
        "metadata": item.get("metadata", {}),
        "createdAt": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
    }


async def require_admin(current_user=Depends(get_current_user)):
    if not bool(current_user.get("is_admin", False) or current_user.get("role") == "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


@router.get("")
@router.get("/balance")
async def get_credits(current_user=Depends(get_current_user)):
    db = get_db()
    user_id = current_user["id"]
    user = await db.users.find_one_and_update(
        {"_id": ObjectId(user_id), "$or": [{"credits_balance": {"$exists": False}}, {"credits_balance": None}]},
        {"$set": {"credits_balance": 0}},
        return_document=ReturnDocument.AFTER,
        projection={"credits_balance": 1},
    )
    if user is None:
        user = await db.users.find_one({"_id": ObjectId(user_id)}, {"credits_balance": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    code = await ensure_referral_code(db, user_id)
    return {"balance": int(user.get("credits_balance", 0) or 0), "referralCode": code}


@router.get("/history")
async def get_credit_history(limit: int = 50, before: Optional[str] = None, current_user=Depends(get_current_user)):
    db = get_db()
    limit = min(max(limit, 1), 100)
    query = {"user_id": current_user["id"]}
    if before:
        try:
            query["_id"] = {"$lt": ObjectId(before)}
        except InvalidId as exc:
            raise HTTPException(status_code=400, detail="Invalid history cursor") from exc
    rows = [serialize_credit_transaction(item) async for item in db.credit_transactions.find(query).sort("created_at", -1).limit(limit)]
    return {"transactions": rows, "hasMore": len(rows) == limit, "nextCursor": rows[-1]["id"] if rows else None}


@router.get("/summary")
async def get_credit_summary(current_user=Depends(get_current_user)):
    """Return the authenticated user's live balance and totals from the token ledger."""
    db = get_db()
    user = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"credits_balance": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    groups = [row async for row in db.credit_transactions.aggregate([
        {"$match": {"user_id": current_user["id"]}},
        {"$group": {
            "_id": None,
            "earned": {"$sum": {"$cond": [{"$gt": ["$amount", 0]}, "$amount", 0]}},
            "spent": {"$sum": {"$cond": [{"$lt": ["$amount", 0]}, {"$abs": "$amount"}, 0]}},
            "pending": {"$sum": {"$cond": [{"$eq": ["$status", "pending"]}, {"$abs": "$amount"}, 0]}},
        }},
    ])]
    totals = groups[0] if groups else {}
    return {
        "balance": int(user.get("credits_balance", 0) or 0),
        "earned": int(totals.get("earned", 0) or 0),
        "spent": int(totals.get("spent", 0) or 0),
        "pending": int(totals.get("pending", 0) or 0),
    }


@router.get("/earning-methods")
async def get_earning_methods(current_user=Depends(get_current_user)):
    settings = await get_credit_settings(get_db())
    return {
        "methods": [
            {"id": "signup", "title": "Signup Reward", "description": "One-time reward for creating an account.", "amount": settings["signup_reward"], "enabled": settings["signup_reward"] > 0},
            {"id": "approved_note", "title": "Upload Approved Note", "description": "Earn Tokens after your uploaded note is approved.", "amount": settings["approved_note_reward"], "enabled": settings["approved_note_reward"] > 0},
            {"id": "referral", "title": "Refer a Friend", "description": "Both users earn Tokens after the referred user contributes an approved note or unlocks a premium note.", "amount": settings["referral_reward"], "referredAmount": settings["referral_signup_reward"], "enabled": settings["referral_reward"] > 0 or settings["referral_signup_reward"] > 0},
        ]
    }


@router.get("/referral")
@router.get("/referrals")
async def get_referral_info(current_user=Depends(get_current_user)):
    db = get_db()
    code = await ensure_referral_code(db, current_user["id"])
    rows = [
        {"id": str(item["_id"]), "code": item.get("referral_code", ""), "status": item.get("status", "registered"), "createdAt": item.get("created_at").isoformat() if item.get("created_at") else None, "qualifiedAt": item.get("qualified_at").isoformat() if item.get("qualified_at") else None}
        async for item in db.referrals.find({"referrer_user_id": current_user["id"]}).sort("created_at", -1).limit(100)
    ]
    return {"referralCode": code, "referrals": rows}


@admin_router.get("/summary")
async def get_credit_admin_summary(admin=Depends(require_admin)):
    db = get_db()
    groups = [row async for row in db.credit_transactions.aggregate([
        {"$group": {"_id": None, "issued": {"$sum": {"$cond": [{"$gt": ["$amount", 0]}, "$amount", 0]}}, "spent": {"$sum": {"$cond": [{"$lt": ["$amount", 0]}, {"$abs": "$amount"}, 0]}}, "count": {"$sum": 1}}}
    ])]
    totals = groups[0] if groups else {}
    top_users = []
    async for user in db.users.find({"credits_balance": {"$gt": 0}}, {"name": 1, "email": 1, "credits_balance": 1}).sort("credits_balance", -1).limit(10):
        top_users.append({"userId": str(user["_id"]), "name": user.get("name", ""), "email": user.get("email", ""), "balance": int(user.get("credits_balance", 0) or 0)})
    recent = [serialize_credit_transaction(item) async for item in db.credit_transactions.find({}).sort("created_at", -1).limit(20)]
    return {"totalIssued": int(totals.get("issued", 0) or 0), "totalSpent": int(totals.get("spent", 0) or 0), "transactionCount": int(totals.get("count", 0) or 0), "topEarners": top_users, "recentActivity": recent, "settings": await get_credit_settings(db)}


@admin_router.get("/transactions")
async def get_admin_credit_transactions(limit: int = 100, admin=Depends(require_admin)):
    limit = min(max(limit, 1), 250)
    rows = [serialize_credit_transaction(item) async for item in get_db().credit_transactions.find({}).sort("created_at", -1).limit(limit)]
    return {"transactions": rows}


@admin_router.get("/settings")
async def get_admin_credit_settings(admin=Depends(require_admin)):
    return {"settings": await get_credit_settings(get_db())}


@admin_router.put("/settings")
async def update_credit_settings(data: CreditSettingsInput, admin=Depends(require_admin)):
    values = {
        "signup_reward": data.signup_reward,
        "approved_note_reward": data.approved_note_reward,
        "referral_reward": data.referral_reward,
        "referral_signup_reward": data.referral_signup_reward,
        "premium_note_price": data.premium_note_price,
        "updated_at": utc_now(),
        "updated_by": admin["id"],
    }
    await get_db().credit_settings.update_one({"_id": "global"}, {"$set": values}, upsert=True)
    return {"settings": await get_credit_settings(get_db())}


async def _resolve_target_user(db, value: str):
    query = {"email": value.strip().lower()}
    if ObjectId.is_valid(value):
        query = {"_id": ObjectId(value)}
    user = await db.users.find_one(query, {"_id": 1, "name": 1, "email": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@admin_router.get("/user")
async def get_admin_credit_user(user_id: str, admin=Depends(require_admin)):
    db = get_db()
    target = await _resolve_target_user(db, user_id)
    user = await db.users.find_one({"_id": target["_id"]}, {"credits_balance": 1})
    return {
        "userId": str(target["_id"]),
        "name": target.get("name", ""),
        "email": target.get("email", ""),
        "balance": int((user or {}).get("credits_balance", 0) or 0),
    }


@admin_router.post("/adjust")
async def adjust_user_credits(data: CreditAdjustmentInput, admin=Depends(require_admin)):
    db = get_db()
    target = await _resolve_target_user(db, data.user_id)
    amount = data.amount if data.operation == "add" else -data.amount
    try:
        client = get_client()
        async with await client.start_session() as session:
            async with session.start_transaction():
                result = await apply_credit_change(
                    db,
                    user_id=str(target["_id"]),
                    amount=amount,
                    transaction_type="ADMIN_ADJUSTMENT",
                    description=data.reason,
                    idempotency_key=f"admin-adjustment:{admin['id']}:{data.request_id}",
                    metadata={"admin_id": admin["id"], "admin_email": admin.get("email", ""), "operation": data.operation},
                    session=session,
                )
    except InsufficientCredits as exc:
        raise HTTPException(status_code=400, detail="Not enough Tokens") from exc
    return {
        "message": "Tokens adjusted",
        "userId": str(target["_id"]),
        "userEmail": target.get("email", ""),
        "balance": result["balance"],
        "alreadyApplied": result["already_applied"],
    }


@admin_router.post("/reward")
async def reward_user_credits(data: CreditRewardInput, admin=Depends(require_admin)):
    db = get_db()
    target = await _resolve_target_user(db, data.user_id)
    client = get_client()
    async with await client.start_session() as session:
        async with session.start_transaction():
            result = await apply_credit_change(
                db,
                user_id=str(target["_id"]),
                amount=data.amount,
                transaction_type="ADMIN_REWARD",
                description=data.reason,
                idempotency_key=f"admin-reward:{admin['id']}:{data.request_id}",
                metadata={"admin_id": admin["id"], "admin_email": admin.get("email", "")},
                session=session,
            )
    return {"message": "Tokens rewarded", "balance": result["balance"], "alreadyApplied": result["already_applied"]}


@admin_router.post("/refund-note/{note_id}")
async def refund_note_credits(note_id: str, user_id: str, reason: str = "Premium note purchase refund", admin=Depends(require_admin)):
    db = get_db()
    try:
        note_object_id = ObjectId(note_id)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    target = await _resolve_target_user(db, user_id)
    user_id = str(target["_id"])
    client = get_client()
    try:
        async with await client.start_session() as session:
            async with session.start_transaction():
                note = await db.notes.find_one({"_id": note_object_id, "unlocked_by": user_id}, session=session)
                if not note:
                    raise HTTPException(status_code=404, detail="Active premium note purchase not found")
                sequence = int(note.get("credit_purchase_sequence", 0) or 0)
                cost = int(note.get("unlock_cost", 0) or 0)
                purchase = await db.credit_transactions.find_one(
                    {"idempotency_key": f"note-purchase:{user_id}:{note_id}:{sequence}", "type": "CREDIT_SPEND"},
                    session=session,
                )
                if cost <= 0 or not purchase:
                    raise HTTPException(status_code=400, detail="This note does not have a Credit purchase to refund")
                updated = await db.notes.update_one(
                    {"_id": note_object_id, "unlocked_by": user_id},
                    {"$pull": {"unlocked_by": user_id}, "$set": {"last_refunded_purchase_sequence": sequence, "last_refunded_at": utc_now()}},
                    session=session,
                )
                if not updated.modified_count:
                    raise HTTPException(status_code=409, detail="Purchase was already refunded")
                result = await apply_credit_change(
                    db,
                    user_id=user_id,
                    amount=cost,
                    transaction_type="CREDIT_REFUND",
                    description=reason.strip() or "Premium note purchase refund",
                    idempotency_key=f"note-refund:{user_id}:{note_id}:{sequence}",
                    reference_id=note_id,
                    metadata={"admin_id": admin["id"], "purchase_sequence": sequence},
                    session=session,
                )
    except HTTPException:
        raise
    return {"message": "Tokens refunded", "balance": result["balance"]}
