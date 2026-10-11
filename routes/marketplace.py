import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from routes.gigs import serialize_gig
from routes.marketplace_listings import _serialize_listing
from utils.auth_utils import get_current_user
from utils.db import get_db

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])
PAGE_LIMIT = 50
MAX_PAGE = 100


def serialize_listing(kind: str, item: dict) -> dict:
    if kind in {"project", "ppt"}:
        listing = _serialize_listing(item)
        listing["thumbnail_url"] = (item.get("preview_images") or [""])[0]
        return listing
    if kind == "note":
        listing = {
            "id": str(item.get("_id", item.get("id", ""))),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "subject": item.get("subject", ""),
            "topic": item.get("topic", ""),
            "tags": item.get("tags", []),
            "price": int(item.get("unlock_cost", item.get("price", 0)) or 0),
            "thumbnail_url": item.get("thumbnail_url", ""),
            "seller_name": item.get("uploader_name", ""),
            "created_at": item.get("created_at"),
        }
    elif kind == "service":
        listing = serialize_gig(item)
    else:
        listing = {
            "id": str(item.get("_id", item.get("id", ""))),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "price": int(item.get("price", 0) or 0),
            "budget": int(item.get("price", 0) or 0),
            "deadline": item.get("deadline", ""),
            "location": item.get("location", ""),
            "required_skills": item.get("required_skills", []),
            "is_urgent": bool(item.get("is_urgent", False)),
            "status": item.get("status", "open"),
            "created_by_name": item.get("created_by_name", ""),
            "created_at": item.get("created_at"),
        }
    listing["kind"] = kind
    listing["price"] = int(listing.get("price", listing.get("budget", 0)) or 0)
    listing["seller_name"] = (
        listing.get("seller_name")
        or listing.get("created_by_name")
        or listing.get("sellerName")
        or ""
    )
    created_at = listing.get("created_at", listing.get("createdAt"))
    if isinstance(created_at, datetime):
        listing["created_at"] = created_at.isoformat()
    return listing


@router.get("")
async def get_marketplace(
    category: str = "all",
    q: str = "",
    page: int = Query(1, ge=1, le=MAX_PAGE),
    limit: int = Query(20, ge=1, le=PAGE_LIMIT),
    current_user=Depends(get_current_user),
):
    db = get_db()
    normalized_category = category.strip().lower()
    specs = {
        "notes": (
            "note",
            db.notes,
            {"status": "approved"},
            [("created_at", -1)],
        ),
        "services": (
            "service",
            db.gigs,
            {
                "user_id": {"$ne": current_user["id"]},
                "$or": [
                    {"approval_status": "approved"},
                    {"approval_status": {"$exists": False}},
                ],
            },
            [("is_featured", -1), ("created_at", -1)],
        ),
        "tasks": (
            "task",
            db.tasks,
            {"status": "open", "created_by": {"$ne": current_user["id"]}},
            [("is_urgent", -1), ("is_boosted", -1), ("popularity_score", -1), ("created_at", -1)],
        ),
        "projects": (
            "project",
            db.marketplace_listings,
            {"kind": "project", "status": "approved"},
            [("created_at", -1)],
        ),
        "ppts": (
            "ppt",
            db.marketplace_listings,
            {"kind": "ppt", "status": "approved"},
            [("created_at", -1)],
        ),
    }
    selected = list(specs) if normalized_category == "all" else [normalized_category]
    if any(item not in specs for item in selected):
        raise HTTPException(status_code=400, detail="Unsupported marketplace category")

    search = q.strip()
    skip = (page - 1) * limit
    look_ahead = page * limit
    groups = []
    total = 0
    for key in selected:
        if key not in specs:
            continue
        kind, collection, base_query, sort = specs[key]
        query: dict = dict(base_query)
        if search:
            escaped = re.escape(search)
            fields = ["title", "description"]
            if kind == "note":
                fields += ["subject", "topic"]
            if kind in {"project", "ppt"}:
                fields += ["category", "subcategory", "tech_stack"]
            query = {"$and": [base_query, {"$or": [{field: {"$regex": escaped, "$options": "i"}} for field in fields]}]}
        cursor = collection.find(query).sort(sort).limit(look_ahead)
        async for item in cursor:
            groups.append(serialize_listing(kind, item))
        total += await collection.count_documents(query)

    groups.sort(key=lambda item: str(item.get("created_at", item.get("createdAt", ""))), reverse=True)
    items = groups[skip : skip + limit]
    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": min(MAX_PAGE, (total + limit - 1) // limit),
        "has_more": skip + len(items) < total and page < MAX_PAGE,
    }
