from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import debit_wallet, ensure_wallet

router = APIRouter(prefix="/api/withdraw", tags=["withdrawals"])
wallet_router = APIRouter(prefix="/api/wallet", tags=["withdrawals"])

MIN_WITHDRAW_AMOUNT = 100


class WithdrawRequestInput(BaseModel):
    amount: int = Field(..., ge=MIN_WITHDRAW_AMOUNT)
    upi: str = Field(..., min_length=3, max_length=120)


def parse_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Withdrawal request not found") from exc


def serialize_request(item: dict) -> dict:
    created_at = item.get("created_at")
    updated_at = item.get("updated_at")
    return {
        "id": str(item.get("_id")),
        "user_id": item.get("user_id", ""),
        "user_name": item.get("user_name", ""),
        "amount": int(item.get("amount", 0) or 0),
        "upi": item.get("upi", ""),
        "status": item.get("status", "pending"),
        "admin_note": item.get("admin_note", ""),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


async def create_withdrawal_request(data: WithdrawRequestInput, current_user=Depends(get_current_user)):
    db = get_db()
    wallet = await ensure_wallet(db, current_user["id"])
    available_balance = int(wallet.get("available_balance", 0) or 0)
    if data.amount < MIN_WITHDRAW_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Minimum withdraw is ₹{MIN_WITHDRAW_AMOUNT}")
    if available_balance < data.amount:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    pending_total = await db.withdraw_requests.count_documents(
        {"user_id": current_user["id"], "status": "pending"}
    )
    if pending_total > 0:
        raise HTTPException(status_code=400, detail="You already have a pending withdrawal request")

    now = datetime.now(timezone.utc)
    request_doc = {
        "user_id": current_user["id"],
        "user_name": current_user.get("name", "User"),
        "amount": data.amount,
        "upi": data.upi.strip(),
        "status": "pending",
        "admin_note": "",
        "created_at": now,
        "updated_at": now,
    }
    result = await db.withdraw_requests.insert_one(request_doc)
    request_doc["_id"] = result.inserted_id
    return {"message": "Withdrawal request created", "request": serialize_request(request_doc)}


@router.post("/request")
async def request_withdrawal(data: WithdrawRequestInput, current_user=Depends(get_current_user)):
    return await create_withdrawal_request(data, current_user)


@wallet_router.post("/withdraw")
async def request_withdrawal_wallet(data: WithdrawRequestInput, current_user=Depends(get_current_user)):
    return await create_withdrawal_request(data, current_user)


async def list_my_withdrawals(
    status: Optional[str] = Query(default=None),
    current_user=Depends(get_current_user),
):
    db = get_db()
    query = {"user_id": current_user["id"]}
    if status:
        query["status"] = status
    requests = [
        serialize_request(item)
        async for item in db.withdraw_requests.find(query).sort("created_at", -1)
    ]
    return {"requests": requests}


@router.get("/requests")
async def get_my_withdrawals(
    status: Optional[str] = Query(default=None),
    current_user=Depends(get_current_user),
):
    return await list_my_withdrawals(status, current_user)


@wallet_router.get("/withdrawals")
async def get_my_withdrawals_wallet(
    status: Optional[str] = Query(default=None),
    current_user=Depends(get_current_user),
):
    return await list_my_withdrawals(status, current_user)


admin_router = APIRouter(prefix="/api/admin/withdraw", tags=["withdrawals"])
admin_plural_router = APIRouter(prefix="/api/admin/withdrawals", tags=["withdrawals"])


async def require_admin(current_user=Depends(get_current_user)):
    if not bool(current_user.get("is_admin", False) or current_user.get("role") == "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


async def list_admin_withdrawals(admin=Depends(require_admin)):
    db = get_db()
    requests = [
        serialize_request(item)
        async for item in db.withdraw_requests.find({}).sort("created_at", -1)
    ]
    return {"requests": requests}


@admin_router.get("/requests")
async def get_withdrawals(admin=Depends(require_admin)):
    return await list_admin_withdrawals(admin)


@admin_plural_router.get("")
@admin_plural_router.get("/")
async def get_withdrawals_plural(admin=Depends(require_admin)):
    return await list_admin_withdrawals(admin)


async def approve_withdrawal_impl(request_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(request_id)
    request_doc = await db.withdraw_requests.find_one({"_id": object_id})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")
    if request_doc.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Withdrawal request already processed")

    debited_wallet = await debit_wallet(
        db,
        user_id=request_doc["user_id"],
        amount=int(request_doc["amount"] or 0),
        reason=f"Withdrawal approved to {request_doc['upi']}",
        transaction_type="withdraw",
        source_type="withdrawal",
        source_id=request_id,
        metadata={"upi": request_doc["upi"], "approved_by": admin["id"]},
    )
    if not debited_wallet:
        raise HTTPException(status_code=400, detail="User no longer has sufficient wallet balance")

    updated = await db.withdraw_requests.find_one_and_update(
        {"_id": object_id, "status": "pending"},
        {
            "$set": {
                "status": "approved",
                "admin_note": "Approved",
                "approved_by": admin["id"],
                "updated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return {"message": "Withdrawal approved", "request": serialize_request(updated)}


@admin_router.put("/{request_id}/approve")
async def approve_withdrawal(request_id: str, admin=Depends(require_admin)):
    return await approve_withdrawal_impl(request_id, admin)


@admin_plural_router.post("/{request_id}/approve")
async def approve_withdrawal_plural(request_id: str, admin=Depends(require_admin)):
    return await approve_withdrawal_impl(request_id, admin)


async def reject_withdrawal_impl(request_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(request_id)
    updated = await db.withdraw_requests.find_one_and_update(
        {"_id": object_id, "status": "pending"},
        {
            "$set": {
                "status": "rejected",
                "admin_note": "Rejected",
                "rejected_by": admin["id"],
                "updated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Pending withdrawal request not found")
    return {"message": "Withdrawal rejected", "request": serialize_request(updated)}


@admin_router.put("/{request_id}/reject")
async def reject_withdrawal(request_id: str, admin=Depends(require_admin)):
    return await reject_withdrawal_impl(request_id, admin)


@admin_plural_router.post("/{request_id}/reject")
async def reject_withdrawal_plural(request_id: str, admin=Depends(require_admin)):
    return await reject_withdrawal_impl(request_id, admin)
