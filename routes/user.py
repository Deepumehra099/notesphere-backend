import logging
import os
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
        {"name": 1, "email": 1, "avatar_url": 1, "uid": 1},
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "user": {
            "id": str(user["_id"]),
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "avatar_url": user.get("avatar_url", ""),
            "uid": user.get("uid", ""),
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
    return {"message": f"{target_user.get('name', 'User')} has been blocked"}
