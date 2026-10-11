from datetime import datetime, timezone
import math
import os
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_platform_commission_percent() -> float:
    configured = float(os.getenv("PLATFORM_COMMISSION_PERCENT", "5"))
    if not math.isfinite(configured):
        raise ValueError("PLATFORM_COMMISSION_PERCENT must be a finite number")
    return min(100, max(0, configured))


def calculate_commission(amount: int) -> int:
    return max(0, int(round(amount * get_platform_commission_percent() / 100)))


async def sync_legacy_user_balance(db, user_id: str, available_balance: int) -> None:
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"wallet_balance": available_balance}},
    )


async def ensure_wallet(db, user_id: str, *, seed_balance: int | None = None) -> dict:
    # The legacy `users.tokens` field is a reward balance. Never seed INR from it.
    default_balance = max(0, int(seed_balance or 0))
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
    idempotency_key: str = "",
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

    document = {
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
    if idempotency_key:
        document["idempotency_key"] = idempotency_key
        await db.transactions.update_one(
            {"idempotency_key": idempotency_key},
            {"$setOnInsert": document},
            upsert=True,
        )
    else:
        await db.transactions.insert_one(document)


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
    idempotency_key: str = "",
) -> dict:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    await ensure_wallet(db, user_id)
    query = {"user_id": user_id}
    update = {
            "$inc": {
                "available_balance": amount,
                "lifetime_earned": amount,
            },
            "$set": {"updated_at": utc_now()},
        }
    if idempotency_key:
        query["credit_operation_keys"] = {"$ne": idempotency_key}
        update["$addToSet"] = {"credit_operation_keys": idempotency_key}
    wallet = await db.wallets.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if not wallet and idempotency_key:
        wallet = await db.wallets.find_one({"user_id": user_id})
        if not wallet or idempotency_key not in wallet.get("credit_operation_keys", []):
            raise RuntimeError("Wallet credit could not be applied")
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
        idempotency_key=idempotency_key,
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


async def commit_held_funds(
    db,
    *,
    user_id: str,
    amount: int,
    source_type: str,
    source_id: str,
) -> dict | None:
    if amount < 0:
        raise ValueError("amount must be non-negative")

    wallet = await db.wallets.find_one_and_update(
        {
            "user_id": user_id,
            "held_balance": {"$gte": amount},
            "committed_withdrawal_keys": {"$ne": source_id},
        },
        {
            "$inc": {"held_balance": -amount},
            "$addToSet": {"committed_withdrawal_keys": source_id},
            "$set": {"updated_at": utc_now()},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not wallet:
        wallet = await db.wallets.find_one({"user_id": user_id})
        if not wallet or source_id not in wallet.get("committed_withdrawal_keys", []):
            return None

    await db.transactions.update_one(
        {"user_id": user_id, "source_type": source_type, "source_id": source_id, "status": "pending"},
        {
            "$set": {
                "status": "completed",
                "transaction_type": "withdraw",
                "reason": "Withdrawal approved",
            }
        },
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
    refund_amount: int = 0,
) -> dict:
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if refund_amount < 0 or refund_amount > amount:
        raise ValueError("refund_amount must be between zero and escrow amount")

    net_amount = amount - refund_amount
    commission = calculate_commission(net_amount)
    seller_payout = max(0, net_amount - commission)

    await ensure_wallet(db, buyer_user_id)
    await ensure_wallet(db, seller_user_id)

    buyer_wallet = await db.wallets.find_one_and_update(
        {
            "user_id": buyer_user_id,
            "held_balance": {"$gte": amount},
            "settled_escrow_keys": {"$ne": source_id},
        },
        {"$inc": {"held_balance": -amount, "lifetime_spent": -refund_amount},
         "$addToSet": {"settled_escrow_keys": source_id},
         "$set": {"updated_at": utc_now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not buyer_wallet:
        buyer_wallet = await db.wallets.find_one({"user_id": buyer_user_id})
        if not buyer_wallet or source_id not in buyer_wallet.get("settled_escrow_keys", []):
            raise ValueError("held funds not available")

    if refund_amount:
        await credit_wallet(
            db,
            user_id=buyer_user_id,
            amount=refund_amount,
            reason="Task dispute refund",
            transaction_type="refund",
            source_type="refund",
            source_id=source_id,
            counterparty_user_id=seller_user_id,
            metadata={**(metadata or {}), "gross_amount": amount},
            idempotency_key=f"task-refund:{source_id}",
        )
    seller_wallet = None
    if seller_payout:
        seller_wallet = await credit_wallet(
            db,
            user_id=seller_user_id,
            amount=seller_payout,
            reason=reason,
            transaction_type="earn",
            source_type=source_type,
            source_id=source_id,
            counterparty_user_id=buyer_user_id,
            metadata={**(metadata or {}), "gross_amount": net_amount, "commission_amount": commission},
            idempotency_key=f"task-release:{source_id}",
        )

    await sync_legacy_user_balance(db, buyer_user_id, int(buyer_wallet.get("available_balance", 0) or 0))
    if seller_wallet:
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
        metadata={**(metadata or {}), "gross_amount": amount, "refund_amount": refund_amount, "commission_amount": commission, "seller_payout": seller_payout},
        idempotency_key=f"task-settlement:{source_id}",
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
    operation_key = f"{source_type}:{source_id}"
    wallet = await db.wallets.find_one_and_update(
        {
            "user_id": user_id,
            "held_balance": {"$gte": amount},
            "refund_operation_keys": {"$ne": operation_key},
        },
        {
            "$inc": {
                "available_balance": amount,
                "held_balance": -amount,
                "lifetime_spent": -amount,
            },
            "$addToSet": {"refund_operation_keys": operation_key},
            "$set": {"updated_at": utc_now()},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not wallet:
        wallet = await db.wallets.find_one({"user_id": user_id})
        if not wallet or operation_key not in wallet.get("refund_operation_keys", []):
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
        idempotency_key=f"refund:{operation_key}",
    )
    return wallet
