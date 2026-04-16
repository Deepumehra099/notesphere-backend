import logging
import os
from pathlib import Path
from typing import Optional

import cloudinary
import cloudinary.uploader
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pymongo import ReturnDocument

from routes.auth import serialize_user
from utils.auth_utils import get_current_user
from utils.db import get_db

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

router = APIRouter(prefix="/api/user", tags=["user"])
search_router = APIRouter(prefix="/api/users", tags=["user"])
logger = logging.getLogger(__name__)


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
    bio: str = Form(""),
    avatar: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    clean_name = name.strip()
    clean_bio = bio.strip()

    if not clean_name:
        raise HTTPException(status_code=400, detail="Name is required")

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
    updated_user = await db.users.find_one_and_update(
        {"_id": ObjectId(current_user["id"])},
        {
            "$set": {
                "name": clean_name,
                "bio": clean_bio,
                "avatar": avatar_url,
                "avatar_url": avatar_url,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")

    logger.info("Profile updated successfully for user_id=%s", current_user["id"])
    return {"user": serialize_user(updated_user)}
