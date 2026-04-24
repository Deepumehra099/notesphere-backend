from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth_utils import get_current_user
from utils.db import get_db

orders_router = APIRouter(prefix="/api/orders", tags=["account"])
wishlist_router = APIRouter(prefix="/api/wishlist", tags=["account"])
support_router = APIRouter(prefix="/api/support", tags=["account"])


def parse_note_id(note_id: str) -> ObjectId:
    try:
        return ObjectId(note_id)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc


def isoformat(value):
    return value.isoformat() if isinstance(value, datetime) else value


def serialize_order(order: dict, *, order_type: str) -> dict:
    metadata = order.get("metadata", {})
    amount_paise = int(order.get("amount", 0) or 0)
    return {
        "id": str(order.get("_id")),
        "order_id": order.get("order_id", ""),
        "type": order_type,
        "plan": order.get("plan_name", order.get("package_id", "")),
        "status": order.get("status", "created"),
        "payment_status": order.get("status", "created"),
        "amount": amount_paise / 100 if amount_paise else 0,
        "currency": order.get("currency", "INR"),
        "invoice_id": order.get("invoice_id", f"INV-{str(order.get('_id'))[-6:]}"),
        "created_at": isoformat(order.get("created_at")),
        "paid_at": isoformat(order.get("paid_at")),
        "billing_details": {
            "email": metadata.get("email", ""),
            "phone": metadata.get("phone", ""),
            "name": metadata.get("name", ""),
        },
    }


def serialize_ticket(ticket: dict) -> dict:
    return {
        "id": str(ticket.get("_id")),
        "subject": ticket.get("subject", ""),
        "message": ticket.get("message", ""),
        "status": ticket.get("status", "open"),
        "reply": ticket.get("reply", ""),
        "created_at": isoformat(ticket.get("created_at")),
        "updated_at": isoformat(ticket.get("updated_at")),
    }


class WishlistToggleInput(BaseModel):
    note_id: str = Field(..., min_length=1)


class SupportTicketInput(BaseModel):
    subject: str = Field(..., min_length=3, max_length=120)
    message: str = Field(..., min_length=5, max_length=1200)


@orders_router.get("/user")
async def get_user_orders(current_user=Depends(get_current_user)):
    db = get_db()
    wallet_orders = [
        serialize_order(order, order_type="wallet")
        async for order in db.payment_orders.find({"user_id": current_user["id"]}).sort("created_at", -1)
    ]
    subscription_orders = [
        serialize_order(order, order_type="subscription")
        async for order in db.subscription_orders.find({"user_id": current_user["id"]}).sort("created_at", -1)
    ]
    orders = sorted(
        [*wallet_orders, *subscription_orders],
        key=lambda item: item.get("created_at") or "",
        reverse=True,
    )
    return {"orders": orders}


@wishlist_router.get("")
async def get_wishlist(current_user=Depends(get_current_user)):
    db = get_db()
    user = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"wishlist_note_ids": 1})
    note_ids = [parse_note_id(note_id) for note_id in user.get("wishlist_note_ids", [])] if user else []
    if not note_ids:
      return {"items": []}

    items = []
    async for note in db.notes.find({"_id": {"$in": note_ids}, "status": "approved"}):
        items.append(
            {
                "id": str(note.get("_id")),
                "title": note.get("title", ""),
                "subject": note.get("subject", "General"),
                "price": int(note.get("unlock_cost", 0) or 0),
                "thumbnail_url": note.get("thumbnail_url", ""),
                "description": note.get("description", ""),
            }
        )
    return {"items": items}


@wishlist_router.post("/toggle")
async def toggle_wishlist(data: WishlistToggleInput, current_user=Depends(get_current_user)):
    db = get_db()
    note_object_id = parse_note_id(data.note_id)
    note = await db.notes.find_one({"_id": note_object_id}, {"_id": 1})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    user = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"wishlist_note_ids": 1})
    current_ids = set((user or {}).get("wishlist_note_ids", []))
    note_id = str(note_object_id)
    if note_id in current_ids:
        await db.users.update_one({"_id": ObjectId(current_user["id"])}, {"$pull": {"wishlist_note_ids": note_id}})
        return {"message": "Removed from wishlist", "saved": False}

    await db.users.update_one({"_id": ObjectId(current_user["id"])}, {"$addToSet": {"wishlist_note_ids": note_id}})
    return {"message": "Added to wishlist", "saved": True}


@support_router.get("/faq")
async def get_faq():
    return {
        "faqs": [
            {"question": "How do premium plans work?", "answer": "Elite plans unlock buyer or seller perks after payment and any required approval."},
            {"question": "Where can I see my invoices?", "answer": "Open Buy Packages & My Orders in My Account to see plans, orders, and invoice IDs."},
            {"question": "How do I contact support?", "answer": "Use Raise Ticket in Help & Support and the admin team can reply from the dashboard."},
        ]
    }


@support_router.post("/ticket")
async def create_support_ticket(data: SupportTicketInput, current_user=Depends(get_current_user)):
    db = get_db()
    ticket = {
        "user_id": current_user["id"],
        "user_name": current_user.get("name", "User"),
        "user_email": current_user.get("email", ""),
        "subject": data.subject.strip(),
        "message": data.message.strip(),
        "status": "open",
        "reply": "",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await db.support_tickets.insert_one(ticket)
    ticket["_id"] = result.inserted_id
    return {"message": "Support ticket created", "ticket": serialize_ticket(ticket)}


@support_router.get("/ticket")
async def get_my_support_tickets(current_user=Depends(get_current_user)):
    db = get_db()
    tickets = [
        serialize_ticket(ticket)
        async for ticket in db.support_tickets.find({"user_id": current_user["id"]}).sort("updated_at", -1)
    ]
    return {"tickets": tickets}
