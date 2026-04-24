import os
import secrets
import string
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from utils.auth_utils import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from utils.db import get_db
from utils.wallets import ensure_wallet, record_transaction

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterInput(BaseModel):
    name: str
    email: str
    password: str
    phone: str = ""
    branch: str = ""
    semester: int = 1
    location: str = ""
    skills: list[str] = []


class LoginInput(BaseModel):
    email: str
    password: str


def serialize_user(user: dict, wallet: dict | None = None) -> dict:
    avatar_url = user.get("avatar_url") or user.get("avatar", "")
    wallet_balance = int((wallet or {}).get("available_balance", user.get("tokens", 0)) or 0)
    held_balance = int((wallet or {}).get("held_balance", 0) or 0)
    is_admin = bool(user.get("is_admin", False) or user.get("role") == "admin")
    is_verified = bool(user.get("is_verified", user.get("verified", False)))
    return {
        "id": str(user.get("_id", user.get("id", ""))),
        "uid": user.get("uid", ""),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "role": "admin" if is_admin else user.get("role", "user"),
        "is_admin": is_admin,
        "is_banned": bool(user.get("is_banned", False)),
        "tokens": wallet_balance,
        "wallet_balance": wallet_balance,
        "wallet_available": wallet_balance,
        "wallet_held": held_balance,
        "xp": user.get("xp", 0),
        "streak": user.get("streak", 0),
        "avatar": avatar_url,
        "avatar_url": avatar_url,
        "bio": user.get("bio", ""),
        "phone": user.get("phone", ""),
        "location": user.get("location", ""),
        "language": user.get("language", "en"),
        "skills": user.get("skills", []),
        "branch": user.get("branch", ""),
        "semester": user.get("semester", 1),
        "verified": is_verified,
        "is_verified": is_verified,
        "rating": round(float(user.get("task_rating", 0) or 0), 1),
        "task_rating": round(float(user.get("task_rating", 0) or 0), 1),
        "task_rating_count": int(user.get("task_rating_count", 0) or 0),
        "completed_tasks": int(user.get("completed_tasks", 0) or 0),
        "tasks_completed": int(user.get("tasks_completed", user.get("completed_tasks", 0)) or 0),
        "elite_buyer_active": bool(user.get("elite_buyer_active", False)),
        "elite_seller_active": bool(user.get("elite_seller_active", False)),
        "elite_seller_status": user.get("elite_seller_status", "none"),
    }


async def generate_unique_uid(users_collection) -> str:
    alphabet = string.digits

    for _ in range(12):
        candidate = f"NS{''.join(secrets.choice(alphabet) for _ in range(5))}"
        if not await users_collection.find_one({"uid": candidate}, {"_id": 1}):
            return candidate

    raise HTTPException(status_code=500, detail="Unable to generate user UID")


async def ensure_user_uid(users_collection, user: dict) -> dict:
    if user.get("uid"):
        return user

    uid = await generate_unique_uid(users_collection)
    await users_collection.update_one({"_id": user["_id"]}, {"$set": {"uid": uid}})
    user["uid"] = uid
    return user


@router.post("/register")
async def register(data: RegisterInput):
    db = get_db()
    users = db["users"]
    email = data.email.strip().lower()

    existing = await users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")

    user_doc = {
        "uid": await generate_unique_uid(users),
        "name": data.name.strip(),
        "email": email,
        "password_hash": hash_password(data.password),
        "role": "user",
        "is_admin": False,
        "is_banned": False,
        "tokens": 100,
        "xp": 0,
        "streak": 0,
        "avatar": "",
        "avatar_url": "",
        "bio": "",
        "phone": data.phone.strip() if data.phone else "",
        "location": data.location.strip() if data.location else "",
        "language": "en",
        "skills": [skill.strip() for skill in data.skills if skill and skill.strip()],
        "branch": data.branch.strip() if data.branch else "",
        "semester": data.semester or 1,
        "verified": False,
        "is_verified": False,
        "task_rating": 0,
        "task_rating_count": 0,
        "completed_tasks": 0,
        "tasks_completed": 0,
        "blocked_user_ids": [],
        "wishlist_note_ids": [],
        "elite_buyer_active": False,
        "elite_seller_active": False,
        "elite_seller_status": "none",
        "created_at": datetime.now(timezone.utc),
    }
    result = await users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id

    wallet = await ensure_wallet(db, str(result.inserted_id), seed_balance=100)
    await record_transaction(
        db,
        user_id=str(result.inserted_id),
        amount=100,
        transaction_type="earn",
        reason="Welcome wallet bonus",
        source_type="auth",
        source_id=str(result.inserted_id),
    )

    return {
        "message": "User registered successfully",
        "access_token": create_access_token(str(result.inserted_id), email),
        "user": serialize_user(user_doc, wallet),
    }


@router.post("/login")
async def login(data: LoginInput):
    db = get_db()
    users = db["users"]
    email = data.email.strip().lower()
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()

    user = await users.find_one({"email": email})
    if not user and admin_email and admin_password and email == admin_email and data.password == admin_password:
        admin_doc = {
            "uid": await generate_unique_uid(users),
            "name": os.getenv("ADMIN_NAME", "Admin").strip() or "Admin",
            "email": email,
            "password_hash": hash_password(data.password),
            "role": "admin",
            "is_admin": True,
            "is_banned": False,
            "tokens": 0,
            "xp": 0,
            "streak": 0,
            "avatar": "",
            "avatar_url": "",
            "bio": "",
            "phone": "",
            "location": "",
            "language": "en",
            "skills": [],
            "branch": "",
            "semester": 1,
            "verified": True,
            "is_verified": True,
            "task_rating": 0,
            "task_rating_count": 0,
            "completed_tasks": 0,
            "tasks_completed": 0,
            "blocked_user_ids": [],
            "wishlist_note_ids": [],
            "elite_buyer_active": False,
            "elite_seller_active": False,
            "elite_seller_status": "none",
            "created_at": datetime.now(timezone.utc),
        }
        result = await users.insert_one(admin_doc)
        admin_doc["_id"] = result.inserted_id
        user = admin_doc

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = await ensure_user_uid(users, user)

    password_hash = user.get("password_hash")
    password_matches = False
    if admin_email and admin_password and email == admin_email and data.password == admin_password:
        password_matches = True
    else:
        password_matches = (
            verify_password(data.password, password_hash)
            if password_hash
            else user.get("password") == data.password
        )
    if not password_matches:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("is_banned"):
        raise HTTPException(status_code=403, detail="Your account has been banned")

    if not password_hash and user.get("password"):
        password_hash = hash_password(data.password)
        await users.update_one(
            {"_id": user["_id"]},
            {"$set": {"password_hash": password_hash}, "$unset": {"password": ""}},
        )
        user["password_hash"] = password_hash
        user.pop("password", None)

    await users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login_at": datetime.now(timezone.utc)}},
    )
    user["last_login_at"] = datetime.now(timezone.utc)
    wallet = await ensure_wallet(db, str(user["_id"]))

    return {
        "message": "Login successful",
        "access_token": create_access_token(str(user["_id"]), user["email"]),
        "user": serialize_user(user, wallet),
    }


@router.get("/me")
async def get_me(current_user=Depends(get_current_user)):
    db = get_db()
    users = db["users"]
    user_record = await users.find_one({"_id": ObjectId(current_user["id"])})
    if not user_record:
        raise HTTPException(status_code=404, detail="User not found")
    if user_record.get("is_banned"):
        raise HTTPException(status_code=403, detail="Your account has been banned")

    user_record = await ensure_user_uid(users, user_record)
    wallet = await ensure_wallet(db, current_user["id"])
    return {"user": serialize_user(user_record, wallet)}
