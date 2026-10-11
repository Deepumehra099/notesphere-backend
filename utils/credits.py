import os
import secrets
import string
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument

DEFAULT_CREDIT_SETTINGS = {
    "signup_reward": 50,
    "approved_note_reward": 100,
    "referral_reward": 50,
    "referral_signup_reward": 25,
    "premium_note_price": 0,
}
SUPPORTED_CREDIT_TYPES = {
    "SIGNUP_REWARD", "NOTE_UPLOAD_REWARD", "NOTE_APPROVAL_REWARD", "REFERRAL_REWARD",
    "REFERRAL_SIGNUP_REWARD", "DAILY_REWARD", "PROFILE_REWARD", "ADMIN_REWARD",
    "CREDIT_SPEND", "CREDIT_REFUND", "ADMIN_ADJUSTMENT", "PROMOTION_REWARD",
}


class InsufficientCredits(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def get_credit_settings(db, session=None) -> dict[str, int]:
    settings = await db.credit_settings.find_one({"_id": "global"}, session=session) or {}
    result = {}
    env_names = {
        "signup_reward": "CREDITS_SIGNUP_REWARD",
        "approved_note_reward": "CREDITS_APPROVED_NOTE_REWARD",
        "referral_reward": "CREDITS_REFERRAL_REWARD",
        "referral_signup_reward": "CREDITS_REFERRAL_SIGNUP_REWARD",
        "premium_note_price": "CREDITS_PREMIUM_NOTE_PRICE",
    }
    for key, default in DEFAULT_CREDIT_SETTINGS.items():
        configured = settings.get(key, os.getenv(env_names[key], default))
        try:
            result[key] = max(0, int(configured))
        except (TypeError, ValueError):
            result[key] = default
    return result


async def apply_credit_change(
    db,
    *,
    user_id: str,
    amount: int,
    transaction_type: str,
    description: str,
    idempotency_key: str,
    reference_id: str = "",
    metadata: dict[str, Any] | None = None,
    session=None,
) -> dict:
    if not idempotency_key or amount == 0:
        raise ValueError("A non-zero amount and idempotency key are required")
    if transaction_type not in SUPPORTED_CREDIT_TYPES:
        raise ValueError("Unsupported Credit transaction type")
    user_object_id = ObjectId(user_id)
    prior = await db.credit_transactions.find_one({"idempotency_key": idempotency_key}, session=session)
    if prior:
        user = await db.users.find_one({"_id": user_object_id}, {"credits_balance": 1}, session=session)
        return {"already_applied": True, "balance": int((user or {}).get("credits_balance", 0) or 0), "transaction": prior}

    await db.users.update_one(
        {"_id": user_object_id, "$or": [{"credits_balance": {"$exists": False}}, {"credits_balance": None}]},
        {"$set": {"credits_balance": 0}},
        session=session,
    )
    query: dict[str, Any] = {"_id": user_object_id}
    if amount < 0:
        query["credits_balance"] = {"$gte": -amount}
    user = await db.users.find_one_and_update(
        query,
        {"$inc": {"credits_balance": amount}, "$set": {"credits_updated_at": utc_now()}},
        projection={"credits_balance": 1},
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    if not user:
        exists = await db.users.find_one({"_id": user_object_id}, {"_id": 1}, session=session)
        if not exists:
            raise LookupError("Credit account user was not found")
        raise InsufficientCredits

    balance_after = int(user.get("credits_balance", 0) or 0)
    transaction = {
        "user_id": user_id,
        "type": transaction_type,
        "amount": int(amount),
        "balance_before": balance_after - int(amount),
        "balance_after": balance_after,
        "reference_id": str(reference_id or ""),
        "description": description.strip(),
        "metadata": metadata or {},
        "idempotency_key": idempotency_key,
        "created_at": utc_now(),
    }
    await db.credit_transactions.insert_one(transaction, session=session)
    return {"already_applied": False, "balance": balance_after, "transaction": transaction}


def generate_referral_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "NS" + "".join(secrets.choice(alphabet) for _ in range(8))


async def ensure_referral_code(db, user_id: str) -> str:
    user_object_id = ObjectId(user_id)
    user = await db.users.find_one({"_id": user_object_id}, {"referral_code": 1})
    if not user:
        raise LookupError("User was not found")
    if user.get("referral_code"):
        return user["referral_code"]
    for _ in range(12):
        code = generate_referral_code()
        try:
            result = await db.users.update_one(
                {"_id": user_object_id, "$or": [{"referral_code": {"$exists": False}}, {"referral_code": ""}]},
                {"$set": {"referral_code": code}},
            )
        except Exception as exc:
            if exc.__class__.__name__ != "DuplicateKeyError":
                raise
            continue
        updated = await db.users.find_one({"_id": user_object_id}, {"referral_code": 1})
        if updated and updated.get("referral_code"):
            return updated["referral_code"]
    raise RuntimeError("Could not create a unique referral code")


async def qualify_referral(db, referred_user_id: str, *, session) -> bool:
    referral = await db.referrals.find_one_and_update(
        {"referred_user_id": referred_user_id, "status": {"$in": ["registered", "pending", "qualified"]}},
        {"$set": {"status": "qualified", "qualified_at": utc_now()}},
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    if not referral or referral.get("status") == "rewarded":
        return False
    settings = await get_credit_settings(db, session)
    referrer_id = referral["referrer_user_id"]
    if referrer_id == referred_user_id:
        await db.referrals.update_one(
            {"_id": referral["_id"]}, {"$set": {"status": "fraudulent", "review_reason": "Self referral"}}, session=session
        )
        return False
    await apply_credit_change(
        db,
        user_id=referrer_id,
        amount=settings["referral_reward"],
        transaction_type="REFERRAL_REWARD",
        description="Qualified referral reward",
        idempotency_key=f"referral:{referral['_id']}:referrer",
        reference_id=str(referral["_id"]),
        metadata={"referred_user_id": referred_user_id},
        session=session,
    ) if settings["referral_reward"] else None
    await apply_credit_change(
        db,
        user_id=referred_user_id,
        amount=settings["referral_signup_reward"],
        transaction_type="REFERRAL_SIGNUP_REWARD",
        description="Welcome reward for a qualified referral",
        idempotency_key=f"referral:{referral['_id']}:referred",
        reference_id=str(referral["_id"]),
        metadata={"referrer_user_id": referrer_id},
        session=session,
    ) if settings["referral_signup_reward"] else None
    await db.referrals.update_one(
        {"_id": referral["_id"], "status": "qualified"},
        {"$set": {"status": "rewarded", "rewarded_at": utc_now()}},
        session=session,
    )
    return True
