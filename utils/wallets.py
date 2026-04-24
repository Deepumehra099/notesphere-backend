from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def calculate_commission(amount: int) -> int:
    return max(0, int(round(amount * 0.05)))


async def sync_legacy_user_balance(db, user_id: str, available_balance: int) -> None:
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"tokens": available_balance}},
    )


async def ensure_wallet(db, user_id: str, *, seed_balance: int | None = None) -> dict:
    user = await db.users.find_one({"_id": ObjectId(user_id)}, {"tokens": 1})
    default_balance = seed_balance if seed_balance is not None else int((user or {}).get("tokens", 0) or 0)
    now = utc_now()

    wallet = await db.wallets.find_one_and_update(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "available_balance": default_balance,
                "held_balance": 0,
                "lifetime_earned": max(default_balance, 0),
                "lifetime_spent": 0,
                "created_at": now,
                "updated_at": now,
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    available_balance = int(wallet.get("available_balance", 0) or 0)
    held_balance = int(wallet.get("held_balance", 0) or 0)
    lifetime_earned = int(wallet.get("lifetime_earned", 0) or 0)
    lifetime_spent = int(wallet.get("lifetime_spent", 0) or 0)

    normalized_wallet = {
        **wallet,
        "available_balance": available_balance,
        "held_balance": held_balance,
        "lifetime_earned": lifetime_earned,
        "lifetime_spent": lifetime_spent,
    }
    await sync_legacy_user_balance(db, user_id, available_balance)
    return normalized_wallet


async def record_transaction(
    db,
    *,
    user_id: str,
    amount: int,
    transaction_type: str,
    reason: str,
    status: str = "completed",
    source_type: str = "",
    source_id: str = "",
    counterparty_user_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    normalized_status = "pending" if status in {"held", "pending"} else "failed" if status in {"rejected", "failed"} else "completed"
    if source_type in {"payment", "topup", "deposit"}:
        category = "deposit"
    elif source_type in {"withdrawal", "withdraw"} or transaction_type == "withdraw":
        category = "withdraw"
    elif transaction_type in {"earn", "refund", "release"}:
        category = "task"
    else:
        category = "purchase"

    await db.transactions.insert_one(
        {
            "user_id": user_id,
            "amount": amount,
            "type": "credit" if amount >= 0 else "debit",
            "transaction_type": transaction_type,
            "category": category,
            "reason": reason,
            "status": normalized_status,
            "source_type": source_type,
            "source_id": source_id,
            "counterparty_user_id": counterparty_user_id,
            "metadata": metadata or {},
            "created_at": utc_now(),
        }
    )


async def credit_wallet(
    db,
    *,
    user_id: str,
    amount: int,
    reason: str,
    transaction_type: str = "earn",
    source_type: str = "",
    source_id: str = "",
    counterparty_user_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    await ensure_wallet(db, user_id)
    wallet = await db.wallets.find_one_and_update(
        {"user_id": user_id},
        {
            "$inc": {
                "available_balance": amount,
                "lifetime_earned": amount,
            },
            "$set": {"updated_at": utc_now()},
        },
        return_document=ReturnDocument.AFTER,
    )
    await sync_legacy_user_balance(db, user_id, int(wallet.get("available_balance", 0) or 0))
    await record_transaction(
        db,
        user_id=user_id,
        amount=amount,
        transaction_type=transaction_type,
        reason=reason,
        status="completed",
        source_type=source_type,
        source_id=source_id,
        counterparty_user_id=counterparty_user_id,
        metadata=metadata,
    )
    return wallet


async def debit_wallet(
    db,
    *,
    user_id: str,
    amount: int,
    reason: str,
    transaction_type: str = "spend",
    source_type: str = "",
    source_id: str = "",
    counterparty_user_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict | None:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    await ensure_wallet(db, user_id)
    wallet = await db.wallets.find_one_and_update(
        {
            "user_id": user_id,
            "available_balance": {"$gte": amount},
        },
        {
            "$inc": {
                "available_balance": -amount,
                "lifetime_spent": amount,
            },
            "$set": {"updated_at": utc_now()},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not wallet:
        return None

    await sync_legacy_user_balance(db, user_id, int(wallet.get("available_balance", 0) or 0))
    await record_transaction(
        db,
        user_id=user_id,
        amount=-amount,
        transaction_type=transaction_type,
        reason=reason,
        status="completed",
        source_type=source_type,
        source_id=source_id,
        counterparty_user_id=counterparty_user_id,
        metadata=metadata,
    )
    return wallet


async def hold_wallet_funds(
    db,
    *,
    user_id: str,
    amount: int,
    reason: str,
    source_type: str,
    source_id: str,
    counterparty_user_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict | None:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    await ensure_wallet(db, user_id)
    wallet = await db.wallets.find_one_and_update(
        {
            "user_id": user_id,
            "available_balance": {"$gte": amount},
        },
        {
            "$inc": {
                "available_balance": -amount,
                "held_balance": amount,
                "lifetime_spent": amount,
            },
            "$set": {"updated_at": utc_now()},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not wallet:
        return None

    await sync_legacy_user_balance(db, user_id, int(wallet.get("available_balance", 0) or 0))
    await record_transaction(
        db,
        user_id=user_id,
        amount=-amount,
        transaction_type="hold",
        reason=reason,
        status="held",
        source_type=source_type,
        source_id=source_id,
        counterparty_user_id=counterparty_user_id,
        metadata=metadata,
    )
    return wallet


async def release_held_funds(
    db,
    *,
    buyer_user_id: str,
    seller_user_id: str,
    amount: int,
    reason: str,
    source_type: str,
    source_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    commission = calculate_commission(amount)
    seller_payout = max(0, amount - commission)

    await ensure_wallet(db, buyer_user_id)
    await ensure_wallet(db, seller_user_id)

    buyer_wallet = await db.wallets.find_one_and_update(
        {
            "user_id": buyer_user_id,
            "held_balance": {"$gte": amount},
        },
        {"$inc": {"held_balance": -amount}, "$set": {"updated_at": utc_now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not buyer_wallet:
        raise ValueError("held funds not available")

    seller_wallet = await db.wallets.find_one_and_update(
        {"user_id": seller_user_id},
        {
            "$inc": {
                "available_balance": seller_payout,
                "lifetime_earned": seller_payout,
            },
            "$set": {"updated_at": utc_now()},
        },
        return_document=ReturnDocument.AFTER,
    )

    await sync_legacy_user_balance(db, buyer_user_id, int(buyer_wallet.get("available_balance", 0) or 0))
    await sync_legacy_user_balance(db, seller_user_id, int(seller_wallet.get("available_balance", 0) or 0))

    await record_transaction(
        db,
        user_id=buyer_user_id,
        amount=0,
        transaction_type="release",
        reason=reason,
        status="completed",
        source_type=source_type,
        source_id=source_id,
        counterparty_user_id=seller_user_id,
        metadata={**(metadata or {}), "gross_amount": amount, "commission_amount": commission, "seller_payout": seller_payout},
    )
    await record_transaction(
        db,
        user_id=seller_user_id,
        amount=seller_payout,
        transaction_type="earn",
        reason=reason,
        status="completed",
        source_type=source_type,
        source_id=source_id,
        counterparty_user_id=buyer_user_id,
        metadata={**(metadata or {}), "gross_amount": amount, "commission_amount": commission},
    )
    return {
        "commission_amount": commission,
        "seller_payout": seller_payout,
    }


async def refund_held_funds(
    db,
    *,
    user_id: str,
    amount: int,
    reason: str,
    source_type: str,
    source_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict | None:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    await ensure_wallet(db, user_id)
    wallet = await db.wallets.find_one_and_update(
        {
            "user_id": user_id,
            "held_balance": {"$gte": amount},
        },
        {
            "$inc": {
                "available_balance": amount,
                "held_balance": -amount,
                "lifetime_spent": -amount,
            },
            "$set": {"updated_at": utc_now()},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not wallet:
        return None

    await sync_legacy_user_balance(db, user_id, int(wallet.get("available_balance", 0) or 0))
    await record_transaction(
        db,
        user_id=user_id,
        amount=amount,
        transaction_type="refund",
        reason=reason,
        status="completed",
        source_type=source_type,
        source_id=source_id,
        metadata=metadata,
    )
    return wallet
