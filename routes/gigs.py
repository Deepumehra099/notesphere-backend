from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import ensure_wallet, hold_wallet_funds

router = APIRouter(prefix="/api/gigs", tags=["gigs"])


class CreateGigInput(BaseModel):
    title: str = Field(..., min_length=3, max_length=160)
    description: str = Field("", max_length=2000)
    price: int = Field(..., ge=0)


class HireGigInput(BaseModel):
    brief: str = Field("Direct hire from marketplace", max_length=500)
    deadline: str = Field("Flexible", min_length=1, max_length=120)


def parse_object_id(value: str, detail: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail=detail) from exc


def serialize_gig(gig: dict) -> dict:
    created_at = gig.get("created_at")
    return {
        "id": str(gig.get("_id", gig.get("id", ""))),
        "title": gig.get("title", ""),
        "description": gig.get("description", ""),
        "price": gig.get("price", 0),
        "user_id": gig.get("user_id", ""),
        "seller_name": gig.get("seller_name", ""),
        "seller_rating": gig.get("seller_rating", 0),
        "seller_rating_count": gig.get("seller_rating_count", 0),
        "is_featured": bool(gig.get("is_featured", False)),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


@router.post("")
async def create_gig(data: CreateGigInput, current_user=Depends(get_current_user)):
    db = get_db()
    seller = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"task_rating": 1, "task_rating_count": 1})
    gig_doc = {
        "title": data.title.strip(),
        "description": data.description.strip(),
        "price": data.price,
        "user_id": current_user["id"],
        "seller_name": current_user.get("name", "Student"),
        "seller_rating": round(float((seller or {}).get("task_rating", 0) or 0), 1),
        "seller_rating_count": int((seller or {}).get("task_rating_count", 0) or 0),
        "is_featured": False,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.gigs.insert_one(gig_doc)
    gig_doc["_id"] = result.inserted_id
    return {"message": "Gig created", "gig": serialize_gig(gig_doc)}


@router.get("")
async def get_gigs(
    page: int = 1,
    limit: int = 10,
    current_user=Depends(get_current_user),
):
    db = get_db()
    page = max(page, 1)
    limit = max(1, min(limit, 10))
    query = {"user_id": {"$ne": current_user["id"]}}
    projection = {
        "title": 1,
        "description": 1,
        "price": 1,
        "user_id": 1,
        "seller_name": 1,
        "seller_rating": 1,
        "seller_rating_count": 1,
        "created_at": 1,
    }
    skip = (page - 1) * limit
    cursor = db.gigs.find(query, projection).sort("created_at", -1).skip(skip).limit(limit)
    gigs = []
    async for gig in cursor:
        gigs.append(serialize_gig(gig))
    total = await db.gigs.count_documents(query)
    return {
        "gigs": gigs,
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit,
        "has_more": skip + len(gigs) < total,
    }


@router.post("/{gig_id}/hire")
async def hire_gig(gig_id: str, data: HireGigInput, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(gig_id, "Gig not found")
    gig = await db.gigs.find_one({"_id": object_id})
    if not gig:
        raise HTTPException(status_code=404, detail="Gig not found")
    if gig.get("user_id") == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot hire your own gig")

    price = int(gig.get("price", 0) or 0)
    buyer_wallet = await ensure_wallet(db, current_user["id"])
    if price > 0 and int(buyer_wallet.get("available_balance", 0) or 0) < price:
        raise HTTPException(status_code=400, detail="Not enough wallet balance")

    if price > 0:
        held_wallet = await hold_wallet_funds(
            db,
            user_id=current_user["id"],
            amount=price,
            reason=f"Funds held for gig hire: {gig['title']}",
            source_type="gig",
            source_id=gig_id,
            counterparty_user_id=gig["user_id"],
            metadata={"gig_title": gig["title"]},
        )
        if not held_wallet:
            raise HTTPException(status_code=400, detail="Not enough wallet balance")

    now = datetime.now(timezone.utc)
    task_doc = {
        "title": gig["title"],
        "description": data.brief.strip() or gig.get("description", ""),
        "price": price,
        "deadline": data.deadline.strip(),
        "status": "assigned",
        "created_by": current_user["id"],
        "created_by_name": current_user.get("name", "Student"),
        "assigned_to": gig["user_id"],
        "assigned_to_name": gig.get("seller_name", "Student"),
        "created_at": now,
        "accepted_at": now,
        "completed_at": None,
        "escrow_status": "held" if price > 0 else "none",
        "escrow_amount": price,
        "commission_amount": 0,
        "seller_payout": 0,
        "views": 0,
        "clicks": 0,
        "accepts": 0,
        "popularity_score": 0,
        "is_boosted": False,
        "boosted_at": None,
        "buyer_rating": None,
        "buyer_rated_at": None,
        "gig_id": gig_id,
        "gig_hire": True,
    }
    result = await db.tasks.insert_one(task_doc)
    task_id = str(result.inserted_id)

    await db.chats.update_one(
        {"task_id": task_id},
        {
            "$setOnInsert": {
                "task_id": task_id,
                "participants": [current_user["id"], gig["user_id"]],
                "last_message": "Direct hire started",
                "last_message_at": now,
                "created_at": now,
            }
        },
        upsert=True,
    )

    return {
        "message": "Gig hired successfully",
        "gig": serialize_gig(gig),
        "task_id": task_id,
    }
