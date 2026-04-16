from fastapi import APIRouter, Depends
from utils.db import get_db
from utils.auth_utils import get_current_user
from typing import Optional

router = APIRouter(prefix="/api/search", tags=["search"])
db = get_db()

@router.get("/notes")
async def search_notes(
    q: str = "",
    subject: Optional[str] = None,
    sort: str = "newest",
    page: int = 1,
    limit: int = 20,
    current_user=Depends(get_current_user)
):
    query = {"status": "approved"}
    if q:
        query["$or"] = [
            {"title": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}},
            {"subject": {"$regex": q, "$options": "i"}},
            {"topic": {"$regex": q, "$options": "i"}},
        ]
    if subject:
        query["subject"] = {"$regex": subject, "$options": "i"}

    sort_map = {"newest": ("created_at", -1), "popular": ("views", -1), "rating": ("rating", -1)}
    s = sort_map.get(sort, ("created_at", -1))
    skip = (page - 1) * limit

    cursor = db.notes.find(query).sort(s[0], s[1]).skip(skip).limit(limit)
    notes = []
    async for note in cursor:
        n = {**note}
        n["id"] = str(n.pop("_id"))
        n["unlocked_by"] = [str(uid) for uid in n.get("unlocked_by", [])]
        n["is_unlocked"] = current_user["id"] in n.get("unlocked_by", []) or n.get("uploaded_by") == current_user["id"]
        notes.append(n)

    total = await db.notes.count_documents(query)

    # Get unique subjects for filter
    subjects = await db.notes.distinct("subject", {"status": "approved"})

    return {"notes": notes, "total": total, "subjects": subjects, "page": page}

@router.get("/suggestions")
async def get_suggestions(q: str = "", current_user=Depends(get_current_user)):
    if not q:
        # Return popular subjects
        subjects = await db.notes.distinct("subject", {"status": "approved"})
        return {"suggestions": subjects[:10]}
    cursor = db.notes.find(
        {"status": "approved", "title": {"$regex": q, "$options": "i"}},
        {"title": 1, "subject": 1}
    ).limit(10)
    suggestions = []
    async for note in cursor:
        suggestions.append({"title": note["title"], "subject": note["subject"]})
    return {"suggestions": suggestions}
