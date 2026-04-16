from fastapi import APIRouter, Depends
from utils.db import get_db
from utils.auth_utils import get_current_user
from bson import ObjectId

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

@router.get("/wallet")
async def get_wallet(current_user=Depends(get_current_user)):
    db = get_db()
    user = await db.users.find_one(
        {"_id": ObjectId(current_user["id"])},
        {"tokens": 1, "xp": 1, "streak": 1}
    )
    return {
        "tokens": user.get("tokens", 0),
        "xp": user.get("xp", 0),
        "streak": user.get("streak", 0),
    }

@router.get("/transactions")
async def get_transactions(
    page: int = 1,
    limit: int = 20,
    current_user=Depends(get_current_user)
):
    db = get_db()
    skip = (page - 1) * limit
    cursor = db.transactions.find(
        {"user_id": current_user["id"]},
        {"_id": 0}
    ).sort("created_at", -1).skip(skip).limit(limit)
    transactions = []
    async for t in cursor:
        transactions.append(t)
    total = await db.transactions.count_documents({"user_id": current_user["id"]})
    return {"transactions": transactions, "total": total, "page": page}
