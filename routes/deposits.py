from datetime import datetime, timezone
import os
from typing import Literal, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import credit_wallet

router = APIRouter(prefix="/api/deposit", tags=["deposits"])
admin_router = APIRouter(prefix="/api/admin/deposits", tags=["admin-deposits"])


class DepositInput(BaseModel):
    amount: int = Field(..., ge=1)
    method: Literal["upi", "bank"]
    reference: str = Field(..., min_length=1, max_length=140)
    sender_details: str = Field(..., min_length=1, max_length=140)


def parse_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Deposit request not found") from exc


async def require_admin(current_user=Depends(get_current_user)):
    if not bool(current_user.get("is_admin", False) or current_user.get("role") == "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def serialize_deposit(item: dict) -> dict:
    created_at = item.get("created_at")
    updated_at = item.get("updated_at")
    return {
        "id": str(item.get("_id")),
        "user_id": item.get("user_id", ""),
        "user_name": item.get("user_name", ""),
        "amount": int(item.get("amount", 0) or 0),
        "method": item.get("method", "UPI"),
        "reference": item.get("reference", ""),
        "sender_details": item.get("sender_details", ""),
        "status": item.get("status", "pending"),
        "admin_note": item.get("admin_note", ""),
        "createdAt": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updatedAt": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


@router.get("/details")
async def deposit_details(_current_user=Depends(get_current_user)):
    upi_id = os.getenv("DEPOSIT_UPI_ID", "").strip()
    bank_account_number = os.getenv("DEPOSIT_BANK_ACCOUNT_NUMBER", "").strip()
    return {
        "upi": {
            "id": upi_id,
            "name": os.getenv("DEPOSIT_UPI_NAME", "").strip(),
            "configured": bool(upi_id),
        },
        "bank": {
            "name": os.getenv("DEPOSIT_BANK_NAME", "").strip(),
            "account_name": os.getenv("DEPOSIT_BANK_ACCOUNT_NAME", "").strip(),
            "account_number": bank_account_number,
            "ifsc": os.getenv("DEPOSIT_BANK_IFSC", "").strip(),
            "configured": bool(bank_account_number and os.getenv("DEPOSIT_BANK_IFSC", "").strip()),
        },
    }


@router.post("/request")
async def request_deposit(data: DepositInput, current_user=Depends(get_current_user)):
    if not data.reference.strip() or not data.sender_details.strip():
        raise HTTPException(status_code=422, detail="Sender details and transfer reference are required")
    if data.method == "upi" and not os.getenv("DEPOSIT_UPI_ID", "").strip():
        raise HTTPException(status_code=503, detail="UPI deposits are not configured yet")
    if data.method == "bank" and not (
        os.getenv("DEPOSIT_BANK_ACCOUNT_NUMBER", "").strip()
        and os.getenv("DEPOSIT_BANK_IFSC", "").strip()
    ):
        raise HTTPException(status_code=503, detail="Bank deposits are not configured yet")

    db = get_db()
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": current_user["id"],
        "user_name": current_user.get("name", "User"),
        "amount": data.amount,
        "method": data.method,
        "reference": data.reference.strip(),
        "sender_details": data.sender_details.strip(),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    result = await db.deposit_requests.insert_one(doc)
    doc["_id"] = result.inserted_id

    await db.notifications.insert_one(
        {
            "user_id": current_user["id"],
            "title": "Deposit request submitted",
            "body": f"Your deposit request for Rs {data.amount} is waiting for admin approval.",
            "read": False,
            "created_at": now,
        }
    )
    return {"message": "Deposit request submitted", "deposit": serialize_deposit(doc)}


@router.get("/history")
async def deposit_history(status: Optional[str] = Query(default=None), current_user=Depends(get_current_user)):
    db = get_db()
    query = {"user_id": current_user["id"]}
    if status and status != "all":
        query["status"] = status
    deposits = [serialize_deposit(item) async for item in db.deposit_requests.find(query).sort("created_at", -1)]

    if not status or status == "all" or status in {"approved", "completed"}:
        requests_by_id = {item["id"]: item for item in deposits}
        transactions = db.transactions.find(
            {
                "user_id": current_user["id"],
                "$or": [
                    {"source_type": {"$in": ["deposit", "payment_order", "payment", "topup"]}},
                    {"category": "deposit"},
                ],
                "amount": {"$gt": 0},
                "status": {"$in": ["completed", "approved", "success"]},
            }
        ).sort("created_at", -1)
        async for transaction in transactions:
            source_id = str(transaction.get("source_id", "") or "")
            if source_id in requests_by_id:
                continue

            metadata = transaction.get("metadata") or {}
            created_at = transaction.get("created_at")
            deposits.append(
                {
                    "id": source_id or str(transaction.get("_id")),
                    "user_id": transaction.get("user_id", ""),
                    "amount": int(transaction.get("amount", 0) or 0),
                    "method": metadata.get("method", "Payment" if transaction.get("source_type") == "payment_order" else "Deposit"),
                    "reference": metadata.get("reference") or metadata.get("payment_id") or source_id,
                    "sender_details": "",
                    "status": "approved",
                    "admin_note": "",
                    "createdAt": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
                    "updatedAt": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
                }
            )

    deposits.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return {"deposits": deposits}


@admin_router.get("")
@admin_router.get("/")
async def admin_deposits(admin=Depends(require_admin)):
    db = get_db()
    deposits = [serialize_deposit(item) async for item in db.deposit_requests.find({}).sort("created_at", -1)]
    return {"deposits": deposits}


@admin_router.post("/{deposit_id}/approve")
async def approve_deposit(deposit_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(deposit_id)
    deposit = await db.deposit_requests.find_one_and_update(
        {"_id": object_id, "status": {"$in": ["pending", "processing"]}},
        {"$set": {"status": "processing", "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not deposit:
        exists = await db.deposit_requests.find_one({"_id": object_id}, {"_id": 1})
        raise HTTPException(status_code=400 if exists else 404, detail="Deposit already processed" if exists else "Deposit request not found")

    await credit_wallet(
        db,
        user_id=deposit["user_id"],
        amount=int(deposit.get("amount", 0) or 0),
        reason=f"Deposit approved via {deposit.get('method', 'UPI')}",
        transaction_type="top_up",
        source_type="deposit",
        source_id=deposit_id,
        metadata={"reference": deposit.get("reference", ""), "approved_by": admin["id"]},
        idempotency_key=f"manual-deposit:{deposit_id}",
    )
    updated = await db.deposit_requests.find_one_and_update(
        {"_id": object_id, "status": "processing"},
        {"$set": {"status": "approved", "admin_note": "Approved", "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Deposit credited but approval could not be finalized; retry to reconcile")
    await db.notifications.insert_one(
        {
            "user_id": deposit["user_id"],
            "title": "Deposit approved",
            "body": f"Rs {deposit.get('amount', 0)} has been added to your wallet.",
            "read": False,
            "created_at": datetime.now(timezone.utc),
        }
    )
    return {"message": "Deposit approved", "deposit": serialize_deposit(updated)}


@admin_router.post("/{deposit_id}/reject")
async def reject_deposit(deposit_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(deposit_id)
    updated = await db.deposit_requests.find_one_and_update(
        {"_id": object_id, "status": "pending"},
        {"$set": {"status": "rejected", "admin_note": "Rejected", "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Pending deposit request not found")
    await db.notifications.insert_one(
        {
            "user_id": updated["user_id"],
            "title": "Deposit rejected",
            "body": f"Your deposit request for Rs {updated.get('amount', 0)} was rejected.",
            "read": False,
            "created_at": datetime.now(timezone.utc),
        }
    )
    return {"message": "Deposit rejected", "deposit": serialize_deposit(updated)}
