import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cloudinary
import cloudinary.uploader
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from utils.auth_utils import get_current_user
from utils.db import get_db
from utils.wallets import ensure_wallet, hold_wallet_funds, release_held_funds

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

router = APIRouter(prefix="/api/notes", tags=["notes"])
logger = logging.getLogger(__name__)

def configure_cloudinary() -> None:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()

    if api_key.startswith("cloudinary://"):
        logger.error("Invalid Cloudinary configuration: CLOUDINARY_API_KEY contains a cloudinary:// URL")
        raise HTTPException(
            status_code=500,
            detail=(
                "Invalid Cloudinary configuration. Use plain CLOUDINARY_CLOUD_NAME, "
                "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET values."
            ),
        )

    if not cloud_name or not api_key or not api_secret:
        logger.error(
            "Missing Cloudinary configuration. cloud_name=%s api_key_present=%s api_secret_present=%s",
            bool(cloud_name),
            bool(api_key),
            bool(api_secret),
        )
        raise HTTPException(status_code=500, detail="Cloudinary is not configured")

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )

def serialize_note(note):
    n = {**note}
    n["id"] = str(n.pop("_id"))
    price = n.get("unlock_cost", 0) or 0
    n["price"] = price
    n["access_type"] = "paid" if price > 0 else "free"
    if "unlocked_by" in n:
        n["unlocked_by"] = [str(uid) for uid in n.get("unlocked_by", [])]
    return n

@router.get("/feed")
async def get_notes_feed(
    page: int = 1,
    limit: int = 20,
    subject: Optional[str] = None,
    sort: str = "newest",
    current_user=Depends(get_current_user)
):
    db = get_db()
    query = {"status": "approved"}
    if subject:
        query["subject"] = {"$regex": subject, "$options": "i"}
    if sort == "paid":
        query["unlock_cost"] = {"$gt": 0}
    sort_field = {"newest": ("created_at", -1), "popular": ("views", -1), "rating": ("rating", -1), "paid": ("created_at", -1)}
    s = sort_field.get(sort, ("created_at", -1))
    skip = (page - 1) * limit
    cursor = db.notes.find(query).sort(s[0], s[1]).skip(skip).limit(limit)
    notes = []
    async for note in cursor:
        n = serialize_note(note)
        n["is_unlocked"] = (
            n.get("price", 0) <= 0
            or current_user["id"] in n.get("unlocked_by", [])
            or n.get("uploaded_by") == current_user["id"]
        )
        notes.append(n)
    total = await db.notes.count_documents(query)
    return {"notes": notes, "total": total, "page": page, "pages": (total + limit - 1) // limit}


@router.get("")
async def get_notes_feed_compat(
    access: str = "all",
    type: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    subject: Optional[str] = None,
    current_user=Depends(get_current_user),
):
    db = get_db()
    query = {"status": "approved"}
    if subject:
        query["subject"] = {"$regex": subject, "$options": "i"}

    normalized_access = (type or access or "all").strip().lower()
    if normalized_access == "free":
        query["unlock_cost"] = {"$lte": 0}
    elif normalized_access == "paid":
        query["unlock_cost"] = {"$gt": 0}

    skip = (max(page, 1) - 1) * limit
    cursor = db.notes.find(query).sort("created_at", -1).skip(skip).limit(limit)
    notes = []
    async for note in cursor:
        n = serialize_note(note)
        n["is_unlocked"] = (
            n.get("price", 0) <= 0
            or current_user["id"] in n.get("unlocked_by", [])
            or n.get("uploaded_by") == current_user["id"]
        )
        notes.append(n)

    total = await db.notes.count_documents(query)
    return {"notes": notes, "total": total, "page": max(page, 1), "pages": (total + limit - 1) // limit}

@router.get("/my")
@router.get("/mine")
async def get_my_notes(current_user=Depends(get_current_user)):
    db = get_db()
    cursor = db.notes.find({"uploaded_by": current_user["id"]}).sort("created_at", -1)
    notes = []
    async for note in cursor:
        notes.append(serialize_note(note))
    return {"notes": notes}

@router.get("/{note_id}")
async def get_note(note_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    note = await db.notes.find_one({"_id": ObjectId(note_id)})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    await db.notes.update_one({"_id": ObjectId(note_id)}, {"$inc": {"views": 1}})
    n = serialize_note(note)
    n["is_unlocked"] = (
        n.get("price", 0) <= 0
        or current_user["id"] in n.get("unlocked_by", [])
        or n.get("uploaded_by") == current_user["id"]
    )
    return {"note": n}

@router.post("/upload")
async def upload_note(
    title: str = Form(...),
    description: str = Form(""),
    subject: str = Form(...),
    topic: str = Form(""),
    tags: str = Form(""),
    unlock_cost: int = Form(0),
    price: Optional[int] = Form(None),
    file: Optional[UploadFile] = File(None),
    pdf: Optional[UploadFile] = File(None),
    thumbnail: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    configure_cloudinary()

    actual_file = pdf or file
    if actual_file is None or not actual_file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if not actual_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    final_price = max(0, int(price if price is not None else unlock_cost or 0))
    thumbnail_url = ""

    public_id = f"notesphere/notes/{current_user['id']}_{int(datetime.now().timestamp())}"

    try:
        actual_file.file.seek(0)
        result = cloudinary.uploader.upload(
            actual_file.file,
            resource_type="auto",
            public_id=public_id,
            overwrite=True,
        )
        secure_url = result.get("secure_url")
        if not secure_url:
            raise HTTPException(status_code=500, detail="Cloudinary did not return a secure URL")
        logger.info(
            "Cloudinary upload success. user_id=%s filename=%s file_url=%s",
            current_user["id"],
            actual_file.filename,
            secure_url,
        )

        if thumbnail is not None and thumbnail.filename:
            thumbnail.file.seek(0)
            thumbnail_result = cloudinary.uploader.upload(
                thumbnail.file,
                resource_type="image",
                folder="notesphere/note-thumbnails",
            )
            thumbnail_url = thumbnail_result.get("secure_url", "").strip()
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.exception("Cloudinary upload failed for filename=%s: %s", actual_file.filename, e)
        raise HTTPException(status_code=500, detail="File upload failed")
    finally:
        await actual_file.close()
        if thumbnail is not None:
            await thumbnail.close()

    db = get_db()
    note_doc = {
        "title": title.strip(),
        "description": description.strip(),
        "subject": subject.strip(),
        "topic": topic.strip(),
        "unlock_cost": final_price,
        "price": final_price,
        "status": "pending",
        "uploaded_by": current_user["id"],
        "uploader_name": current_user.get("name", "Student"),
        "file_url": secure_url,
        "file_name": actual_file.filename,
        "thumbnail_url": thumbnail_url,
        "tags": [item.strip() for item in tags.split(",") if item.strip()],
        "views": 0,
        "downloads": 0,
        "rating": 0,
        "rating_count": 0,
        "unlocked_by": [],
        "created_at": datetime.now(timezone.utc),
        "approved_at": None,
        "rejected_at": None,
    }
    result = await db.notes.insert_one(note_doc)
    note_doc["_id"] = result.inserted_id

    return {
        "message": "Upload successful",
        "file_url": secure_url,
        "note": serialize_note(note_doc),
    }


@router.post("/upload-note")
async def upload_note_alias(
    title: str = Form(...),
    description: str = Form(""),
    subject: str = Form(...),
    topic: str = Form(""),
    tags: str = Form(""),
    unlock_cost: int = Form(0),
    price: Optional[int] = Form(None),
    file: Optional[UploadFile] = File(None),
    pdf: Optional[UploadFile] = File(None),
    thumbnail: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    return await upload_note(
        title=title,
        description=description,
        subject=subject,
        topic=topic,
        tags=tags,
        unlock_cost=unlock_cost,
        price=price,
        file=file,
        pdf=pdf,
        thumbnail=thumbnail,
        current_user=current_user,
    )

@router.post("/{note_id}/unlock")
async def unlock_note(note_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    note = await db.notes.find_one({"_id": ObjectId(note_id)})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if current_user["id"] in [str(uid) for uid in note.get("unlocked_by", [])]:
        return {"message": "Already unlocked"}
    if note.get("uploaded_by") == current_user["id"]:
        return {"message": "You own this note"}
    cost = note.get("unlock_cost", 10)
    if cost <= 0:
        return {"message": "Free note already accessible"}
    buyer_wallet = await ensure_wallet(db, current_user["id"])
    if int(buyer_wallet.get("available_balance", 0) or 0) < cost:
        raise HTTPException(status_code=400, detail="Not enough wallet balance")
    held_wallet = await hold_wallet_funds(
        db,
        user_id=current_user["id"],
        amount=cost,
        reason=f"Funds held for note unlock: {note['title']}",
        source_type="note",
        source_id=note_id,
        counterparty_user_id=note["uploaded_by"],
        metadata={"note_title": note["title"]},
    )
    if not held_wallet:
        raise HTTPException(status_code=400, detail="Not enough wallet balance")

    settlement = await release_held_funds(
        db,
        buyer_user_id=current_user["id"],
        seller_user_id=note["uploaded_by"],
        amount=cost,
        reason=f"Marketplace sale completed: {note['title']}",
        source_type="note",
        source_id=note_id,
        metadata={"note_title": note["title"]},
    )
    # Add to unlocked list
    await db.notes.update_one(
        {"_id": ObjectId(note_id)},
        {"$push": {"unlocked_by": current_user["id"]}}
    )
    return {
        "message": "Note unlocked successfully",
        "seller_payout": settlement["seller_payout"],
        "commission_amount": settlement["commission_amount"],
    }


@router.post("/buy")
async def buy_note(payload: dict, current_user=Depends(get_current_user)):
    note_id = str(payload.get("noteId") or payload.get("note_id") or "").strip()
    if not note_id:
        raise HTTPException(status_code=400, detail="note_id is required")
    return await unlock_note(note_id, current_user)

@router.post("/{note_id}/rate")
async def rate_note(note_id: str, rating: float, current_user=Depends(get_current_user)):
    db = get_db()
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be 1-5")
    note = await db.notes.find_one({"_id": ObjectId(note_id)})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    new_count = note.get("rating_count", 0) + 1
    new_rating = ((note.get("rating", 0) * note.get("rating_count", 0)) + rating) / new_count
    await db.notes.update_one(
        {"_id": ObjectId(note_id)},
        {"$set": {"rating": round(new_rating, 1), "rating_count": new_count}}
    )
    return {"message": "Rated successfully", "new_rating": round(new_rating, 1)}

@router.post("/{note_id}/download")
async def download_note(note_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    note = await db.notes.find_one({"_id": ObjectId(note_id)})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    is_unlocked = (
        (note.get("unlock_cost", 0) or 0) <= 0
        or current_user["id"] in [str(uid) for uid in note.get("unlocked_by", [])]
        or note.get("uploaded_by") == current_user["id"]
    )
    if not is_unlocked:
        raise HTTPException(status_code=403, detail="Note not unlocked")
    await db.notes.update_one({"_id": ObjectId(note_id)}, {"$inc": {"downloads": 1}})
    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$inc": {"xp": 10}}
    )
    return {"file_url": note["file_url"], "file_name": note["file_name"]}
