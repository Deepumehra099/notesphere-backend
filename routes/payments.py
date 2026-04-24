import logging
import os
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import credit_wallet

router = APIRouter(prefix="/api/payments", tags=["payments"])
payment_router = APIRouter(prefix="/api/payment", tags=["payments"])
db = get_db()
logger = logging.getLogger(__name__)

TOKEN_PACKAGES = [
    {"id": "pack_50", "tokens": 50, "price": 49, "label": "50 Wallet Credits", "popular": False},
    {"id": "pack_100", "tokens": 100, "price": 89, "label": "100 Wallet Credits", "popular": True},
    {"id": "pack_250", "tokens": 250, "price": 199, "label": "250 Wallet Credits", "popular": False},
    {"id": "pack_500", "tokens": 500, "price": 349, "label": "500 Wallet Credits", "popular": False},
]

SUBSCRIPTION_PLANS = [
    {
        "id": "elite_buyer_monthly",
        "role": "eliteBuyer",
        "label": "Elite Buyer",
        "price": 299,
        "period": "monthly",
        "features": ["Priority access", "Special badge", "Premium content"],
    },
    {
        "id": "elite_seller_monthly",
        "role": "eliteSeller",
        "label": "Elite Seller",
        "price": 499,
        "period": "monthly",
        "features": ["Direct contact unlock", "Higher visibility", "Earnings boost"],
    },
]


class CreateOrderInput(BaseModel):
    package_id: str


class VerifyPaymentInput(BaseModel):
    order_id: str
    payment_id: str = ""
    signature: str = ""


class SubscriptionInput(BaseModel):
    plan_id: str


@router.get("/packages")
async def get_packages(current_user=Depends(get_current_user)):
    packages = []
    for package in TOKEN_PACKAGES:
        packages.append({**package, "credits": package["tokens"]})
    return {"packages": packages}


@router.get("/subscription-plans")
async def get_subscription_plans(current_user=Depends(get_current_user)):
    return {"plans": SUBSCRIPTION_PLANS}


@router.post("/create-order")
async def create_order(data: CreateOrderInput, current_user=Depends(get_current_user)):
    package = next((p for p in TOKEN_PACKAGES if p["id"] == data.package_id), None)
    if not package:
        raise HTTPException(status_code=400, detail="Invalid package")

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")

    if not key_id or not key_secret:
        raise HTTPException(
            status_code=503,
            detail="Online payments are not configured yet. Add Razorpay keys to enable wallet top-ups.",
        )

    import razorpay

    client = razorpay.Client(auth=(key_id, key_secret))
    try:
        razor_order = client.order.create(
            {
                "amount": package["price"] * 100,
                "currency": "INR",
                "payment_capture": 1,
                "receipt": f"tok_{current_user['id'][:8]}_{package['id']}",
            }
        )
        await db.payment_orders.insert_one(
            {
                "order_id": razor_order["id"],
                "user_id": current_user["id"],
                "package_id": package["id"],
                "tokens": package["tokens"],
                "amount": package["price"] * 100,
                "currency": "INR",
                "status": "created",
                "invoice_id": f"INV-WAL-{current_user['id'][:4]}-{package['id'][-3:]}",
                "metadata": {
                    "email": current_user.get("email", ""),
                    "phone": current_user.get("phone", ""),
                    "name": current_user.get("name", ""),
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
        return {
            "order_id": razor_order["id"],
            "amount": razor_order["amount"],
            "currency": razor_order["currency"],
            "key_id": key_id,
            "package": package,
        }
    except Exception as e:
        logger.error(f"Razorpay order creation failed: {e}")
        raise HTTPException(status_code=500, detail="Payment service error")


@router.post("/verify")
async def verify_payment(data: VerifyPaymentInput, current_user=Depends(get_current_user)):
    order = await db.payment_orders.find_one({"order_id": data.order_id, "user_id": current_user["id"]})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("status") == "paid":
        return {"message": "Already processed", "tokens_added": order["tokens"]}

    if not data.payment_id or not data.signature:
        raise HTTPException(status_code=400, detail="Payment details are required before wallet credits can be added")

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        raise HTTPException(status_code=503, detail="Online payments are not configured yet")

    import razorpay

    client = razorpay.Client(auth=(key_id, key_secret))
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": data.order_id,
                "razorpay_payment_id": data.payment_id,
                "razorpay_signature": data.signature,
            }
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    tokens = order["tokens"]
    await credit_wallet(
        db,
        user_id=current_user["id"],
        amount=tokens,
        reason=f"Wallet top-up: {tokens} credits",
        transaction_type="top_up",
        source_type="payment_order",
        source_id=data.order_id,
        metadata={"payment_id": data.payment_id, "package_id": order.get("package_id", "")},
    )
    await db.payment_orders.update_one(
        {"order_id": data.order_id},
        {"$set": {"status": "paid", "payment_id": data.payment_id, "paid_at": datetime.now(timezone.utc)}},
    )
    return {"message": "Payment verified", "tokens_added": tokens, "credits_added": tokens}


async def create_subscription_order(data: SubscriptionInput, current_user):
    plan = next((p for p in SUBSCRIPTION_PLANS if p["id"] == data.plan_id), None)
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid subscription plan")

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        raise HTTPException(
            status_code=503,
            detail="Online payments are not configured yet. Add Razorpay keys to enable subscriptions.",
        )

    import razorpay

    client = razorpay.Client(auth=(key_id, key_secret))
    try:
        razor_order = client.order.create(
            {
                "amount": plan["price"] * 100,
                "currency": "INR",
                "payment_capture": 1,
                "receipt": f"sub_{current_user['id'][:8]}_{plan['id']}",
            }
        )
        await db.subscription_orders.insert_one(
            {
                "order_id": razor_order["id"],
                "user_id": current_user["id"],
                "plan_id": plan["id"],
                "plan_name": plan["label"],
                "role": plan["role"],
                "amount": razor_order["amount"],
                "currency": razor_order["currency"],
                "status": "created",
                "invoice_id": f"INV-SUB-{current_user['id'][:4]}-{plan['id'][-4:]}",
                "metadata": {
                    "email": current_user.get("email", ""),
                    "phone": current_user.get("phone", ""),
                    "name": current_user.get("name", ""),
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
        return {
            "order_id": razor_order["id"],
            "amount": razor_order["amount"],
            "currency": razor_order["currency"],
            "key_id": key_id,
            "plan": plan,
        }
    except Exception as e:
        logger.error(f"Razorpay subscription order creation failed: {e}")
        raise HTTPException(status_code=500, detail="Payment service error")


@router.post("/subscribe")
async def subscribe(data: SubscriptionInput, current_user=Depends(get_current_user)):
    return await create_subscription_order(data, current_user)


@payment_router.post("/subscribe")
async def subscribe_alias(data: SubscriptionInput, current_user=Depends(get_current_user)):
    return await create_subscription_order(data, current_user)


@router.post("/subscribe/verify")
async def verify_subscription(data: VerifyPaymentInput, current_user=Depends(get_current_user)):
    order = await db.subscription_orders.find_one({"order_id": data.order_id, "user_id": current_user["id"]})
    if not order:
        raise HTTPException(status_code=404, detail="Subscription order not found")
    if order.get("status") == "paid":
        return {"message": "Already processed", "role": order.get("role", "")}

    if not data.payment_id or not data.signature:
        raise HTTPException(status_code=400, detail="Payment details are required before subscription can be activated")

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        raise HTTPException(status_code=503, detail="Online payments are not configured yet")

    import razorpay

    client = razorpay.Client(auth=(key_id, key_secret))
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": data.order_id,
                "razorpay_payment_id": data.payment_id,
                "razorpay_signature": data.signature,
            }
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    role = order.get("role", "")
    updates = {}
    if role == "eliteBuyer":
        updates.update({"role": "eliteBuyer", "elite_buyer_active": True})
    if role == "eliteSeller":
        updates.update({"elite_seller_status": "pending_approval"})

    if updates:
        await db.users.update_one({"_id": ObjectId(current_user["id"])}, {"$set": updates})

    await db.subscription_orders.update_one(
        {"order_id": data.order_id},
        {
            "$set": {
                "status": "paid",
                "payment_id": data.payment_id,
                "paid_at": datetime.now(timezone.utc),
                "approval_status": "pending" if role == "eliteSeller" else "approved",
            }
        },
    )
    return {
        "message": "Subscription activated" if role == "eliteBuyer" else "Subscription received and pending admin approval",
        "role": role,
    }
