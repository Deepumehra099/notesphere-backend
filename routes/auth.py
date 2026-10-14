import os
import secrets
import string
import hashlib
import hmac
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone

import jwt

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from utils.auth_utils import (
    create_access_token,
    create_refresh_token,
    get_jwt_secret,
    get_current_user,
    hash_password,
    verify_password,
)
from utils.db import get_client, get_db
from utils.wallets import ensure_wallet
from utils.credits import apply_credit_change, generate_referral_code, get_credit_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterInput(BaseModel):
    name: str
    username: str = ""
    email: str
    password: str
    phone: str = ""
    branch: str = ""
    semester: int = 1
    location: str = ""
    skills: list[str] = []
    referral_code: str = Field(default="", alias="referralCode")


class LoginInput(BaseModel):
    email: str
    password: str


class RefreshInput(BaseModel):
    refresh_token: str


@router.get("/username-availability")
async def username_availability(username: str = Query(..., min_length=3, max_length=25)):
    username = username.strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        raise HTTPException(status_code=400, detail="Username must be 3–24 characters and use only letters, numbers, or underscores")
    existing = await get_db().users.find_one(
        {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
        {"_id": 1},
    )
    return {"available": existing is None}


class ForgotPasswordInput(BaseModel):
    email: str


class ResetPasswordInput(BaseModel):
    email: str
    otp: str
    newPassword: str


def _otp_digest(email: str, otp: str) -> str:
    secret = os.getenv("JWT_SECRET", "notesphere-password-reset")
    return hmac.new(secret.encode(), f"{email}:{otp}".encode(), hashlib.sha256).hexdigest()


def _send_password_reset_email(email: str, otp: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.getenv("SMTP_USER", os.getenv("EMAIL_USER", ""))
    # Google displays App Passwords grouped with spaces (xxxx xxxx xxxx xxxx).
    # SMTP authentication expects the raw 16-character value.
    password = "".join(os.getenv("SMTP_PASSWORD", os.getenv("EMAIL_PASS", "")).split())
    sender = os.getenv("SMTP_FROM", username)
    if not username or not password or not sender:
        raise RuntimeError("Email delivery is not configured. Set SMTP_USER and SMTP_PASSWORD in backend/.env")

    message = EmailMessage()
    message["Subject"] = "Notesphere password reset code"
    message["From"] = sender
    message["To"] = email
    message.set_content(f"Your Notesphere password reset code is {otp}. It expires in 5 minutes.")
    message.add_alternative(
        f"<p>Your Notesphere password reset code is:</p><p style='font-size:28px;font-weight:bold;letter-spacing:6px'>{otp}</p><p>This code expires in 5 minutes. If you didn't request a reset, ignore this email.</p>",
        subtype="html",
    )
    with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


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
        "username": user.get("username", ""),
        "email": user.get("email", ""),
        "role": "admin" if is_admin else user.get("role", "user"),
        "is_admin": is_admin,
        "is_banned": bool(user.get("is_banned", False)),
        "last_login_at": user.get("last_login_at"),
        "tokens": int(user.get("credits_balance", user.get("tokens", 0)) or 0),
        "wallet_balance": wallet_balance,
        "wallet_available": wallet_balance,
        "wallet_held": held_balance,
        "credits_balance": int(user.get("credits_balance", 0) or 0),
        "referral_code": user.get("referral_code", ""),
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
    username = data.username.strip().removeprefix("@").lower()

    if username and not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        raise HTTPException(status_code=400, detail="Username must be 3–24 characters and use only letters, numbers, or underscores")

    existing = await users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")

    if username and await users.find_one({"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}}, {"_id": 1}):
        raise HTTPException(status_code=400, detail="Username is already taken")

    referrer = None
    referral_code = data.referral_code.strip().upper()
    if referral_code:
        referrer = await users.find_one({"referral_code": referral_code}, {"_id": 1, "email": 1})
        if not referrer:
            raise HTTPException(status_code=400, detail="Referral code is invalid")
        if referrer.get("email", "").strip().lower() == email:
            raise HTTPException(status_code=400, detail="You cannot refer your own account")

    for _ in range(12):
        user_referral_code = generate_referral_code()
        if not await users.find_one({"referral_code": user_referral_code}, {"_id": 1}):
            break
    else:
        raise HTTPException(status_code=500, detail="Unable to create a referral code")

    uid = await generate_unique_uid(users)
    username_base = "".join(character.lower() if character.isalnum() else "_" for character in data.name.strip()).strip("_")[:28] or "user"
    user_doc = {
        "uid": uid,
        "username": username or f"{username_base}_{uid.lower()}",
        "name": data.name.strip(),
        "email": email,
        "password_hash": hash_password(data.password),
        "role": "user",
        "is_admin": False,
        "is_banned": False,
        "tokens": 0,
        "credits_balance": 0,
        "referral_code": user_referral_code,
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
    settings = await get_credit_settings(db)
    client = get_client()
    try:
        async with await client.start_session() as session:
            async with session.start_transaction():
                result = await users.insert_one(user_doc, session=session)
                user_doc["_id"] = result.inserted_id
                user_id = str(result.inserted_id)
                if settings["signup_reward"]:
                    await apply_credit_change(
                        db,
                        user_id=user_id,
                        amount=settings["signup_reward"],
                        transaction_type="SIGNUP_REWARD",
                        description="Welcome to NotesSphere",
                        idempotency_key=f"signup:{user_id}",
                        reference_id=user_id,
                        session=session,
                    )
                if referrer:
                    await db.referrals.insert_one({
                        "referrer_user_id": str(referrer["_id"]),
                        "referred_user_id": user_id,
                        "referral_code": referral_code,
                        "status": "registered",
                        "created_at": datetime.now(timezone.utc),
                    }, session=session)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="Account or referral code already exists. Please try again.") from exc

    wallet = await ensure_wallet(db, str(result.inserted_id), seed_balance=0)
    user_doc["credits_balance"] = settings["signup_reward"]

    return {
        "message": "User registered successfully",
        "access_token": create_access_token(str(result.inserted_id), email),
        "refresh_token": create_refresh_token(str(result.inserted_id)),
        "user": serialize_user(user_doc, wallet),
    }


@router.post("/forgot-password")
@router.post("/send-otp")
async def send_password_reset_otp(data: ForgotPasswordInput):
    email = data.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(status_code=400, detail="Enter a valid email address")

    db = get_db()
    user = await db.users.find_one({"email": email}, {"_id": 1, "email": 1})
    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email")

    otp = f"{secrets.randbelow(1_000_000):06d}"
    expiry = datetime.now(timezone.utc).timestamp() + 300
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_reset_otp_hash": _otp_digest(email, otp), "password_reset_otp_expiry": expiry}},
    )
    try:
        import asyncio
        await asyncio.get_running_loop().run_in_executor(None, _send_password_reset_email, email, otp)
    except Exception as exc:
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$unset": {"password_reset_otp_hash": "", "password_reset_otp_expiry": ""}},
        )
        raise HTTPException(status_code=503, detail="Could not send OTP email. Check SMTP settings and try again.") from exc
    return {"message": "OTP sent to your email. It expires in 5 minutes."}


@router.post("/reset-password")
async def reset_password(data: ResetPasswordInput):
    email = data.email.strip().lower()
    otp = data.otp.strip()
    if "@" not in email or len(otp) != 6 or not otp.isdigit():
        raise HTTPException(status_code=400, detail="Enter a valid email and 6-digit OTP")
    if len(data.newPassword) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    db = get_db()
    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=400, detail="OTP is invalid or expired. Request a new one.")
    expiry = user.get("password_reset_otp_expiry", 0)
    if isinstance(expiry, datetime):
        expiry = expiry.timestamp()
    expected = user.get("password_reset_otp_hash", "")
    if not expected or float(expiry or 0) < datetime.now(timezone.utc).timestamp() or not hmac.compare_digest(expected, _otp_digest(email, otp)):
        raise HTTPException(status_code=400, detail="OTP is invalid or expired. Request a new one.")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hash_password(data.newPassword)}, "$unset": {"password": "", "password_reset_otp_hash": "", "password_reset_otp_expiry": ""}},
    )
    return {"message": "Password reset successful. Sign in with your new password."}


@router.post("/login")
async def login(data: LoginInput):
    db = get_db()
    users = db["users"]
    identifier = data.email.strip()
    email = identifier.lower()
    username = identifier.removeprefix("@").lower()
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    is_configured_admin_login = bool(
        admin_email and admin_password and email == admin_email and data.password == admin_password
    )

    user = await users.find_one({"$or": [
        {"email": email},
        {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
    ]})
    if not user and is_configured_admin_login:
        admin_doc = {
            "uid": await generate_unique_uid(users),
            "name": os.getenv("ADMIN_NAME", "Admin").strip() or "Admin",
            "email": email,
            "password_hash": hash_password(data.password),
            "role": "admin",
            "is_admin": True,
            "is_banned": False,
            "tokens": 0,
            "credits_balance": 0,
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
    if is_configured_admin_login:
        password_matches = True
    else:
        password_matches = (
            verify_password(data.password, password_hash)
            if password_hash
            else user.get("password") == data.password
        )
    if not password_matches:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("is_banned") and is_configured_admin_login:
        # Recover the configured owner/admin account if it was accidentally
        # banned from the admin user list. Env credentials remain authoritative.
        await users.update_one(
            {"_id": user["_id"]},
            {"$set": {"is_banned": False, "is_admin": True, "role": "admin"}},
        )
        user["is_banned"] = False
        user["is_admin"] = True
        user["role"] = "admin"
    elif user.get("is_banned"):
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
        "refresh_token": create_refresh_token(str(user["_id"])),
        "user": serialize_user(user, wallet),
    }


@router.post("/refresh")
async def refresh_session(data: RefreshInput):
    try:
        payload = jwt.decode(data.refresh_token, get_jwt_secret(), algorithms=["HS256"])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        user_id = str(ObjectId(payload["sub"]))
    except HTTPException:
        raise
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Refresh session expired. Please sign in again.")

    user = await get_db().users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("is_banned"):
        raise HTTPException(status_code=403, detail="Your account has been banned")

    return {
        "access_token": create_access_token(user_id, user.get("email", "")),
        "refresh_token": create_refresh_token(user_id),
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
