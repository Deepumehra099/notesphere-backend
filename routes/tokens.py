from bson import ObjectId
from fastapi import APIRouter, Depends

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import ensure_wallet

router = APIRouter(prefix="/api/wallet", tags=["wallet"])
legacy_router = APIRouter(prefix="/api/tokens", tags=["tokens"])


async def build_wallet_payload(current_user):
    db = get_db()
    user = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"xp": 1, "streak": 1})
    wallet = await ensure_wallet(db, current_user["id"])
    balance = int(wallet.get("available_balance", 0) or 0)
    held_balance = int(wallet.get("held_balance", 0) or 0)
    return {
        "balance": balance,
        "wallet_balance": balance,
        "available_balance": balance,
        "held_balance": held_balance,
        "pending_balance": held_balance,
        "pendingAmount": held_balance,
        "total_balance": balance + held_balance,
        "total_earned": int(wallet.get("lifetime_earned", 0) or 0),
        "total_spent": int(wallet.get("lifetime_spent", 0) or 0),
        "totalDeposited": int(wallet.get("lifetime_earned", 0) or 0),
        "totalWithdrawn": int(wallet.get("lifetime_spent", 0) or 0),
        "tokens": balance,
        "xp": user.get("xp", 0),
        "streak": user.get("streak", 0),
    }


def normalize_transaction(transaction: dict) -> dict:
    amount = int(transaction.get("amount", 0) or 0)
    raw_type = str(transaction.get("transaction_type", transaction.get("type", "")) or "").lower()
    normalized_type = str(transaction.get("type", "") or "").lower()
    source_type = str(transaction.get("source_type", "") or "").lower()
    reason = str(transaction.get("reason", "") or "").strip()
    raw_status = str(transaction.get("status", "completed") or "completed").lower()
    explicit_category = str(transaction.get("category", "") or "").lower()
    created_at = transaction.get("created_at")

    normalized_type = normalized_type if normalized_type in {"credit", "debit"} else ("credit" if amount >= 0 else "debit")

    if explicit_category in {"deposit", "withdraw", "task", "purchase"}:
        category = explicit_category
    elif source_type in {"payment", "topup", "deposit"} or "top-up" in reason.lower() or "top up" in reason.lower():
        category = "deposit"
    elif source_type in {"withdrawal", "withdraw"} or raw_type == "withdraw" or "withdraw" in reason.lower():
        category = "withdraw"
    elif source_type in {"note_purchase", "task_fee", "purchase"} or raw_type in {"spend", "hold"}:
        category = "purchase"
    elif raw_type in {"earn", "refund", "release"}:
        category = "task"
    else:
        category = "deposit" if amount >= 0 else "purchase"

    if category == "deposit":
        title = "Deposit"
    elif category == "withdraw":
        title = "Withdraw"
    elif category == "task":
        title = "Task Earned" if amount >= 0 else "Task Payment"
    else:
        title = "Purchase"

    if raw_status == "held":
        status = "pending"
    elif raw_status in {"completed", "approved", "success"}:
        status = "completed"
    elif raw_status in {"rejected", "failed"}:
        status = "failed"
    else:
        status = "pending"

    return {
        "amount": abs(amount),
        "signed_amount": amount,
        "type": normalized_type,
        "category": category,
        "status": status,
        "title": title,
        "reason": reason,
        "date": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "createdAt": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "source_type": source_type,
        "source_id": transaction.get("source_id", ""),
    }


async def build_transactions_payload(page: int, limit: int, current_user):
    db = get_db()
    skip = (page - 1) * limit
    cursor = (
        db.transactions.find({"user_id": current_user["id"]}, {"_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    transactions = []
    async for transaction in cursor:
        transactions.append(normalize_transaction(transaction))
    total = await db.transactions.count_documents({"user_id": current_user["id"]})
    return {
        "transactions": transactions,
        "total": total,
        "page": page,
        "limit": limit,
        "hasMore": skip + len(transactions) < total,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "hasMore": skip + len(transactions) < total,
        },
    }


@router.get("")
@router.get("/")
@router.get("/balance")
@router.get("/wallet")
async def get_wallet(current_user=Depends(get_current_user)):
    return await build_wallet_payload(current_user)


@legacy_router.get("/wallet")
async def get_wallet_legacy(current_user=Depends(get_current_user)):
    return await build_wallet_payload(current_user)


@router.get("/transactions")
async def get_transactions(
    page: int = 1,
    limit: int = 20,
    current_user=Depends(get_current_user),
):
    return await build_transactions_payload(page, limit, current_user)


@legacy_router.get("/transactions")
async def get_transactions_legacy(
    page: int = 1,
    limit: int = 20,
    current_user=Depends(get_current_user),
):
    return await build_transactions_payload(page, limit, current_user)
