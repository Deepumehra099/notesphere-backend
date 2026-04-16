from datetime import datetime, timezone

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
    return {
        "id": str(user["_id"]),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "role": user.get("role", "user"),
        "tokens": user.get("tokens", 0),
        "xp": user.get("xp", 0),
        "streak": user.get("streak", 0),
        "avatar_url": user.get("avatar_url", ""),
        "bio": user.get("bio", ""),
        "branch": user.get("branch", ""),
        "semester": user.get("semester", 1),
    }


@router.post("/register")
async def register(data: RegisterInput):
    db = get_db()
    users = db["users"]
    email = data.email.strip().lower()

    existing = await users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")

    user_doc = {
        "name": data.name.strip(),
        "email": email,
        "password_hash": hash_password(data.password),
        "role": "user",
        "tokens": 100,
        "xp": 0,
        "streak": 0,
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
    return {"user": current_user}
