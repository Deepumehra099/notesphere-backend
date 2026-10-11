from bson import ObjectId
from datetime import datetime, time, timezone
from fastapi import APIRouter, Depends, Query

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import ensure_wallet

router = APIRouter(prefix="/api/wallet", tags=["wallet"])
legacy_router = APIRouter(prefix="/api/tokens", tags=["tokens"])


async def build_wallet_payload(current_user):
    db = get_db()
    user = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"xp": 1, "streak": 1, "credits_balance": 1})
    wallet = await ensure_wallet(db, current_user["id"])
    balance = int(wallet.get("available_balance", 0) or 0)
    total_held_balance = int(wallet.get("held_balance", 0) or 0)
    deposit_criteria = {
        "$or": [
            {"$eq": ["$category", "deposit"]},
            {"$in": ["$source_type", ["payment", "payment_order", "topup", "deposit"]]},
            {
                "$regexMatch": {
                    "input": {"$ifNull": ["$reason", ""]},
                    "regex": "top[- ]?up",
                    "options": "i",
                }
            },
        ]
    }
    withdrawal_criteria = {
        "$or": [
            {"$eq": ["$category", "withdraw"]},
            {"$in": ["$source_type", ["withdrawal", "withdraw"]]},
            {"$eq": ["$transaction_type", "withdraw"]},
        ]
    }
    [transaction_totals] = [
        item
        async for item in db.transactions.aggregate(
            [
                {"$match": {"user_id": current_user["id"], "status": {"$in": ["completed", "approved", "success"]}}},
                {
                    "$group": {
                        "_id": None,
                        "total_deposited": {
                            "$sum": {"$cond": [deposit_criteria, {"$abs": {"$ifNull": ["$amount", 0]}}, 0]}
                        },
                        "total_credited": {
                            "$sum": {
                                "$cond": [
                                    {"$and": [
                                        {"$eq": ["$type", "credit"]},
                                        {"$not": [deposit_criteria]},
                                        {"$in": ["$transaction_type", ["earn", "release"]]},
                                    ]},
                                    {"$abs": {"$ifNull": ["$amount", 0]}},
                                    0,
                                ]
                            }
                        },
                        "total_withdrawn": {
                            "$sum": {"$cond": [withdrawal_criteria, {"$abs": {"$ifNull": ["$amount", 0]}}, 0]}
                        },
                    }
                },
            ]
        )
    ] or [{}]
    pending_amount = 0
    async for withdrawal in db.withdraw_requests.find(
        {
            "user_id": current_user["id"],
            "funds_reserved": True,
            "status": {"$in": ["pending", "processing", "completing", "rejecting"]},
        },
        {"amount": 1},
    ):
        pending_amount += int(withdrawal.get("amount", 0) or 0)
    held_balance = max(0, total_held_balance - pending_amount)
    return {
        "balance": balance,
        "wallet_balance": balance,
        "available_balance": balance,
        "held_balance": held_balance,
        "freezeBalance": pending_amount,
        "pendingBalance": pending_amount,
        "pending_balance": pending_amount,
        "pendingAmount": pending_amount,
        "total_balance": balance + total_held_balance,
        "total_earned": int(transaction_totals.get("total_credited", 0) or 0),
        "totalEarned": int(transaction_totals.get("total_credited", 0) or 0),
        "total_spent": int(wallet.get("lifetime_spent", 0) or 0),
        "totalDeposited": int(transaction_totals.get("total_deposited", 0) or 0),
        "totalWithdrawn": int(transaction_totals.get("total_withdrawn", 0) or 0),
        "tokens": int((user or {}).get("credits_balance", 0) or 0),
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

    if category == "deposit":
        activity_category = "deposits"
    elif category == "withdraw":
        activity_category = "withdrawals"
    elif source_type == "refund" or raw_type == "refund":
        activity_category = "refunds"
    elif raw_type in {"earn", "release"}:
        activity_category = "earnings"
    else:
        activity_category = "purchases"

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
        "activity_category": activity_category,
        "status": status,
        "title": title,
        "reason": reason,
        "date": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "createdAt": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "source_type": source_type,
        "source_id": transaction.get("source_id", ""),
    }


def parse_date_filter(value: str | None):
    if not value:
        return None
    try:
        date_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            date_value = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.min, tzinfo=timezone.utc)
        except ValueError:
            return None

    start = datetime.combine(date_value.date(), time.min, tzinfo=timezone.utc)
    end = datetime.combine(date_value.date(), time.max, tzinfo=timezone.utc)
    return {"$gte": start, "$lte": end}


async def build_transactions_payload(page: int, limit: int, current_user, type_filter: str | None = None, category: str | None = None, date: str | None = None):
    db = get_db()
    skip = (page - 1) * limit
    # Token rewards and spending live only in credit_transactions. Keep legacy
    # reward rows out of the INR transaction history.
    query = {
        "user_id": current_user["id"],
        "source_type": {"$nin": ["signup", "referral", "note_reward", "daily_reward", "achievement_reward", "token", "credits"]},
        "transaction_type": {"$nin": ["signup_reward", "referral_reward", "note_reward", "daily_reward", "achievement_reward", "credit_spend", "credit_refund"]},
    }
    if type_filter and type_filter in {"credit", "debit"}:
        query["type"] = type_filter
    if category and category != "all":
        query["category"] = category
    date_filter = parse_date_filter(date)
    if date_filter:
        query["created_at"] = date_filter
    cursor = (
        db.transactions.find(query, {"_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    transactions = []
    async for transaction in cursor:
        transactions.append(normalize_transaction(transaction))
    total = await db.transactions.count_documents(query)
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
    user = await get_db().users.find_one({"_id": ObjectId(current_user["id"])}, {"credits_balance": 1})
    balance = int((user or {}).get("credits_balance", 0) or 0)
    return {"balance": balance, "available_tokens": balance, "tokens": balance, "currency": "Tokens"}


@router.get("/transactions")
async def get_transactions(
    page: int = 1,
    limit: int = 20,
    type: str | None = Query(default=None),
    category: str | None = Query(default=None),
    date: str | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    return await build_transactions_payload(page, limit, current_user, type, category, date)


@legacy_router.get("/transactions")
async def get_transactions_legacy(
    page: int = 1,
    limit: int = 20,
    type: str | None = Query(default=None),
    category: str | None = Query(default=None),
    date: str | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    db = get_db()
    page = min(max(int(page or 1), 1), 100)
    limit = min(max(int(limit or 20), 1), 100)
    query = {"user_id": current_user["id"]}
    if type == "credit":
        query["amount"] = {"$gt": 0}
    elif type == "debit":
        query["amount"] = {"$lt": 0}
    transactions = []
    cursor = db.credit_transactions.find(query).sort("created_at", -1).skip((page - 1) * limit).limit(limit)
    async for row in cursor:
        created_at = row.get("created_at")
        transactions.append({
            "id": str(row.get("_id", "")),
            "type": row.get("type", ""),
            "amount": abs(int(row.get("amount", 0) or 0)),
            "signed_amount": int(row.get("amount", 0) or 0),
            "currency": "Tokens",
            "description": row.get("description", ""),
            "date": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "createdAt": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        })
    return {"transactions": transactions, "page": page, "limit": limit, "hasMore": len(transactions) == limit}
