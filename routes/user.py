import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import cloudinary
import cloudinary.uploader
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from routes.auth import serialize_user
from utils.auth_utils import get_current_user, hash_password
from utils.db import get_db
from utils.push_notifications import send_push_notification

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")
UPLOADS_DIR = Path(__file__).resolve().parents[1] / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/api/user", tags=["user"])
search_router = APIRouter(prefix="/api/users", tags=["user"])
logger = logging.getLogger(__name__)


class ReportUserInput(BaseModel):
    user_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=3, max_length=500)


class BlockUserInput(BaseModel):
    user_id: str = Field(..., min_length=1)


def parse_skills_input(raw_value: str) -> list[str]:
    seen: set[str] = set()
    skills: list[str] = []
    for part in raw_value.split(","):
        skill = part.strip()
        normalized = skill.lower()
        if not skill or normalized in seen:
            continue
        seen.add(normalized)
        skills.append(skill)
    return skills


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


async def search_user_by_uid_impl(
    uid: str = Query(..., min_length=3),
    current_user=Depends(get_current_user),
):
    db = get_db()
    clean_uid = uid.strip().upper()

    user = await db.users.find_one(
        {
            "uid": clean_uid,
            "_id": {"$ne": ObjectId(current_user["id"])},
        },
        {"name": 1, "avatar_url": 1, "avatar": 1, "uid": 1, "username": 1},
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "user": {
            "id": str(user["_id"]),
            "name": user.get("name", ""),
            "avatar_url": user.get("avatar_url") or user.get("avatar", ""),
            "uid": user.get("uid", ""),
            "username": user.get("username", ""),
        }
    }


@router.get("/search")
async def search_user_by_uid(
    uid: str = Query(..., min_length=3),
    current_user=Depends(get_current_user),
):
    return await search_user_by_uid_impl(uid=uid, current_user=current_user)


@search_router.get("/search")
async def search_user_by_uid_alias(
    uid: str = Query(..., min_length=3),
    current_user=Depends(get_current_user),
):
    return await search_user_by_uid_impl(uid=uid, current_user=current_user)


@router.put("/profile")
async def update_profile(
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    bio: str = Form(""),
    location: str = Form(""),
    language: str = Form("en"),
    skills: str = Form(""),
    password: str = Form(""),
    avatar: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    clean_name = name.strip()
    clean_email = email.strip().lower()
    clean_phone = phone.strip()
    clean_bio = bio.strip()
    clean_location = location.strip()
    clean_language = (language.strip().lower() or "en")
    clean_skills = parse_skills_input(skills)
    clean_password = password.strip()

    if not clean_name:
        raise HTTPException(status_code=400, detail="Name is required")

    if not clean_email:
        raise HTTPException(status_code=400, detail="Email is required")

    if clean_password and len(clean_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    if clean_language not in {"en", "hi"}:
        raise HTTPException(status_code=400, detail="Language must be en or hi")

    avatar_url = current_user.get("avatar_url") or current_user.get("avatar", "")

    if avatar is not None:
        if not avatar.filename:
            raise HTTPException(status_code=400, detail="Invalid avatar file")

        content_type = (avatar.content_type or "").lower()
        if content_type and not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Avatar must be an image")

        configure_cloudinary()

        try:
            avatar.file.seek(0)
            upload_result = cloudinary.uploader.upload(
                avatar.file,
                resource_type="image",
                folder="notesphere/avatars",
                public_id=f"user_{current_user['id']}",
                overwrite=True,
            )
            avatar_url = (upload_result.get("secure_url") or "").strip()
            if not avatar_url:
                raise HTTPException(status_code=500, detail="Cloudinary did not return an avatar URL")

            logger.info("Profile avatar uploaded successfully for user_id=%s", current_user["id"])
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Avatar upload failed for user_id=%s: %s", current_user["id"], e)
            raise HTTPException(status_code=500, detail="Avatar upload failed")
        finally:
            await avatar.close()

    db = get_db()
    existing_user = await db.users.find_one(
        {
            "email": clean_email,
            "_id": {"$ne": ObjectId(current_user["id"])},
        },
        {"_id": 1},
    )
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already exists")

    updates = {
        "name": clean_name,
        "email": clean_email,
        "phone": clean_phone,
        "bio": clean_bio,
        "location": clean_location,
        "language": clean_language,
        "skills": clean_skills,
        "avatar": avatar_url,
        "avatar_url": avatar_url,
    }
    if clean_password:
        updates["password_hash"] = hash_password(clean_password)

    updated_user = await db.users.find_one_and_update(
        {"_id": ObjectId(current_user["id"])},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )

    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")

    logger.info("Profile updated successfully for user_id=%s", current_user["id"])
    return {"user": serialize_user(updated_user)}


@router.post("/upload-avatar")
async def upload_avatar(
    avatar: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    if not avatar.filename:
        raise HTTPException(status_code=400, detail="Avatar file is required")

    content_type = (avatar.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Avatar must be an image")

    file_ext = Path(avatar.filename).suffix or ".jpg"
    filename = f"avatar_{current_user['id']}_{int(datetime.utcnow().timestamp())}{file_ext}"
    destination = UPLOADS_DIR / filename

    try:
        with destination.open("wb") as buffer:
            shutil.copyfileobj(avatar.file, buffer)
    finally:
        await avatar.close()

    avatar_url = f"/uploads/{filename}"
    db = get_db()
    updated_user = await db.users.find_one_and_update(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {"avatar": avatar_url, "avatar_url": avatar_url}},
        return_document=ReturnDocument.AFTER,
    )

    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "message": "Avatar uploaded successfully",
        "avatar": avatar_url,
        "user": serialize_user(updated_user),
    }


@router.put("/update")
async def update_profile_alias(
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    bio: str = Form(""),
    location: str = Form(""),
    language: str = Form("en"),
    skills: str = Form(""),
    password: str = Form(""),
    avatar: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    return await update_profile(
        name=name,
        email=email,
        phone=phone,
        bio=bio,
        location=location,
        language=language,
        skills=skills,
        password=password,
        avatar=avatar,
        current_user=current_user,
    )


@router.post("/update-profile")
async def update_profile_post_alias(
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    bio: str = Form(""),
    location: str = Form(""),
    language: str = Form("en"),
    skills: str = Form(""),
    password: str = Form(""),
    avatar: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    return await update_profile(
        name=name,
        email=email,
        phone=phone,
        bio=bio,
        location=location,
        language=language,
        skills=skills,
        password=password,
        avatar=avatar,
        current_user=current_user,
    )


class LanguageInput(BaseModel):
    language: str = Field(..., pattern="^(en|hi)$")


@router.put("/language")
async def update_language(data: LanguageInput, current_user=Depends(get_current_user)):
    db = get_db()
    updated_user = await db.users.find_one_and_update(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {"language": data.language}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "Language updated", "user": serialize_user(updated_user)}


@router.post("/report")
async def report_user(data: ReportUserInput, current_user=Depends(get_current_user)):
    db = get_db()
    if data.user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot report yourself")

    try:
        target_object_id = ObjectId(data.user_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc

    target_user = await db.users.find_one({"_id": target_object_id}, {"name": 1})
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    clean_reason = data.reason.strip()
    report_doc = {
        "type": "user",
        "reported_id": data.user_id,
        "reported_name": target_user.get("name", "User"),
        "reported_by": current_user["id"],
        "reported_by_name": current_user.get("name", "Student"),
        "reason": clean_reason,
        "created_at": datetime.utcnow(),
        "status": "open",
    }
    await db.reports.insert_one(report_doc)
    return {"message": "User reported successfully"}


@router.post("/block")
async def block_user(data: BlockUserInput, current_user=Depends(get_current_user)):
    db = get_db()
    if data.user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot block yourself")

    try:
        target_object_id = ObjectId(data.user_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc

    target_user = await db.users.find_one({"_id": target_object_id}, {"name": 1})
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.users.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$addToSet": {"blocked_user_ids": data.user_id}},
    )
    await db.follows.delete_many({"$or": [
        {"follower_id": current_user["id"], "following_id": data.user_id},
        {"follower_id": data.user_id, "following_id": current_user["id"]},
    ]})
    return {"message": f"{target_user.get('name', 'User')} has been blocked"}


def _public_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]), "uid": user.get("uid", ""),
        "name": user.get("name", ""), "username": user.get("username", ""),
        "bio": user.get("bio", ""), "avatar_url": user.get("avatar_url") or user.get("avatar", ""),
        "created_at": user.get("created_at").isoformat() if isinstance(user.get("created_at"), datetime) else user.get("created_at", ""),
    }


def _profile_target_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc


@search_router.get("/search-social")
async def search_social_users(q: str = Query(..., min_length=2, max_length=80), page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=50), current_user=Depends(get_current_user)):
    db = get_db()
    term = re.escape(q.strip().removeprefix("@"))
    if len(term) < 2:
        return {"users": [], "page": page, "limit": limit}
    query = {"$and": [
        {"_id": {"$ne": ObjectId(current_user["id"])}},
        {"$or": [
            {"uid": {"$regex": term, "$options": "i"}},
            {"name": {"$regex": term, "$options": "i"}},
            {"username": {"$regex": term, "$options": "i"}},
        ]},
        {"is_banned": {"$ne": True}},
    ]}
    cursor = db.users.find(query, {"uid": 1, "name": 1, "username": 1, "bio": 1, "avatar": 1, "avatar_url": 1, "created_at": 1}).sort("name", 1).skip((page - 1) * limit).limit(limit)
    return {"users": [_public_user(user) async for user in cursor], "page": page, "limit": limit}


@search_router.get("/{user_id}/profile")
async def public_profile(user_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    target_id = _profile_target_id(user_id)
    user = await db.users.find_one({"_id": target_id, "is_banned": {"$ne": True}}, {"uid": 1, "name": 1, "username": 1, "bio": 1, "avatar": 1, "avatar_url": 1, "created_at": 1, "blocked_user_ids": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    viewer, target = current_user["id"], str(target_id)
    blocked_by_viewer = target in [str(x) for x in current_user.get("blocked_user_ids", [])]
    blocked_viewer = viewer in [str(x) for x in user.get("blocked_user_ids", [])]
    blocked = blocked_by_viewer or blocked_viewer
    following = await db.follows.find_one({"follower_id": viewer, "following_id": target}) is not None
    follows_viewer = await db.follows.find_one({"follower_id": target, "following_id": viewer}) is not None
    return {"profile": _public_user(user), "posts_count": await db.notes.count_documents({"uploaded_by": target, "status": "approved"}),
        "followers_count": await db.follows.count_documents({"following_id": target}), "following_count": await db.follows.count_documents({"follower_id": target}),
        "is_following": following, "follows_viewer": follows_viewer, "blocked": blocked, "blocked_by_viewer": blocked_by_viewer, "blocked_viewer": blocked_viewer, "is_self": viewer == target}


@router.post("/follow/{user_id}")
async def follow_user(user_id: str, current_user=Depends(get_current_user)):
    db = get_db(); target_id = _profile_target_id(user_id); viewer = current_user["id"]; target = str(target_id)
    if viewer == target: raise HTTPException(status_code=400, detail="You cannot follow yourself")
    user = await db.users.find_one({"_id": target_id, "is_banned": {"$ne": True}}, {"_id": 1, "blocked_user_ids": 1})
    if not user: raise HTTPException(status_code=404, detail="User not found")
    if target in [str(x) for x in current_user.get("blocked_user_ids", [])] or viewer in [str(x) for x in user.get("blocked_user_ids", [])]:
        raise HTTPException(status_code=403, detail="You cannot follow this user")
    relation = await db.follows.update_one({"follower_id": viewer, "following_id": target}, {"$setOnInsert": {"follower_id": viewer, "following_id": target, "created_at": datetime.utcnow()}}, upsert=True)
    if relation.upserted_id:
        notification_user = await db.users.find_one({"_id": target_id}, {"notification_preferences.follows": 1}) or {}
        if (notification_user.get("notification_preferences") or {}).get("follows", True):
            follower_name = current_user.get("name", "Someone")
            await db.notifications.insert_one({"user_id": target, "sender_id": viewer, "sender_avatar": current_user.get("avatar_url") or current_user.get("avatar", ""), "type": "follow", "title": "New follower", "message": f"{follower_name} started following you", "reference_id": viewer, "read": False, "created_at": datetime.utcnow()})
            await send_push_notification(db, target, "New follower", f"{follower_name} started following you", {"type": "follow", "userId": viewer})
    return {"following": True, "followers_count": await db.follows.count_documents({"following_id": target})}


@router.delete("/follow/{user_id}")
async def unfollow_user(user_id: str, current_user=Depends(get_current_user)):
    target = str(_profile_target_id(user_id)); viewer = current_user["id"]
    await get_db().follows.delete_one({"follower_id": viewer, "following_id": target})
    return {"following": False, "followers_count": await get_db().follows.count_documents({"following_id": target})}


@router.delete("/block/{user_id}")
async def unblock_user(user_id: str, current_user=Depends(get_current_user)):
    target = str(_profile_target_id(user_id))
    await get_db().users.update_one({"_id": ObjectId(current_user["id"])}, {"$pull": {"blocked_user_ids": target}})
    return {"blocked": False}


@search_router.get("/{user_id}/relationships/{kind}")
async def profile_relationships(user_id: str, kind: str, page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=50), _current_user=Depends(get_current_user)):
    if kind not in {"followers", "following"}:
        raise HTTPException(status_code=404, detail="Relationship list not found")
    target = str(_profile_target_id(user_id)); db = get_db()
    key, user_key = ("following_id", "follower_id") if kind == "followers" else ("follower_id", "following_id")
    cursor = db.follows.find({key: target}).sort("created_at", -1).skip((page - 1) * limit).limit(limit)
    ids = [row.get(user_key) async for row in cursor]
    object_ids = []
    for value in ids:
        try: object_ids.append(ObjectId(value))
        except Exception: pass
    users = [_public_user(item) async for item in db.users.find({"_id": {"$in": object_ids}, "is_banned": {"$ne": True}}, {"uid": 1, "name": 1, "username": 1, "bio": 1, "avatar": 1, "avatar_url": 1, "created_at": 1})]
    return {"users": users, "page": page, "limit": limit}


@search_router.get("/{user_id}/posts")
async def public_user_posts(user_id: str, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=50), _current_user=Depends(get_current_user)):
    target = str(_profile_target_id(user_id)); db = get_db()
    cursor = db.notes.find({"uploaded_by": target, "status": "approved"}).sort("created_at", -1).skip((page - 1) * limit).limit(limit)
    posts = []
    async for note in cursor:
        posts.append({"id": str(note["_id"]), "title": note.get("title", ""), "subject": note.get("subject", ""), "thumbnail_url": note.get("thumbnail_url", ""), "created_at": note.get("created_at").isoformat() if isinstance(note.get("created_at"), datetime) else note.get("created_at", "")})
    return {"posts": posts, "page": page, "limit": limit}
