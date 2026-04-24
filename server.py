from pathlib import Path
import os
from typing import Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING

from routes.ai import router as ai_router
from routes.account import orders_router, support_router, wishlist_router
from routes.admin import router as admin_router
from routes.auth import router as auth_router
from routes.chat import router as chat_router, ws_router as chat_ws_router
from routes.gigs import router as gigs_router
from routes.notes import router as notes_router, upload_note as upload_note_handler
from routes.payments import payment_router, router as payments_router
from routes.search import router as search_router
from routes.tasks import router as tasks_router
from routes.tokens import (
    build_wallet_payload,
    legacy_router as legacy_tokens_router,
    router as wallet_router,
)
from routes.user import router as user_router, search_router as user_search_router
from routes.withdrawals import (
    admin_plural_router as withdraw_admin_plural_router,
    admin_router as withdraw_admin_router,
    router as withdraw_router,
    wallet_router as withdraw_wallet_router,
)
from utils.db import get_db
from utils.auth_utils import get_current_user

load_dotenv()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


load_env_file(Path(__file__).resolve().parent / ".env")

app = FastAPI()
uploads_dir = Path(__file__).resolve().parent / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)

# ✅ CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

# =========================
# 🔐 ADMIN LOGIN MODEL
# =========================
class AdminLogin(BaseModel):
    email: str
    password: str


# =========================
# 🔐 ADMIN LOGIN API
# =========================
@app.post("/admin-login")
def admin_login(data: AdminLogin):
    admin_email = os.getenv("ADMIN_EMAIL")
    admin_password = os.getenv("ADMIN_PASSWORD")

    if not admin_email or not admin_password:
        raise HTTPException(status_code=500, detail="Admin credentials not set")

    if data.email == admin_email and data.password == admin_password:
        return {
            "success": True,
            "message": "Admin login successful"
        }
    else:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")


# =========================
# ROUTES
# =========================
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(notes_router)
app.include_router(ai_router)
app.include_router(chat_router)
app.include_router(chat_ws_router)
app.include_router(gigs_router)
app.include_router(wallet_router)
app.include_router(legacy_tokens_router)
app.include_router(payments_router)
app.include_router(payment_router)
app.include_router(search_router)
app.include_router(tasks_router)
app.include_router(user_router)
app.include_router(user_search_router)
app.include_router(orders_router)
app.include_router(wishlist_router)
app.include_router(support_router)
app.include_router(withdraw_router)
app.include_router(withdraw_wallet_router)
app.include_router(withdraw_admin_router)
app.include_router(withdraw_admin_plural_router)


@app.on_event("startup")
async def ensure_indexes():
    db = get_db()
    await db.tasks.create_index(
        [
            ("status", ASCENDING),
            ("created_by", ASCENDING),
            ("assigned_to", ASCENDING),
            ("is_urgent", DESCENDING),
            ("is_boosted", DESCENDING),
            ("popularity_score", DESCENDING),
            ("views", DESCENDING),
            ("clicks", DESCENDING),
            ("accepts", DESCENDING),
            ("boosted_at", DESCENDING),
            ("created_at", DESCENDING),
        ]
    )
    await db.tasks.create_index([("created_by", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
    await db.tasks.create_index([("assigned_to", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
    await db.task_analytics.create_index([("task_id", ASCENDING)], unique=True)
    await db.task_analytics.create_index([("popularity_score", DESCENDING), ("updated_at", DESCENDING)])
    await db.reports.create_index([("reported_id", ASCENDING), ("created_at", DESCENDING)])
    await db.reports.create_index([("reported_by", ASCENDING), ("created_at", DESCENDING)])
    await db.users.create_index([("email", ASCENDING)], unique=True)
    await db.users.create_index([("uid", ASCENDING)], unique=True)
    await db.users.create_index([("is_admin", ASCENDING), ("is_banned", ASCENDING), ("created_at", DESCENDING)])
    await db.gigs.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await db.gigs.create_index([("is_featured", DESCENDING), ("created_at", DESCENDING)])
    await db.notes.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    await db.notes.create_index([("status", ASCENDING), ("unlock_cost", ASCENDING), ("created_at", DESCENDING)])
    await db.chats.create_index([("task_id", ASCENDING)], unique=True)
    await db.messages.create_index([("chat_id", ASCENDING), ("created_at", DESCENDING)])
    await db.transactions.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await db.transactions.create_index([("status", ASCENDING), ("type", ASCENDING), ("created_at", DESCENDING)])
    await db.payment_orders.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await db.subscription_orders.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await db.support_tickets.create_index([("user_id", ASCENDING), ("updated_at", DESCENDING)])
    await db.withdraw_requests.create_index([("user_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])


# =========================
# HEALTH CHECK
# =========================
@app.get("/")
def root():
    return {"status": "running"}


@app.get("/api")
def api():
    return {"status": "ok"}


@app.get("/api/health")
def health():
    return {"status": "healthy"}


@app.get("/api/home")
async def home(current_user=Depends(get_current_user)):
    db = get_db()

    user = await db.users.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    wallet = await build_wallet_payload(current_user)

    notifications = []
    async for item in db.notifications.find({"user_id": current_user["id"]}).sort("created_at", -1).limit(5):
        notifications.append(
            {
                "id": str(item["_id"]),
                "title": item.get("title", ""),
                "body": item.get("body", ""),
                "read": bool(item.get("read", False)),
                "createdAt": item.get("created_at").isoformat() if item.get("created_at") else None,
            }
        )

    notes = []
    async for note in db.notes.find({"status": "approved"}).sort("created_at", -1).limit(3):
        serialized = {
            "id": str(note["_id"]),
            "title": note.get("title", ""),
            "description": note.get("description", ""),
            "subject": note.get("subject", ""),
            "topic": note.get("topic", ""),
            "price": int(note.get("unlock_cost", 0) or 0),
            "thumbnailUrl": note.get("thumbnail_url", note.get("cover_url", "")),
            "file_url": note.get("file_url", ""),
            "downloads": int(note.get("downloads", 0) or 0),
            "createdAt": note.get("created_at").isoformat() if note.get("created_at") else None,
        }
        notes.append(serialized)

    tasks = []
    async for task in db.tasks.find({"status": "open"}).sort(
        [("is_urgent", -1), ("is_boosted", -1), ("popularity_score", -1), ("created_at", -1)]
    ).limit(12):
        tasks.append(
            {
                "id": str(task["_id"]),
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "budget": int(task.get("price", 0) or 0),
                "price": int(task.get("price", 0) or 0),
                "mode": "nearby" if task.get("location") else "remote",
                "urgency": "urgent" if task.get("is_urgent") else "normal",
                "boosted": bool(task.get("is_boosted", False)),
                "trendingScore": int(task.get("popularity_score", 0) or 0),
                "location": task.get("location", ""),
                "createdAt": task.get("created_at").isoformat() if task.get("created_at") else None,
            }
        )

    boosted_tasks = [task for task in tasks if task.get("boosted")][:5]
    trending_tasks = sorted(tasks, key=lambda task: int(task.get("trendingScore", 0) or 0), reverse=True)[:5]

    return {
        "user": {
            "id": str(user["_id"]),
            "uid": user.get("uid", ""),
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "role": "admin" if user.get("is_admin") else user.get("role", "user"),
            "is_admin": bool(user.get("is_admin", False)),
            "rating": round(float(user.get("task_rating", 0) or 0), 1),
            "avatarUrl": user.get("avatar_url") or user.get("avatar", ""),
            "wallet_balance": wallet.get("balance", 0),
            "wallet": {
                "balance": wallet.get("balance", 0),
                "earnings": wallet.get("total_earned", 0),
                "pendingAmount": wallet.get("pendingAmount", 0),
                "totalWithdrawn": wallet.get("totalWithdrawn", 0),
            },
            "stats": {
                "notesCount": int(user.get("notes_count", 0) or 0),
                "tasksPosted": int(user.get("tasks_posted", 0) or 0),
                "tasksCompleted": int(user.get("tasks_completed", user.get("completed_tasks", 0)) or 0),
                "gigsCount": int(user.get("gigs_count", 0) or 0),
                "downloads": int(user.get("downloads_count", 0) or 0),
            },
        },
        "notifications": notifications,
        "metrics": {
            "walletBalance": wallet.get("balance", 0),
            "notesCount": int(user.get("notes_count", 0) or 0),
            "tasksCount": int(user.get("tasks_posted", 0) or 0),
            "earnings": wallet.get("total_earned", 0),
            "rating": round(float(user.get("task_rating", 0) or 0), 1),
        },
        "boostedTasks": boosted_tasks,
        "trendingTasks": trending_tasks,
        "taskFeed": tasks,
        "notesPreview": notes,
    }


@app.get("/api/my-notes")
async def my_notes_compat(current_user=Depends(get_current_user)):
    db = get_db()
    notes = []
    async for note in db.notes.find({"uploaded_by": current_user["id"]}).sort("created_at", -1):
        notes.append(
            {
                "id": str(note["_id"]),
                "title": note.get("title", ""),
                "description": note.get("description", ""),
                "subject": note.get("subject", ""),
                "topic": note.get("topic", ""),
                "price": int(note.get("unlock_cost", 0) or 0),
                "status": note.get("status", "pending"),
                "file_url": note.get("file_url", ""),
                "thumbnail_url": note.get("thumbnail_url", ""),
                "created_at": note.get("created_at").isoformat() if note.get("created_at") else None,
            }
        )
    return {"notes": notes}


@app.post("/api/upload-note")
async def upload_note_compat(
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
    return await upload_note_handler(
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


# =========================
# RUN SERVER
# =========================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
