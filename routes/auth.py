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

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterInput(BaseModel):
    name: str
    email: str
    password: str
    branch: str = ""
    semester: int = 1


class LoginInput(BaseModel):
    email: str
    password: str


def serialize_user(user: dict) -> dict:
    avatar_url = user.get("avatar_url") or user.get("avatar", "")
    return {
        "id": str(user.get("_id", user.get("id", ""))),
        "uid": user.get("uid", ""),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "role": user.get("role", "user"),
        "tokens": user.get("tokens", 0),
        "xp": user.get("xp", 0),
        "streak": user.get("streak", 0),
        "avatar": avatar_url,
        "avatar_url": avatar_url,
        "bio": user.get("bio", ""),
        "branch": user.get("branch", ""),
        "semester": user.get("semester", 1),
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
        "tokens": 100,
        "xp": 0,
        "streak": 0,
        "avatar": "",
        "avatar_url": "",
        "bio": "",
        "branch": data.branch.strip() if data.branch else "",
        "semester": data.semester or 1,
        "created_at": datetime.now(timezone.utc),
    }
    result = await users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id

    await db["transactions"].insert_one({
        "user_id": str(result.inserted_id),
        "amount": 100,
        "type": "earn",
        "reason": "Welcome bonus",
        "created_at": datetime.now(timezone.utc),
    })

    return {
        "message": "User registered successfully",
        "access_token": create_access_token(str(result.inserted_id), email),
        "user": serialize_user(user_doc),
    }


@router.post("/login")
async def login(data: LoginInput):
    db = get_db()
    users = db["users"]
    email = data.email.strip().lower()

    user = await users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = await ensure_user_uid(users, user)

    password_hash = user.get("password_hash")
    password_matches = (
        verify_password(data.password, password_hash)
        if password_hash
        else user.get("password") == data.password
    )
    if not password_matches:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not password_hash and user.get("password"):
        password_hash = hash_password(data.password)
        await users.update_one(
            {"_id": user["_id"]},
            {"$set": {"password_hash": password_hash}, "$unset": {"password": ""}},
        )
        user["password_hash"] = password_hash
        user.pop("password", None)

    return {
        "message": "Login successful",
        "access_token": create_access_token(str(user["_id"]), user["email"]),
        "user": serialize_user(user),
    }


@router.get("/me")
async def get_me(current_user=Depends(get_current_user)):
    db = get_db()
    users = db["users"]
    user_record = await users.find_one({"_id": ObjectId(current_user["id"])})
    if not user_record:
        raise HTTPException(status_code=404, detail="User not found")

    user_record = await ensure_user_uid(users, user_record)
    return {"user": serialize_user(user_record)}
