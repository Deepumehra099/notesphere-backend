from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from datetime import datetime, timezone
from bson import ObjectId
from utils.db import get_db
from utils.auth_utils import get_current_user
from typing import Optional
import cloudinary
import cloudinary.uploader
import os
import logging

router = APIRouter(prefix="/api/notes", tags=["notes"])
logger = logging.getLogger(__name__)

# Configure Cloudinary on module load
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
    secure=True,
)

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
    db = get_db()
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    content = await file.read()
    public_id = f"notessphere/notes/{current_user['id']}_{int(datetime.now().timestamp())}"

    try:
        result = cloudinary.uploader.upload(
            content,
            resource_type="raw",
            public_id=public_id,
            overwrite=True,
            access_mode="public",
        )
        file_url = result["secure_url"]
        logger.info(f"Cloudinary upload success: {file_url}")
    except Exception as e:
        logger.error(f"Cloudinary upload failed: {e}")
        raise HTTPException(status_code=500, detail="File upload failed. Please try again.")

    note_doc = {
        "title": title,
        "description": description,
        "subject": subject,
        "topic": topic,
        "file_url": file_url,
        "file_name": file.filename,
        "cloudinary_public_id": public_id,
        "uploaded_by": current_user["id"],
        "uploader_name": current_user["name"],
        "uploader_avatar": current_user.get("avatar_url", ""),
        "status": "pending",
        "rating": 0.0,
        "rating_count": 0,
        "views": 0,
        "downloads": 0,
        "unlock_cost": unlock_cost,
        "unlocked_by": [],
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.notes.insert_one(note_doc)

    # Earn tokens for uploading
    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$inc": {"tokens": 20, "xp": 50}}
    )
    await db.transactions.insert_one({
        "user_id": current_user["id"],
        "amount": 20,
        "type": "earn",
        "reason": f"Uploaded note: {title}",
        "created_at": datetime.now(timezone.utc),
    })

    note_doc["id"] = str(result.inserted_id)
    note_doc.pop("_id", None)
    return {"note": note_doc, "message": "Note uploaded to Cloudinary, pending approval"}

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
