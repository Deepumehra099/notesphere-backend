from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from datetime import datetime, timezone
from bson import ObjectId
from utils.db import get_db
from utils.auth_utils import get_current_user
import os
import logging

router = APIRouter(prefix="/api/payments", tags=["payments"])
db = get_db()
logger = logging.getLogger(__name__)

TOKEN_PACKAGES = [
    {"id": "pack_50", "tokens": 50, "price": 49, "label": "50 Tokens", "popular": False},
    {"id": "pack_100", "tokens": 100, "price": 89, "label": "100 Tokens", "popular": True},
    {"id": "pack_250", "tokens": 250, "price": 199, "label": "250 Tokens", "popular": False},
    {"id": "pack_500", "tokens": 500, "price": 349, "label": "500 Tokens", "popular": False},
]

class CreateOrderInput(BaseModel):
    package_id: str

@router.get("/packages")
async def get_packages(current_user=Depends(get_current_user)):
    return {"packages": TOKEN_PACKAGES}

@router.post("/create-order")
async def create_order(data: CreateOrderInput, current_user=Depends(get_current_user)):
    package = next((p for p in TOKEN_PACKAGES if p["id"] == data.package_id), None)
    if not package:
        raise HTTPException(status_code=400, detail="Invalid package")

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")

    if not key_id or not key_secret:
        # Razorpay not configured - simulate order for demo
        order_id = f"order_demo_{int(datetime.now().timestamp())}"
        await db.payment_orders.insert_one({
            "order_id": order_id,
            "user_id": current_user["id"],
            "package_id": package["id"],
            "tokens": package["tokens"],
            "amount": package["price"] * 100,
            "currency": "INR",
            "status": "created",
            "demo_mode": True,
            "created_at": datetime.now(timezone.utc),
        })
        return {
            "order_id": order_id,
            "amount": package["price"] * 100,
            "currency": "INR",
            "key_id": "rzp_test_demo",
            "demo_mode": True,
            "package": package,
        }

    import razorpay
    client = razorpay.Client(auth=(key_id, key_secret))
    try:
        razor_order = client.order.create({
            "amount": package["price"] * 100,
            "currency": "INR",
            "payment_capture": 1,
            "receipt": f"tok_{current_user['id'][:8]}_{package['id']}",
        })
        await db.payment_orders.insert_one({
            "order_id": razor_order["id"],
            "user_id": current_user["id"],
            "package_id": package["id"],
            "tokens": package["tokens"],
            "amount": package["price"] * 100,
            "currency": "INR",
            "status": "created",
            "demo_mode": False,
            "created_at": datetime.now(timezone.utc),
        })
        return {
            "order_id": razor_order["id"],
            "amount": razor_order["amount"],
            "currency": razor_order["currency"],
            "key_id": key_id,
            "demo_mode": False,
            "package": package,
        }
    except Exception as e:
        logger.error(f"Razorpay order creation failed: {e}")
        raise HTTPException(status_code=500, detail="Payment service error")

class VerifyPaymentInput(BaseModel):
    order_id: str
    payment_id: str = ""
    signature: str = ""

@router.post("/verify")
async def verify_payment(data: VerifyPaymentInput, current_user=Depends(get_current_user)):
    order = await db.payment_orders.find_one({"order_id": data.order_id, "user_id": current_user["id"]})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("status") == "paid":
        return {"message": "Already processed", "tokens_added": order["tokens"]}

    if not order.get("demo_mode"):
        key_id = os.environ.get("RAZORPAY_KEY_ID", "")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        if key_id and key_secret:
            import razorpay
            client = razorpay.Client(auth=(key_id, key_secret))
            try:
                client.utility.verify_payment_signature({
                    "razorpay_order_id": data.order_id,
                    "razorpay_payment_id": data.payment_id,
                    "razorpay_signature": data.signature,
                })
            except Exception:
                raise HTTPException(status_code=400, detail="Payment verification failed")

    # Credit tokens
    tokens = order["tokens"]
    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$inc": {"tokens": tokens}}
    )
    await db.transactions.insert_one({
        "user_id": current_user["id"],
        "amount": tokens,
        "type": "earn",
        "reason": f"Purchased {tokens} tokens",
        "created_at": datetime.now(timezone.utc),
    })
    await db.payment_orders.update_one(
        {"order_id": data.order_id},
        {"$set": {"status": "paid", "payment_id": data.payment_id, "paid_at": datetime.now(timezone.utc)}}
    )
    return {"message": "Payment verified", "tokens_added": tokens}
