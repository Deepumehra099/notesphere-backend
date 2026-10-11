import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import hold_wallet_funds, release_held_funds

router = APIRouter(prefix="/api/marketplace", tags=["marketplace-listings"])
admin_router = APIRouter(prefix="/api/admin/marketplace", tags=["admin-marketplace"])
UPLOADS_DIR = Path(__file__).resolve().parents[1] / "uploads"
PRIVATE_DIR = Path(__file__).resolve().parents[1] / "private-marketplace"
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_LISTING_FILE_BYTES = 100 * 1024 * 1024
ALLOWED_IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
FILE_EXTENSIONS = {
    "project": {".zip"},
    "ppt": {".ppt", ".pptx", ".pdf"},
}
FILE_MIMES = {
    "project": {"application/zip", "application/x-zip-compressed", "application/octet-stream"},
    "ppt": {"application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/pdf", "application/octet-stream"},
}


def _object_id(value: str):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Listing not found")


def _clean_list(value: str, field: str, max_count: int = 30) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a JSON array") from exc
    if not isinstance(parsed, list) or len(parsed) > max_count:
        raise HTTPException(status_code=400, detail=f"{field} is invalid")
    items = [str(item).strip()[:200] for item in parsed if str(item).strip()]
    return items


async def _store_upload(upload: UploadFile, folder: Path, *, limit: int, extensions: set[str], mimes: set[str], image: bool = False) -> tuple[str, int]:
    extension = Path(upload.filename or "").suffix.lower()
    content_type = (upload.content_type or "application/octet-stream").lower()
    if extension not in extensions or content_type not in mimes:
        raise HTTPException(status_code=400, detail="Unsupported upload type")
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / f"{uuid.uuid4().hex}{extension}"
    total = 0
    prefix = b""
    try:
        with file_path.open("wb") as target:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail="Uploaded file exceeds the size limit")
                if len(prefix) < 16:
                    prefix += chunk[: 16 - len(prefix)]
                target.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        if image and not (
            prefix.startswith(b"\xff\xd8\xff")
            or prefix.startswith(b"\x89PNG\r\n\x1a\n")
            or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
        ):
            raise HTTPException(status_code=400, detail="Preview must be a valid JPEG, PNG, or WebP image")
        if extension == ".zip" and not prefix.startswith(b"PK\x03\x04"):
            raise HTTPException(status_code=400, detail="Project upload must be a valid ZIP archive")
        if extension == ".pdf" and not prefix.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="PPT PDF must be a valid PDF document")
        return str(file_path), total
    except Exception:
        file_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def _serialize_listing(item: dict, *, include_private: bool = False) -> dict:
    created = item.get("created_at")
    updated = item.get("updated_at")
    result = {
        "id": str(item.get("_id", "")),
        "kind": item.get("kind", "project"),
        "title": item.get("title", ""),
        "category": item.get("category", ""),
        "subcategory": item.get("subcategory", ""),
        "description": item.get("description", ""),
        "short_description": item.get("short_description", ""),
        "features": item.get("features", []),
        "requirements": item.get("requirements", []),
        "included_items": item.get("included_items", []),
        "tech_stack": item.get("tech_stack", []),
        "price": int(item.get("price", 0) or 0),
        "currency": "INR",
        "preview_images": item.get("preview_images", []),
        "demo_url": item.get("demo_url", ""),
        "github_url": item.get("github_url", ""),
        "slide_count": item.get("slide_count", 0),
        "status": item.get("status", "pending"),
        "review_reason": item.get("review_reason", ""),
        "seller_id": item.get("seller_id", ""),
        "seller_name": item.get("seller_name", "Student"),
        "seller_avatar": item.get("seller_avatar", ""),
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
        "updated_at": updated.isoformat() if isinstance(updated, datetime) else updated,
        "approved_at": item.get("approved_at").isoformat() if isinstance(item.get("approved_at"), datetime) else item.get("approved_at"),
        "has_file": bool(item.get("file_path")),
        "file_extension": Path(item.get("original_file_name", "")).suffix.lower().lstrip("."),
    }
    if include_private:
        result["file_path"] = item.get("file_path", "")
        result["original_file_name"] = item.get("original_file_name", "")
    return result


def _parse_marketplace_form(kind: str, title: str, category: str, subcategory: str, short_description: str, description: str, tech_stack: str, features: str, requirements: str, included_items: str, price: int, demo_url: str, github_url: str, slide_count: int):
    title = title.strip()
    category = category.strip()
    description = description.strip()
    if len(title) < 3 or len(title) > 160 or not category or len(category) > 80:
        raise HTTPException(status_code=400, detail="A valid title and category are required")
    if not description or len(description) > 12000 or len(short_description) > 500:
        raise HTTPException(status_code=400, detail="A valid description is required")
    if price < 1 or price > 10_000_000:
        raise HTTPException(status_code=400, detail="Price must be a positive INR amount")
    return {
        "kind": kind,
        "title": title,
        "category": category,
        "subcategory": subcategory.strip()[:80],
        "short_description": short_description.strip()[:500] or description[:220],
        "description": description,
        "tech_stack": _clean_list(tech_stack, "Technology / topic"),
        "features": _clean_list(features, "Features"),
        "requirements": _clean_list(requirements, "Requirements"),
        "included_items": _clean_list(included_items, "Included items"),
        "price": int(price),
        "demo_url": demo_url.strip()[:500],
        "github_url": github_url.strip()[:500],
        "slide_count": int(slide_count),
        "status": "pending",
        "review_reason": "",
        "views": 0,
        "sales_count": 0,
        "favorites": 0,
    }


async def _create_listing(kind: str, *, title: str, category: str, subcategory: str, short_description: str, description: str, tech_stack: str, features: str, requirements: str, included_items: str, price: int, demo_url: str, github_url: str, slide_count: int, listing_file: UploadFile, preview_files: list[UploadFile], current_user: dict):
    if not listing_file.filename:
        raise HTTPException(status_code=400, detail="A source file is required")
    if not 1 <= len(preview_files) <= 5 or any(not image.filename for image in preview_files):
        raise HTTPException(status_code=400, detail="Upload between 1 and 5 preview images")
    doc = _parse_marketplace_form(kind, title, category, subcategory, short_description, description, tech_stack, features, requirements, included_items, price, demo_url, github_url, slide_count)
    if kind == "ppt" and not 1 <= slide_count <= 1000:
        raise HTTPException(status_code=400, detail="PPT slide count must be between 1 and 1000")

    saved_paths: list[Path] = []
    file_path = ""
    preview_images: list[str] = []
    try:
        file_path, _ = await _store_upload(listing_file, PRIVATE_DIR / kind, limit=MAX_LISTING_FILE_BYTES, extensions=FILE_EXTENSIONS[kind], mimes=FILE_MIMES[kind])
        saved_paths.append(Path(file_path))
        preview_dir = UPLOADS_DIR / "marketplace-previews"
        for image in preview_files:
            path, _ = await _store_upload(image, preview_dir, limit=MAX_PREVIEW_BYTES, extensions=ALLOWED_IMAGES, mimes={"image/jpeg", "image/png", "image/webp"}, image=True)
            saved_paths.append(Path(path))
            preview_images.append(f"/uploads/marketplace-previews/{Path(path).name}")

        user = await get_db().users.find_one({"_id": ObjectId(current_user["id"])}, {"avatar_url": 1, "avatar": 1})
        now = datetime.now(timezone.utc)
        doc.update({
            "seller_id": current_user["id"],
            "seller_name": current_user.get("name", "Student"),
            "seller_avatar": (user or {}).get("avatar_url") or (user or {}).get("avatar", ""),
            "original_file_name": Path(listing_file.filename).name[:255],
            "file_path": file_path,
            "preview_images": preview_images,
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
        })
        insert = await get_db().marketplace_listings.insert_one(doc)
        doc["_id"] = insert.inserted_id
        return {"message": "Listing submitted for review", "listing": _serialize_listing(doc)}
    except Exception:
        for path in saved_paths:
            path.unlink(missing_ok=True)
        raise


@router.post("/projects")
async def create_project(
    title: str = Form(...), category: str = Form(...), subcategory: str = Form(""), short_description: str = Form(""), description: str = Form(...), tech_stack: str = Form("[]"), features: str = Form("[]"), requirements: str = Form("[]"), included_items: str = Form("[]"), price: int = Form(...), demo_url: str = Form(""), github_url: str = Form(""), listing_file: UploadFile = File(...), preview_files: list[UploadFile] = File(...), current_user=Depends(get_current_user),
):
    return await _create_listing("project", title=title, category=category, subcategory=subcategory, short_description=short_description, description=description, tech_stack=tech_stack, features=features, requirements=requirements, included_items=included_items, price=price, demo_url=demo_url, github_url=github_url, slide_count=0, listing_file=listing_file, preview_files=preview_files, current_user=current_user)


@router.post("/ppts")
async def create_ppt(
    title: str = Form(...), category: str = Form(...), subcategory: str = Form(""), short_description: str = Form(""), description: str = Form(...), tech_stack: str = Form("[]"), price: int = Form(...), slide_count: int = Form(...), listing_file: UploadFile = File(...), preview_files: list[UploadFile] = File(...), current_user=Depends(get_current_user),
):
    return await _create_listing("ppt", title=title, category=category, subcategory=subcategory, short_description=short_description, description=description, tech_stack=tech_stack, features="[]", requirements="[]", included_items="[]", price=price, demo_url="", github_url="", slide_count=slide_count, listing_file=listing_file, preview_files=preview_files, current_user=current_user)


@router.get("/projects/mine")
@router.get("/ppts/mine")
async def get_my_listings(request: Request, kind: str = Query("project", pattern="^(project|ppt)$"), current_user=Depends(get_current_user)):
    if "/ppts/" in request.url.path:
        kind = "ppt"
    db = get_db()
    items = [_serialize_listing(item) async for item in db.marketplace_listings.find({"seller_id": current_user["id"], "kind": kind}).sort("created_at", -1).limit(100)]
    return {"listings": items}


@router.get("/projects/{listing_id}")
@router.get("/ppts/{listing_id}")
async def get_listing(listing_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    listing = await db.marketplace_listings.find_one({"_id": _object_id(listing_id)})
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    is_owner = listing.get("seller_id") == current_user["id"]
    is_admin = bool(current_user.get("is_admin") or current_user.get("role") == "admin")
    has_purchase = bool(await db.marketplace_purchases.find_one({"listing_id": listing_id, "buyer_id": current_user["id"], "status": "paid"}))
    if listing.get("status") != "approved" and not (is_owner or is_admin or has_purchase):
        raise HTTPException(status_code=404, detail="Listing not found")
    await db.marketplace_listings.update_one({"_id": listing["_id"]}, {"$inc": {"views": 1}})
    seller = await db.users.find_one({"_id": ObjectId(listing["seller_id"])}, {"name": 1, "avatar_url": 1, "uid": 1, "task_rating": 1, "task_rating_count": 1, "is_verified": 1})
    result = _serialize_listing(listing)
    result["seller"] = {
        "id": listing["seller_id"],
        "name": (seller or {}).get("name", listing.get("seller_name", "Student")),
        "avatar_url": (seller or {}).get("avatar_url", listing.get("seller_avatar", "")),
        "uid": (seller or {}).get("uid", ""),
        "rating": float((seller or {}).get("task_rating", 0) or 0),
        "rating_count": int((seller or {}).get("task_rating_count", 0) or 0),
        "verified": bool((seller or {}).get("is_verified", False)),
    }
    result["views"] = int(listing.get("views", 0) or 0) + 1
    result["sales_count"] = int(listing.get("sales_count", 0) or 0)
    result["is_purchased"] = listing.get("seller_id") == current_user["id"] or bool(await db.marketplace_purchases.find_one({"listing_id": listing_id, "buyer_id": current_user["id"], "status": "paid"}))
    return {"listing": result}


@router.post("/projects/{listing_id}/chat")
@router.post("/ppts/{listing_id}/chat")
async def start_listing_chat(listing_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    listing = await db.marketplace_listings.find_one({"_id": _object_id(listing_id), "status": "approved"})
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.get("seller_id") == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot contact yourself about your listing")
    thread = await db.marketplace_chat_threads.find_one({"listing_id": listing_id, "buyer_id": current_user["id"]})
    if not thread:
        task_id = ObjectId()
        thread_doc = {"listing_id": listing_id, "buyer_id": current_user["id"], "seller_id": listing["seller_id"], "task_id": str(task_id), "created_at": datetime.now(timezone.utc)}
        try:
            await db.marketplace_chat_threads.insert_one(thread_doc)
            thread = thread_doc
        except Exception:
            thread = await db.marketplace_chat_threads.find_one({"listing_id": listing_id, "buyer_id": current_user["id"]})
            if not thread:
                raise HTTPException(status_code=503, detail="Could not start a seller conversation")
    task_id = ObjectId(thread["task_id"])
    now = datetime.now(timezone.utc)
    buyer = await db.users.find_one({"_id": ObjectId(current_user["id"])}, {"name": 1, "email": 1, "verified": 1})
    await db.tasks.update_one(
        {"_id": task_id},
        {"$setOnInsert": {
            "title": listing["title"], "description": f"Marketplace inquiry: {listing.get('short_description', '')}",
            "price": int(listing["price"]), "deadline": "Flexible", "status": "assigned",
            "created_by": current_user["id"], "created_by_name": current_user.get("name", "Student"),
            "created_by_email": (buyer or {}).get("email", ""), "created_by_verified": bool((buyer or {}).get("verified", False)),
            "assigned_to": listing["seller_id"], "assigned_to_name": listing.get("seller_name", "Student"),
            "created_at": now, "accepted_at": now, "completed_at": None,
            "escrow_status": "none", "escrow_amount": 0, "commission_amount": 0, "seller_payout": 0,
            "is_urgent": False, "is_boosted": False, "views": 0, "clicks": 0, "accepts": 0,
            "marketplace_listing_id": listing_id,
            "chat_context": {"purpose": "buy", "category": listing["kind"], "listing_id": listing_id, "title": listing["title"], "price": int(listing["price"])},
        }},
        upsert=True,
    )
    await db.chats.update_one(
        {"task_id": str(task_id)},
        {"$setOnInsert": {"task_id": str(task_id), "participants": [current_user["id"], listing["seller_id"]], "last_message": "", "last_message_at": now, "created_at": now}},
        upsert=True,
    )
    return {"task_id": str(task_id), "listing_id": listing_id, "context": {"purpose": "buy", "category": listing["kind"], "title": listing["title"], "price": int(listing["price"])}}


@router.get("/projects/{listing_id}/download")
@router.get("/ppts/{listing_id}/download")
async def download_listing(listing_id: str, current_user=Depends(get_current_user)):
    listing = await get_db().marketplace_listings.find_one({"_id": _object_id(listing_id)})
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    is_seller = listing.get("seller_id") == current_user["id"]
    is_admin = bool(current_user.get("is_admin") or current_user.get("role") == "admin")
    purchase = await get_db().marketplace_purchases.find_one({"listing_id": listing_id, "buyer_id": current_user["id"], "status": "paid"})
    if listing.get("status") != "approved" and not (is_seller or is_admin or purchase):
        raise HTTPException(status_code=404, detail="Listing not found")
    if not (is_seller or is_admin or purchase):
        raise HTTPException(status_code=403, detail="Purchase this listing before downloading its file")
    file_path = Path(listing.get("file_path", ""))
    if not file_path.is_file() or PRIVATE_DIR not in file_path.resolve().parents:
        raise HTTPException(status_code=404, detail="Listing file is unavailable")
    return FileResponse(file_path, filename=listing.get("original_file_name", file_path.name), media_type="application/octet-stream")


async def _finish_purchase(purchase: dict, listing: dict):
    db = get_db()
    purchase_id = str(purchase["_id"])
    try:
        result = await release_held_funds(
            db,
            buyer_user_id=purchase["buyer_id"],
            seller_user_id=listing["seller_id"],
            amount=int(purchase["amount"]),
            reason=f"Marketplace sale: {listing['title']}",
            source_type="marketplace_purchase",
            source_id=purchase_id,
            metadata={"listing_id": str(listing["_id"]), "listing_kind": listing["kind"], "listing_title": listing["title"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Purchase funds are held and settlement is being reconciled") from exc
    now = datetime.now(timezone.utc)
    changed = await db.marketplace_purchases.update_one(
        {"_id": purchase["_id"], "status": {"$ne": "paid"}},
        {"$set": {"status": "paid", "paid_at": now, "commission_amount": result["commission_amount"], "seller_payout": result["seller_payout"]}},
    )
    if changed.modified_count:
        await db.marketplace_listings.update_one({"_id": listing["_id"]}, {"$inc": {"sales_count": 1}})
    purchase["status"] = "paid"
    purchase["paid_at"] = now
    purchase.update(result)
    return purchase


@router.post("/projects/{listing_id}/purchase")
@router.post("/ppts/{listing_id}/purchase")
async def purchase_listing(listing_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    listing = await db.marketplace_listings.find_one({"_id": _object_id(listing_id), "status": "approved"})
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.get("seller_id") == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot purchase your own listing")
    existing = await db.marketplace_purchases.find_one({"listing_id": listing_id, "buyer_id": current_user["id"]})
    if existing:
        if existing.get("status") == "paid":
            return {"purchase": _serialize_purchase(existing), "already_purchased": True}
        held = await db.transactions.find_one({"source_type": "marketplace_purchase", "source_id": str(existing["_id"]), "status": "held"})
        if held:
            existing = await _finish_purchase(existing, listing)
            return {"purchase": _serialize_purchase(existing), "already_purchased": True}
        raise HTTPException(status_code=409, detail="A purchase attempt is already being processed")

    now = datetime.now(timezone.utc)
    purchase_doc = {
        "listing_id": listing_id,
        "listing_kind": listing["kind"],
        "listing_title": listing["title"],
        "seller_name": listing.get("seller_name", "Student"),
        "preview_image": (listing.get("preview_images") or [""])[0],
        "buyer_id": current_user["id"],
        "seller_id": listing["seller_id"],
        "amount": int(listing["price"]),
        "currency": "INR",
        "status": "processing",
        "created_at": now,
    }
    try:
        inserted = await db.marketplace_purchases.insert_one(purchase_doc)
    except Exception as exc:
        existing = await db.marketplace_purchases.find_one({"listing_id": listing_id, "buyer_id": current_user["id"]})
        if existing and existing.get("status") == "paid":
            return {"purchase": _serialize_purchase(existing), "already_purchased": True}
        raise HTTPException(status_code=409, detail="Purchase is already being processed") from exc
    purchase_doc["_id"] = inserted.inserted_id
    held = await hold_wallet_funds(
        db,
        user_id=current_user["id"],
        amount=int(listing["price"]),
        reason=f"Purchase: {listing['title']}",
        source_type="marketplace_purchase",
        source_id=str(inserted.inserted_id),
        counterparty_user_id=listing["seller_id"],
        metadata={"listing_id": listing_id, "listing_kind": listing["kind"]},
    )
    if not held:
        await db.marketplace_purchases.delete_one({"_id": inserted.inserted_id, "status": "processing"})
        raise HTTPException(status_code=400, detail="Not enough available INR wallet balance")
    await db.marketplace_purchases.update_one({"_id": inserted.inserted_id}, {"$set": {"status": "funds_held"}})
    purchase_doc["status"] = "funds_held"
    purchase_doc = await _finish_purchase(purchase_doc, listing)
    return {"purchase": _serialize_purchase(purchase_doc), "already_purchased": False}


def _serialize_purchase(purchase: dict) -> dict:
    return {
        "id": str(purchase.get("_id", "")),
        "listing_id": purchase.get("listing_id", ""),
        "listing_kind": purchase.get("listing_kind", ""),
        "listing_title": purchase.get("listing_title", ""),
        "seller_name": purchase.get("seller_name", "Student"),
        "preview_image": purchase.get("preview_image", ""),
        "amount": int(purchase.get("amount", 0) or 0),
        "currency": purchase.get("currency", "INR"),
        "status": purchase.get("status", "processing"),
        "created_at": purchase.get("created_at").isoformat() if isinstance(purchase.get("created_at"), datetime) else purchase.get("created_at"),
        "paid_at": purchase.get("paid_at").isoformat() if isinstance(purchase.get("paid_at"), datetime) else purchase.get("paid_at"),
        "commission_amount": int(purchase.get("commission_amount", 0) or 0),
        "seller_payout": int(purchase.get("seller_payout", 0) or 0),
    }


@router.get("/purchases")
async def get_my_purchases(current_user=Depends(get_current_user)):
    purchases = [_serialize_purchase(item) async for item in get_db().marketplace_purchases.find({"buyer_id": current_user["id"]}).sort("created_at", -1).limit(100)]
    return {"purchases": purchases}


@router.get("/seller/listings")
async def seller_dashboard(current_user=Depends(get_current_user)):
    db = get_db()
    listings = [_serialize_listing(item) async for item in db.marketplace_listings.find({"seller_id": current_user["id"]}).sort("created_at", -1).limit(100)]
    sales = [_serialize_purchase(item) async for item in db.marketplace_purchases.find({"seller_id": current_user["id"], "status": "paid"}).sort("paid_at", -1).limit(100)]
    return {"listings": listings, "sales": sales}


@admin_router.get("/listings")
async def admin_listings(status: str = Query("pending", pattern="^(pending|approved|rejected|suspended|all)$"), current_user=Depends(get_current_user)):
    if not current_user.get("is_admin") and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    query = {} if status == "all" else {"status": status}
    items = [_serialize_listing(item) async for item in get_db().marketplace_listings.find(query).sort("created_at", -1).limit(200)]
    return {"listings": items}


@admin_router.patch("/listings/{listing_id}")
async def admin_update_listing(listing_id: str, status: str = Form(...), reason: str = Form(""), current_user=Depends(get_current_user)):
    if not current_user.get("is_admin") and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if status not in {"approved", "rejected", "suspended", "pending"}:
        raise HTTPException(status_code=400, detail="Unsupported listing status")
    now = datetime.now(timezone.utc)
    update = {"status": status, "review_reason": reason.strip()[:1000], "updated_at": now}
    if status == "approved":
        update["approved_at"] = now
    result = await get_db().marketplace_listings.find_one_and_update({"_id": _object_id(listing_id)}, {"$set": update}, return_document=ReturnDocument.AFTER)
    if not result:
        raise HTTPException(status_code=404, detail="Listing not found")
    return {"listing": _serialize_listing(result)}


@admin_router.get("/purchases")
async def admin_purchases(current_user=Depends(get_current_user)):
    if not current_user.get("is_admin") and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    purchases = [_serialize_purchase(item) async for item in get_db().marketplace_purchases.find({}).sort("created_at", -1).limit(500)]
    return {"purchases": purchases}


@admin_router.delete("/listings/{listing_id}")
async def admin_delete_listing(listing_id: str, current_user=Depends(get_current_user)):
    if not current_user.get("is_admin") and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    listing_object_id = _object_id(listing_id)
    existing = await get_db().marketplace_listings.find_one({"_id": listing_object_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if await get_db().marketplace_purchases.count_documents({"listing_id": listing_id, "status": "paid"}):
        raise HTTPException(status_code=409, detail="A sold listing cannot be deleted because buyers still need their files")
    result = await get_db().marketplace_listings.find_one_and_delete({"_id": listing_object_id})
    if not result:
        raise HTTPException(status_code=404, detail="Listing not found")
    Path(result.get("file_path", "")).unlink(missing_ok=True)
    for image_url in result.get("preview_images", []):
        (UPLOADS_DIR / "marketplace-previews" / Path(image_url).name).unlink(missing_ok=True)
    return {"message": "Listing deleted"}


@router.delete("/projects/{listing_id}")
@router.delete("/ppts/{listing_id}")
async def delete_own_listing(listing_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    result = await db.marketplace_listings.find_one_and_delete({"_id": _object_id(listing_id), "seller_id": current_user["id"], "status": {"$in": ["pending", "rejected"]}})
    if not result:
        raise HTTPException(status_code=404, detail="Editable listing not found")
    Path(result.get("file_path", "")).unlink(missing_ok=True)
    for image_url in result.get("preview_images", []):
        (UPLOADS_DIR / "marketplace-previews" / Path(image_url).name).unlink(missing_ok=True)
    return {"message": "Listing deleted"}

