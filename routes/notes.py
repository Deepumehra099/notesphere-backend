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


configure_cloudinary()

def serialize_note(note):
    n = {**note}
    n["id"] = str(n.pop("_id"))
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
    sort_field = {"newest": ("created_at", -1), "popular": ("views", -1), "rating": ("rating", -1)}
    s = sort_field.get(sort, ("created_at", -1))
    skip = (page - 1) * limit
    cursor = db.notes.find(query).sort(s[0], s[1]).skip(skip).limit(limit)
    notes = []
    async for note in cursor:
        n = serialize_note(note)
        n["is_unlocked"] = current_user["id"] in n.get("unlocked_by", []) or n.get("uploaded_by") == current_user["id"]
        notes.append(n)
    total = await db.notes.count_documents(query)
    return {"notes": notes, "total": total, "page": page, "pages": (total + limit - 1) // limit}

@router.get("/my")
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
    n["is_unlocked"] = current_user["id"] in n.get("unlocked_by", []) or n.get("uploaded_by") == current_user["id"]
    return {"note": n}

@router.post("/upload")
async def upload_note(
    title: str = Form(...),
    description: str = Form(""),
    subject: str = Form(...),
    topic: str = Form(""),
    unlock_cost: int = Form(10),
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    configure_cloudinary()

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    public_id = f"notesphere/notes/{current_user['id']}_{int(datetime.now().timestamp())}"

    try:
        file.file.seek(0)
        result = cloudinary.uploader.upload(
            file.file,
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
            file.filename,
            secure_url,
        )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.exception("Cloudinary upload failed for filename=%s: %s", file.filename, e)
        raise HTTPException(status_code=500, detail="File upload failed")
    finally:
        await file.close()

    return {
        "message": "Upload successful",
        "file_url": secure_url,
    }

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
    user = await db.users.find_one({"_id": ObjectId(current_user["id"])})
    if user["tokens"] < cost:
        raise HTTPException(status_code=400, detail="Not enough tokens")
    # Deduct tokens from buyer
    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$inc": {"tokens": -cost}}
    )
    await db.transactions.insert_one({
        "user_id": current_user["id"],
        "amount": -cost,
        "type": "spend",
        "reason": f"Unlocked note: {note['title']}",
        "created_at": datetime.now(timezone.utc),
    })
    # Give tokens to uploader
    await db.users.update_one(
        {"_id": ObjectId(note["uploaded_by"])},
        {"$inc": {"tokens": cost // 2}}
    )
    await db.transactions.insert_one({
        "user_id": note["uploaded_by"],
        "amount": cost // 2,
        "type": "earn",
        "reason": f"Someone unlocked your note: {note['title']}",
        "created_at": datetime.now(timezone.utc),
    })
    # Add to unlocked list
    await db.notes.update_one(
        {"_id": ObjectId(note_id)},
        {"$push": {"unlocked_by": current_user["id"]}}
    )
    return {"message": "Note unlocked successfully"}

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
    is_unlocked = current_user["id"] in [str(uid) for uid in note.get("unlocked_by", [])] or note.get("uploaded_by") == current_user["id"]
    if not is_unlocked:
        raise HTTPException(status_code=403, detail="Note not unlocked")
    await db.notes.update_one({"_id": ObjectId(note_id)}, {"$inc": {"downloads": 1}})
    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$inc": {"xp": 10}}
    )
    return {"file_url": note["file_url"], "file_name": note["file_name"]}
