from datetime import datetime, timezone
import re
from typing import Literal, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import commit_held_funds, debit_wallet, hold_wallet_funds, refund_held_funds

router = APIRouter(prefix="/api/withdraw", tags=["withdrawals"])
wallet_router = APIRouter(prefix="/api/wallet", tags=["withdrawals"])

MIN_WITHDRAW_AMOUNT = 100


class WithdrawRequestInput(BaseModel):
    amount: int = Field(..., ge=MIN_WITHDRAW_AMOUNT, le=50_000)
    method: Literal["upi", "bank"] = "upi"
    upi: str = Field(default="", max_length=120)
    account_name: str = Field(default="", max_length=140)
    account_number: str = Field(default="", max_length=40)
    ifsc: str = Field(default="", max_length=20)


def parse_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Withdrawal request not found") from exc


def serialize_request(item: dict) -> dict:
    created_at = item.get("created_at")
    updated_at = item.get("updated_at")
    status = item.get("status", "pending")
    status = "processing" if status in {"completing", "rejecting"} else status
    return {
        "id": str(item.get("_id")),
        "user_id": item.get("user_id", ""),
        "user_name": item.get("user_name", ""),
        "amount": int(item.get("amount", 0) or 0),
        "method": item.get("method", "upi"),
        "upi": item.get("upi", ""),
        "account_name": item.get("account_name", ""),
        "account_number": item.get("account_number", ""),
        "ifsc": item.get("ifsc", ""),
        "account_ref": item.get("account_number", "") if item.get("method") == "bank" else item.get("upi", ""),
        "reference_id": str(item.get("_id", "")),
        "status": status,
        "admin_note": item.get("admin_note", ""),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


async def create_withdrawal_request(data: WithdrawRequestInput, current_user=Depends(get_current_user)):
    db = get_db()
    if data.amount > 50_000:
        raise HTTPException(status_code=400, detail="Maximum withdrawal is ₹50,000 per request")

    if data.method == "upi" and not re.fullmatch(r"[^\s@]+@[^\s@]+", data.upi.strip()):
        raise HTTPException(status_code=400, detail="Enter a valid UPI ID")
    if data.method == "bank":
        if not data.account_name.strip() or not re.fullmatch(r"\d{6,20}", data.account_number.strip()):
            raise HTTPException(status_code=400, detail="Enter a valid account name and account number")
        if not re.fullmatch(r"[A-Za-z]{4}0[A-Za-z0-9]{6}", data.ifsc.strip()):
            raise HTTPException(status_code=400, detail="Enter a valid IFSC code")

    pending_total = await db.withdraw_requests.count_documents(
        {"user_id": current_user["id"], "status": {"$in": ["pending", "processing", "completing", "rejecting"]}}
    )
    if pending_total > 0:
        raise HTTPException(status_code=400, detail="You already have a pending withdrawal request")

    now = datetime.now(timezone.utc)
    request_id = ObjectId()
    request_doc = {
        "_id": request_id,
        "user_id": current_user["id"],
        "user_name": current_user.get("name", "User"),
        "amount": data.amount,
        "method": data.method,
        "upi": data.upi.strip() if data.method == "upi" else "",
        "account_name": data.account_name.strip(),
        "account_number": data.account_number.strip(),
        "ifsc": data.ifsc.strip().upper(),
        "status": "pending",
        "funds_reserved": True,
        "admin_note": "",
        "created_at": now,
        "updated_at": now,
    }
    held_wallet = await hold_wallet_funds(
        db,
        user_id=current_user["id"],
        amount=data.amount,
        reason=f"Withdrawal requested via {data.method}",
        source_type="withdrawal",
        source_id=str(request_id),
        metadata={"method": data.method},
    )
    if not held_wallet:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    try:
        await db.withdraw_requests.insert_one(request_doc)
    except Exception:
        refunded_wallet = await refund_held_funds(
            db,
            user_id=current_user["id"],
            amount=data.amount,
            reason="Withdrawal request could not be created",
            source_type="withdrawal",
            source_id=str(request_id),
        )
        if not refunded_wallet:
            raise RuntimeError("Unable to refund wallet after withdrawal request creation failed")
        await db.transactions.update_one(
            {
                "user_id": current_user["id"],
                "source_type": "withdrawal",
                "source_id": str(request_id),
                "status": "pending",
            },
            {"$set": {"status": "failed", "reason": "Withdrawal request could not be created"}},
        )
        raise

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
    updated = await db.withdraw_requests.find_one_and_update(
        {"_id": object_id, "status": "pending"},
        {"$set": {"status": "processing", "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        exists = await db.withdraw_requests.find_one({"_id": object_id}, {"_id": 1})
        if not exists:
            raise HTTPException(status_code=404, detail="Withdrawal request not found")
        raise HTTPException(status_code=400, detail="Withdrawal request already processed")
    return {"message": "Withdrawal is processing", "request": serialize_request(updated)}


async def complete_withdrawal_impl(request_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(request_id)
    request_doc = await db.withdraw_requests.find_one_and_update(
        {"_id": object_id, "status": "processing"},
        {"$set": {"status": "completing", "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.BEFORE,
    )
    if not request_doc:
        exists = await db.withdraw_requests.find_one({"_id": object_id}, {"_id": 1})
        if not exists:
            raise HTTPException(status_code=404, detail="Withdrawal request not found")
        raise HTTPException(status_code=400, detail="Withdrawal is not processing")

    if request_doc.get("funds_reserved"):
        settled_wallet = await commit_held_funds(
            db,
            user_id=request_doc["user_id"],
            amount=int(request_doc["amount"] or 0),
            source_type="withdrawal",
            source_id=request_id,
        )
    else:
        settled_wallet = await debit_wallet(
            db,
            user_id=request_doc["user_id"],
            amount=int(request_doc["amount"] or 0),
            reason=f"Withdrawal approved via {request_doc.get('method', 'upi')}",
            transaction_type="withdraw",
            source_type="withdrawal",
            source_id=request_id,
            metadata={"approved_by": admin["id"]},
        )
    if not settled_wallet:
        await db.withdraw_requests.update_one(
            {"_id": object_id, "status": "completing"},
            {"$set": {"status": "processing", "updated_at": datetime.now(timezone.utc)}},
        )
        raise HTTPException(status_code=400, detail="User no longer has sufficient wallet balance")

    updated = await db.withdraw_requests.find_one_and_update(
        {"_id": object_id, "status": "completing"},
        {
            "$set": {
                "status": "completed",
                "admin_note": "Withdrawal completed",
                "completed_by": admin["id"],
                "completed_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return {"message": "Withdrawal completed", "request": serialize_request(updated)}


@admin_router.put("/{request_id}/approve")
async def approve_withdrawal(request_id: str, admin=Depends(require_admin)):
    return await approve_withdrawal_impl(request_id, admin)


@admin_plural_router.post("/{request_id}/approve")
async def approve_withdrawal_plural(request_id: str, admin=Depends(require_admin)):
    return await approve_withdrawal_impl(request_id, admin)


@admin_router.put("/{request_id}/complete")
async def complete_withdrawal(request_id: str, admin=Depends(require_admin)):
    return await complete_withdrawal_impl(request_id, admin)


@admin_plural_router.post("/{request_id}/complete")
async def complete_withdrawal_plural(request_id: str, admin=Depends(require_admin)):
    return await complete_withdrawal_impl(request_id, admin)


async def reject_withdrawal_impl(request_id: str, admin=Depends(require_admin)):
    db = get_db()
    object_id = parse_object_id(request_id)
    request_doc = await db.withdraw_requests.find_one_and_update(
        {"_id": object_id, "status": "pending"},
        {"$set": {"status": "rejecting", "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.BEFORE,
    )
    if not request_doc:
        raise HTTPException(status_code=404, detail="Pending withdrawal request not found")

    if request_doc.get("funds_reserved"):
        refunded_wallet = await refund_held_funds(
            db,
            user_id=request_doc["user_id"],
            amount=int(request_doc["amount"] or 0),
            reason="Withdrawal rejected",
            source_type="withdrawal",
            source_id=request_id,
            metadata={"rejected_by": admin["id"]},
        )
        if not refunded_wallet:
            await db.withdraw_requests.update_one(
                {"_id": object_id, "status": "rejecting"},
                {"$set": {"status": "pending", "updated_at": datetime.now(timezone.utc)}},
            )
            raise HTTPException(status_code=400, detail="Unable to refund held withdrawal amount")

        await db.transactions.update_one(
            {
                "user_id": request_doc["user_id"],
                "source_type": "withdrawal",
                "source_id": request_id,
                "status": "pending",
            },
            {"$set": {"status": "failed", "reason": "Withdrawal rejected"}},
        )
    updated = await db.withdraw_requests.find_one_and_update(
        {"_id": object_id, "status": "rejecting"},
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
    return {"message": "Withdrawal rejected", "request": serialize_request(updated)}


@admin_router.put("/{request_id}/reject")
async def reject_withdrawal(request_id: str, admin=Depends(require_admin)):
    return await reject_withdrawal_impl(request_id, admin)


@admin_plural_router.post("/{request_id}/reject")
async def reject_withdrawal_plural(request_id: str, admin=Depends(require_admin)):
    return await reject_withdrawal_impl(request_id, admin)
