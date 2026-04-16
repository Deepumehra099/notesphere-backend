from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone
from bson import ObjectId
from utils.db import get_db
from utils.auth_utils import get_current_user

router = APIRouter(prefix="/api/admin", tags=["admin"])
db = get_db()

async def require_admin(current_user=Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

@router.get("/pending-notes")
async def get_pending_notes(admin=Depends(require_admin)):
    cursor = db.notes.find({"status": "pending"}).sort("created_at", -1)
    notes = []
    async for note in cursor:
        n = {**note}
        n["id"] = str(n.pop("_id"))
        n.pop("unlocked_by", None)
        notes.append(n)
    return {"notes": notes}

@router.post("/notes/{note_id}/approve")
async def approve_note(note_id: str, admin=Depends(require_admin)):
    result = await db.notes.update_one(
        {"_id": ObjectId(note_id)},
        {"$set": {"status": "approved", "approved_at": datetime.now(timezone.utc)}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note approved"}

@router.post("/notes/{note_id}/reject")
async def reject_note(note_id: str, admin=Depends(require_admin)):
    result = await db.notes.update_one(
        {"_id": ObjectId(note_id)},
        {"$set": {"status": "rejected", "rejected_at": datetime.now(timezone.utc)}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note rejected"}

@router.get("/users")
async def get_all_users(admin=Depends(require_admin)):
    cursor = db.users.find({}, {"password_hash": 0})
    users = []
    async for user in cursor:
        u = {**user}
        u["id"] = str(u.pop("_id"))
        users.append(u)
    return {"users": users}

@router.get("/analytics")
async def get_analytics(admin=Depends(require_admin)):
    total_users = await db.users.count_documents({})
    total_notes = await db.notes.count_documents({})
    pending_notes = await db.notes.count_documents({"status": "pending"})
    approved_notes = await db.notes.count_documents({"status": "approved"})
    total_transactions = await db.transactions.count_documents({})
    return {
        "total_users": total_users,
        "total_notes": total_notes,
        "pending_notes": pending_notes,
        "approved_notes": approved_notes,
        "total_transactions": total_transactions,
    }
