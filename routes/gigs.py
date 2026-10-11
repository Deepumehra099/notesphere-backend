import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import ensure_wallet, hold_wallet_funds

router = APIRouter(prefix="/api/gigs", tags=["gigs"])
UPLOADS_DIR = Path(__file__).resolve().parents[1] / "uploads" / "gig-portfolios"
MAX_PORTFOLIO_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class CreateGigInput(BaseModel):
    title: str = Field(..., min_length=3, max_length=160)
    description: str = Field("", max_length=2000)
    category: str = Field("General", max_length=80)
    price: int = Field(..., ge=1)
    skills: list[str] = Field(default_factory=list, max_length=20)
    delivery_days: int = Field(7, ge=1, le=365)
    revisions: int = Field(1, ge=0, le=20)
    demo_url: str = Field("", max_length=500)
    availability: str = Field("available", pattern="^(available|limited|unavailable)$")


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
        "category": gig.get("category", "General"),
        "price": gig.get("price", 0),
        "skills": gig.get("skills", []),
        "delivery_days": int(gig.get("delivery_days", 7) or 7),
        "revisions": int(gig.get("revisions", 1) or 0),
        "demo_url": gig.get("demo_url", ""),
        "availability": gig.get("availability", "available"),
        "portfolio_images": gig.get("portfolio_images", []),
        "user_id": gig.get("user_id", ""),
        "seller_name": gig.get("seller_name", ""),
        "seller_rating": gig.get("seller_rating", 0),
        "seller_rating_count": gig.get("seller_rating_count", 0),
        "is_featured": bool(gig.get("is_featured", False)),
        "approval_status": gig.get("approval_status", "approved"),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
    }


async def _save_portfolio_image(upload: UploadFile) -> str:
    extension = Path(upload.filename or "").suffix.lower()
    content_type = (upload.content_type or "").lower()
    if extension not in IMAGE_EXTENSIONS or content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Portfolio images must be JPEG, PNG, or WebP")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    destination = UPLOADS_DIR / f"{uuid.uuid4().hex}{extension}"
    total = 0
    prefix = b""
    try:
        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_PORTFOLIO_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="Portfolio image exceeds the 8 MB limit")
                prefix += chunk[: max(0, 16 - len(prefix))]
                output.write(chunk)
        valid = prefix.startswith(b"\xff\xd8\xff") or prefix.startswith(b"\x89PNG\r\n\x1a\n") or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
        if total == 0 or not valid:
            raise HTTPException(status_code=400, detail="Portfolio image content is invalid")
        return f"/uploads/gig-portfolios/{destination.name}"
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


@router.post("")
async def create_gig(
    title: str = Form(...), description: str = Form(""), category: str = Form("General"), price: int = Form(...),
    skills: str = Form("[]"), delivery_days: int = Form(7), revisions: int = Form(1), demo_url: str = Form(""),
    availability: str = Form("available"), portfolio_images: list[UploadFile] = File(default=[]),
    current_user=Depends(get_current_user),
):
    try:
        parsed_skills = json.loads(skills or "[]")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Skills must be a JSON array") from exc
    if not isinstance(parsed_skills, list) or len(parsed_skills) > 20:
        raise HTTPException(status_code=400, detail="Provide up to 20 skills")
    data = CreateGigInput(title=title, description=description, category=category, price=price, skills=parsed_skills, delivery_days=delivery_days, revisions=revisions, demo_url=demo_url, availability=availability)
    if len(portfolio_images) > 5:
        raise HTTPException(status_code=400, detail="Upload no more than 5 portfolio images")
    saved_images: list[str] = []
    try:
        for upload in portfolio_images:
            if upload.filename:
                saved_images.append(await _save_portfolio_image(upload))
    except Exception:
        for image_url in saved_images:
            (UPLOADS_DIR / Path(image_url).name).unlink(missing_ok=True)
        raise
    db = get_db()
    seller = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"task_rating": 1, "task_rating_count": 1})
    gig_doc = {
        "title": data.title.strip(),
        "description": data.description.strip(),
        "category": data.category.strip() or "General",
        "price": data.price,
        "skills": list(dict.fromkeys(skill.strip()[:60] for skill in data.skills if skill.strip()))[:20],
        "delivery_days": data.delivery_days,
        "revisions": data.revisions,
        "demo_url": data.demo_url.strip(),
        "availability": data.availability,
        "portfolio_images": saved_images,
        "user_id": current_user["id"],
        "seller_name": current_user.get("name", "Student"),
        "seller_rating": round(float((seller or {}).get("task_rating", 0) or 0), 1),
        "seller_rating_count": int((seller or {}).get("task_rating_count", 0) or 0),
        "is_featured": False,
        "approval_status": "pending",
        "created_at": datetime.now(timezone.utc),
    }
    try:
        result = await db.gigs.insert_one(gig_doc)
    except Exception:
        for image_url in saved_images:
            (UPLOADS_DIR / Path(image_url).name).unlink(missing_ok=True)
        raise
    gig_doc["_id"] = result.inserted_id
    return {"message": "Gig submitted for review", "gig": serialize_gig(gig_doc)}


@router.get("/mine")
async def get_my_gigs(current_user=Depends(get_current_user)):
    db = get_db()
    gigs = [
        serialize_gig(gig)
        async for gig in db.gigs.find({"user_id": current_user["id"]}).sort("created_at", -1)
    ]
    return {"gigs": gigs}


@router.get("/orders")
async def get_my_gig_orders(current_user=Depends(get_current_user)):
    db = get_db()
    orders = []
    async for task in db.tasks.find({"created_by": current_user["id"], "gig_hire": True}).sort("created_at", -1).limit(100):
        orders.append({
            "id": str(task.get("_id", "")),
            "title": task.get("title", "Service order"),
            "seller_name": task.get("assigned_to_name", "Student"),
            "price": int(task.get("price", 0) or 0),
            "status": task.get("status", "assigned"),
            "created_at": task.get("created_at").isoformat() if isinstance(task.get("created_at"), datetime) else task.get("created_at"),
        })
    return {"orders": orders}


@router.get("")
async def get_gigs(
    page: int = 1,
    limit: int = 10,
    current_user=Depends(get_current_user),
):
    db = get_db()
    page = max(page, 1)
    limit = max(1, min(limit, 10))
    query = {
        "user_id": {"$ne": current_user["id"]},
        "availability": {"$ne": "unavailable"},
        "$or": [
            {"approval_status": "approved"},
            {"approval_status": {"$exists": False}},
        ],
    }
    projection = {
        "title": 1,
        "description": 1,
        "category": 1,
        "price": 1,
        "user_id": 1,
        "seller_name": 1,
        "seller_rating": 1,
        "seller_rating_count": 1,
        "created_at": 1,
        "skills": 1,
        "delivery_days": 1,
        "revisions": 1,
        "demo_url": 1,
        "availability": 1,
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


@router.get("/{gig_id}")
async def get_gig(gig_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    gig = await db.gigs.find_one({"_id": parse_object_id(gig_id, "Gig not found")})
    if not gig or gig.get("approval_status", "approved") != "approved":
        if not gig or gig.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=404, detail="Gig not found")
    seller = await db.users.find_one({"_id": parse_object_id(gig.get("user_id", ""), "Seller not found")}, {"avatar_url": 1, "is_verified": 1})
    result = serialize_gig(gig)
    result["seller"] = {
        "id": gig.get("user_id", ""),
        "name": gig.get("seller_name", "Student"),
        "avatar_url": (seller or {}).get("avatar_url", ""),
        "verified": bool((seller or {}).get("is_verified", False)),
        "rating": float(gig.get("seller_rating", 0) or 0),
        "rating_count": int(gig.get("seller_rating_count", 0) or 0),
    }
    return {"gig": result}


@router.post("/{gig_id}/hire")
async def hire_gig(gig_id: str, data: HireGigInput, current_user=Depends(get_current_user)):
    db = get_db()
    object_id = parse_object_id(gig_id, "Gig not found")
    gig = await db.gigs.find_one({"_id": object_id})
    if not gig:
        raise HTTPException(status_code=404, detail="Gig not found")
    if gig.get("approval_status", "approved") != "approved":
        raise HTTPException(status_code=404, detail="Gig not found")
    if gig.get("availability") == "unavailable":
        raise HTTPException(status_code=409, detail="This seller is currently unavailable")
    if gig.get("user_id") == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot hire your own gig")

    price = int(gig.get("price", 0) or 0)
    if price <= 0:
        raise HTTPException(status_code=409, detail="Gig price must be a positive amount for escrow protection")
    buyer_wallet = await ensure_wallet(db, current_user["id"])
    if int(buyer_wallet.get("available_balance", 0) or 0) < price:
        raise HTTPException(status_code=400, detail="Not enough wallet balance")

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
        "escrow_status": "held",
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
        "chat_context": {
            "purpose": "buy",
            "category": "service",
            "listing_id": gig_id,
            "title": gig["title"],
            "price": price,
        },
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
