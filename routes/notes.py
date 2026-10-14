import logging
import os
import re
from io import BytesIO
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import cloudinary
import cloudinary.uploader
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pymongo import ReturnDocument

from utils.auth_utils import get_current_user
from utils.db import get_client, get_db
from utils.credits import InsufficientCredits, apply_credit_change, qualify_referral

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

router = APIRouter(prefix="/api/notes", tags=["notes"])
logger = logging.getLogger(__name__)
UPLOADS_ROOT = Path(__file__).resolve().parents[1] / "uploads"
NOTES_UPLOADS_DIR = UPLOADS_ROOT / "notes"
THUMBNAILS_UPLOADS_DIR = UPLOADS_ROOT / "note-thumbnails"
NOTES_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


async def proxy_cloudinary_pdf(url: str, headers: dict[str, str]):
    import httpx

    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(180.0, connect=30.0),
    )
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("Cloudinary PDF request failed. error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not reach PDF storage. Please retry.") from exc

    if upstream.status_code != 200:
        status = upstream.status_code
        provider_error = upstream.headers.get("X-Cld-Error", "")
        await upstream.aclose()
        await client.aclose()
        logger.warning("Cloudinary rejected PDF delivery. upstream_status=%s provider_error=%s", status, provider_error[:300])
        if status == 401 and any(marker in provider_error.lower() for marker in ("acl deny", "pdf", "blocked", "not allowed")):
            raise HTTPException(
                status_code=502,
                detail="Cloudinary is blocking PDF delivery for this account. Enable PDF delivery in Cloudinary Security settings, or re-upload the PDF after local PDF storage is enabled.",
            )
        raise HTTPException(status_code=502, detail=f"PDF storage returned HTTP {status}.")

    response_headers = dict(headers)
    content_length = upstream.headers.get("content-length")
    if content_length and content_length.isdigit():
        response_headers["Content-Length"] = content_length

    async def stream_pdf():
        try:
            async for chunk in upstream.aiter_raw(128 * 1024):
                yield chunk
        except httpx.HTTPError as exc:
            logger.warning("Cloudinary PDF stream ended early. error_type=%s", type(exc).__name__)
            raise
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(stream_pdf(), media_type="application/pdf", headers=response_headers)

def configure_cloudinary() -> bool:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()

    if api_key.startswith("cloudinary://"):
        logger.warning("Invalid Cloudinary configuration detected, falling back to local uploads")
        return False

    if not cloud_name or not api_key or not api_secret:
        logger.warning("Cloudinary config missing, falling back to local uploads")
        return False

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    return True

async def save_upload_locally(upload: UploadFile, directory: Path, prefix: str) -> str:
    original_name = upload.filename or f"{prefix}.bin"
    suffix = Path(original_name).suffix or ""
    safe_name = f"{prefix}_{uuid4().hex}{suffix}"
    destination = directory / safe_name
    upload.file.seek(0)
    with destination.open("wb") as output:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    return f"/uploads/{directory.name}/{safe_name}"


def make_pdf_preview(upload: UploadFile) -> bytes:
    import fitz

    upload.file.seek(0)
    pdf_bytes = upload.file.read()
    upload.file.seek(0)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if not document.page_count:
            raise ValueError("PDF has no pages")
        page = document.load_page(0)
        return page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False).tobytes("png")


def save_preview_locally(preview: bytes, prefix: str) -> str:
    filename = f"{prefix}_{uuid4().hex}.png"
    (THUMBNAILS_UPLOADS_DIR / filename).write_bytes(preview)
    return f"/uploads/{THUMBNAILS_UPLOADS_DIR.name}/{filename}"

def serialize_note(note, viewer_id: str | None = None):
    n = {**note}
    n["id"] = str(n.pop("_id"))
    price = n.get("unlock_cost", 0) or 0
    n["price"] = price
    n["access_type"] = "paid" if price > 0 else "free"
    unlocked_by = {str(uid) for uid in n.get("unlocked_by", [])}
    has_access = (
        price <= 0
        or str(n.get("uploaded_by", "")) == str(viewer_id or "")
        or str(viewer_id or "") in unlocked_by
    )
    # Content is always delivered through the authenticated /content route.
    # Never serialize a storage URL (including after a premium unlock).
    n["file_url"] = ""
    n["pdf_url"] = ""
    if "unlocked_by" in n:
        n["unlocked_by"] = [str(uid) for uid in n.get("unlocked_by", [])]
    return n

@router.get("/feed")
async def get_notes_feed(
    page: int = 1,
    limit: int = 20,
    subject: Optional[str] = None,
    sort: str = "newest",
    current_user=Depends(get_current_user)
):
    db = get_db()
    query = {"status": "approved"}
    if subject:
        query["subject"] = {"$regex": subject, "$options": "i"}
    if sort == "paid":
        query["unlock_cost"] = {"$gt": 0}
    sort_field = {"newest": ("created_at", -1), "popular": ("views", -1), "rating": ("rating", -1), "paid": ("created_at", -1)}
    s = sort_field.get(sort, ("created_at", -1))
    skip = (page - 1) * limit
    cursor = db.notes.find(query).sort(s[0], s[1]).skip(skip).limit(limit)
    notes = []
    async for note in cursor:
        n = serialize_note(note, current_user["id"])
        n["is_unlocked"] = (
            n.get("price", 0) <= 0
            or current_user["id"] in n.get("unlocked_by", [])
            or n.get("uploaded_by") == current_user["id"]
        )
        notes.append(n)
    total = await db.notes.count_documents(query)
    return {"notes": notes, "total": total, "page": page, "pages": (total + limit - 1) // limit}


@router.get("")
async def get_notes_feed_compat(
    access: str = "all",
    type: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    subject: Optional[str] = None,
    current_user=Depends(get_current_user),
):
    db = get_db()
    query = {"status": "approved"}
    if subject:
        query["subject"] = {"$regex": subject, "$options": "i"}

    normalized_access = (type or access or "all").strip().lower()
    if normalized_access == "free":
        query["unlock_cost"] = {"$lte": 0}
    elif normalized_access == "paid":
        query["unlock_cost"] = {"$gt": 0}

    skip = (max(page, 1) - 1) * limit
    cursor = db.notes.find(query).sort("created_at", -1).skip(skip).limit(limit)
    notes = []
    async for note in cursor:
        n = serialize_note(note, current_user["id"])
        n["is_unlocked"] = (
            n.get("price", 0) <= 0
            or current_user["id"] in n.get("unlocked_by", [])
            or n.get("uploaded_by") == current_user["id"]
        )
        notes.append(n)

    total = await db.notes.count_documents(query)
    return {"notes": notes, "total": total, "page": max(page, 1), "pages": (total + limit - 1) // limit}

@router.get("/my")
@router.get("/mine")
async def get_my_notes(current_user=Depends(get_current_user)):
    db = get_db()
    cursor = db.notes.find({"uploaded_by": current_user["id"]}).sort("created_at", -1)
    notes = []
    async for note in cursor:
        notes.append(serialize_note(note, current_user["id"]))
    return {"notes": notes}


@router.get("/purchased")
async def get_purchased_notes(current_user=Depends(get_current_user)):
    db = get_db()
    cursor = db.notes.find({"status": "approved", "unlock_cost": {"$gt": 0}, "unlocked_by": current_user["id"]}).sort("created_at", -1).limit(100)
    notes = []
    async for note in cursor:
        item = serialize_note(note, current_user["id"])
        item["is_unlocked"] = True
        notes.append(item)
    return {"notes": notes}

@router.get("/{note_id}")
async def get_note(note_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    try:
        note_object_id = ObjectId(note_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    note = await db.notes.find_one({"_id": note_object_id})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.get("status") != "approved" and note.get("uploaded_by") != current_user["id"] and current_user.get("role") != "admin":
        raise HTTPException(status_code=404, detail="Note not found")
    try:
        await db.note_views.insert_one({"note_id": note_id, "user_id": current_user["id"], "first_viewed_at": datetime.now(timezone.utc)})
        await db.notes.update_one({"_id": note_object_id}, {"$inc": {"views": 1}})
        note["views"] = int(note.get("views", 0) or 0) + 1
    except Exception as exc:
        from pymongo.errors import DuplicateKeyError
        if not isinstance(exc, DuplicateKeyError):
            raise
    n = serialize_note(note, current_user["id"])
    n["is_unlocked"] = (
        n.get("price", 0) <= 0
        or current_user["id"] in n.get("unlocked_by", [])
        or n.get("uploaded_by") == current_user["id"]
    )
    return {"note": n}


@router.get("/{note_id}/content")
async def get_note_content(note_id: str, current_user=Depends(get_current_user)):
    try:
        note_object_id = ObjectId(note_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    note = await get_db().notes.find_one({"_id": note_object_id})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    user_id = current_user["id"]
    is_admin = bool(current_user.get("is_admin") or current_user.get("role") == "admin")
    if note.get("status") != "approved" and note.get("uploaded_by") != user_id and not is_admin:
        raise HTTPException(status_code=404, detail="Note not found")
    allowed = (int(note.get("unlock_cost", 0) or 0) <= 0 or note.get("uploaded_by") == user_id or user_id in [str(value) for value in note.get("unlocked_by", [])] or is_admin)
    if not allowed:
        raise HTTPException(status_code=403, detail="Unlock this note before reading it")
    file_url = str(note.get("file_url") or note.get("pdf_url") or "")
    parsed = urlparse(file_url)
    if note.get("cloudinary_public_id") or (parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".cloudinary.com")):
        # Cloudinary's SDK config is process-local. Uploads configure it in the
        # upload handler, but a restarted API process must configure it again
        # before signing a download URL. Older note rows only contain the
        # delivery URL, so reconstruct the public ID and sign those as well.
        if not configure_cloudinary():
            raise HTTPException(status_code=503, detail="PDF storage is not configured on the server")
        from cloudinary.utils import private_download_url
        if note.get("cloudinary_public_id"):
            public_id = note["cloudinary_public_id"]
            resource_type = note.get("cloudinary_resource_type") or "raw"
            file_format = note.get("cloudinary_format") or ("" if resource_type == "raw" else "pdf")
            delivery_type = "authenticated"
        else:
            parts = parsed.path.strip("/").split("/")
            if len(parts) < 5 or parts[1] not in {"image", "video", "raw"} or parts[2] not in {"upload", "private", "authenticated"}:
                raise HTTPException(status_code=404, detail="PDF storage URL is invalid")
            resource_type, delivery_type = parts[1], parts[2]
            asset_parts = parts[3:]
            if asset_parts and re.fullmatch(r"v\d+", asset_parts[0]):
                asset_parts = asset_parts[1:]
            if not asset_parts:
                raise HTTPException(status_code=404, detail="PDF storage URL is invalid")
            file_format = ""
            if resource_type != "raw" and "." in asset_parts[-1]:
                asset_parts[-1], file_format = asset_parts[-1].rsplit(".", 1)
            public_id = "/".join(asset_parts)
        file_url = private_download_url(
            public_id, file_format,
            resource_type=resource_type,
            type=delivery_type,
            expires_at=int(datetime.now(timezone.utc).timestamp()) + 300,
        )
    filename = Path(str(note.get("file_name") or "note.pdf")).name.replace('"', "")
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": f'inline; filename="{filename}"'}
    local_path = urlparse(file_url).path
    if local_path.startswith("/api/uploads/"):
        local_path = local_path.removeprefix("/api")
    if local_path.startswith("/uploads/"):
        target = (UPLOADS_ROOT / local_path.removeprefix("/uploads/")).resolve()
        if not target.is_relative_to(UPLOADS_ROOT.resolve()) or not target.is_file():
            logger.warning("Local note PDF is missing. note_id=%s path=%s", note_id, local_path)
            raise HTTPException(status_code=404, detail="PDF is unavailable")
        return FileResponse(target, media_type="application/pdf", headers=headers, filename=None)
    parsed = urlparse(file_url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".cloudinary.com"):
        raise HTTPException(status_code=404, detail="PDF is unavailable")
    # Keep the signed storage URL server-side so storage auth errors cannot be
    # mistaken for an expired Notesphere login by the mobile client.
    return await proxy_cloudinary_pdf(file_url, headers)

@router.post("/upload")
async def upload_note(
    title: str = Form(...),
    description: str = Form(""),
    subject: str = Form(...),
    topic: str = Form(""),
    tags: str = Form(""),
    unlock_cost: Optional[str] = Form(None),
    price: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    pdf: Optional[UploadFile] = File(None),
    thumbnail: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    clean_title = (title or "").strip()
    clean_subject = (subject or "").strip()
    clean_topic = (topic or "").strip()

    if not clean_title:
        raise HTTPException(status_code=400, detail="Title is required")
    if not clean_subject:
        raise HTTPException(status_code=400, detail="Subject is required")

    actual_file = pdf or file
    if actual_file is None or not actual_file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if not actual_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    raw_price = price if price not in (None, "") else unlock_cost
    if raw_price in (None, ""):
        raw_price = 0
    try:
        final_price = int(raw_price or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Price must be a whole number of Credits")
    if final_price < 0:
        raise HTTPException(status_code=400, detail="Price cannot be negative")
    thumbnail_url = ""
    result = {}
    cloudinary_ready = False
    cloudinary_pdf_ready = False
    preview = None
    if thumbnail is None or not thumbnail.filename:
        try:
            preview = make_pdf_preview(actual_file)
        except Exception as exc:
            logger.warning("Could not generate PDF preview. filename=%s error_type=%s", actual_file.filename, type(exc).__name__)

    public_id = f"notesphere/notes/{current_user['id']}_{int(datetime.now().timestamp())}"

    try:
        cloudinary_ready = configure_cloudinary()
        pdf_storage = os.getenv("NOTES_PDF_STORAGE", "cloudinary").strip().lower()
        cloudinary_pdf_ready = cloudinary_ready and pdf_storage == "cloudinary"
        if cloudinary_pdf_ready:
            actual_file.file.seek(0)
            result = cloudinary.uploader.upload(
                actual_file.file,
                resource_type="auto",
                type="authenticated",
                public_id=public_id,
                overwrite=True,
            )
            secure_url = result.get("secure_url")
            if not secure_url:
                raise RuntimeError("Cloudinary did not return secure_url")
            logger.info("Cloudinary upload success. user_id=%s filename=%s", current_user["id"], actual_file.filename)
        else:
            secure_url = await save_upload_locally(actual_file, NOTES_UPLOADS_DIR, f"note_{current_user['id']}")
            logger.info("Note PDF stored locally. user_id=%s", current_user["id"])
    except Exception as e:
        logger.warning("Primary note upload failed; using local storage fallback. error_type=%s", type(e).__name__)
        result = {}
        cloudinary_pdf_ready = False
        try:
            secure_url = await save_upload_locally(actual_file, NOTES_UPLOADS_DIR, f"note_{current_user['id']}")
        except Exception as local_error:
            logger.error("Local note upload fallback failed. error_type=%s", type(local_error).__name__)
            raise HTTPException(status_code=500, detail="File upload failed")

    if thumbnail is not None and thumbnail.filename:
        if cloudinary_ready:
            try:
                thumbnail.file.seek(0)
                thumbnail_result = cloudinary.uploader.upload(
                    thumbnail.file,
                    resource_type="image",
                    folder="notesphere/note-thumbnails",
                )
                thumbnail_url = thumbnail_result.get("secure_url", "").strip()
            except Exception as exc:
                logger.warning("Thumbnail Cloudinary upload failed; using local storage. error_type=%s", type(exc).__name__)
        if not thumbnail_url:
            thumbnail_url = await save_upload_locally(thumbnail, THUMBNAILS_UPLOADS_DIR, f"thumb_{current_user['id']}")
    elif preview:
        if cloudinary_ready:
            try:
                thumbnail_result = cloudinary.uploader.upload(
                    BytesIO(preview),
                    resource_type="image",
                    folder="notesphere/note-thumbnails",
                )
                thumbnail_url = thumbnail_result.get("secure_url", "").strip()
            except Exception as exc:
                logger.warning("PDF preview Cloudinary upload failed; using local storage. error_type=%s", type(exc).__name__)
        if not thumbnail_url:
            thumbnail_url = save_preview_locally(preview, f"thumb_{current_user['id']}")
    await actual_file.close()
    if thumbnail is not None:
        await thumbnail.close()

    db = get_db()
    note_doc = {
        "title": clean_title,
        "description": description.strip(),
        "subject": clean_subject,
        "topic": clean_topic,
        "unlock_cost": final_price,
        "price": final_price,
        "status": "pending",
        "uploaded_by": current_user["id"],
        "uploader_name": current_user.get("name", "Student"),
        "file_url": secure_url,
        "cloudinary_public_id": result.get("public_id") if cloudinary_pdf_ready else None,
        "cloudinary_resource_type": result.get("resource_type") if cloudinary_pdf_ready else None,
        "cloudinary_format": result.get("format") if cloudinary_pdf_ready else None,
        "file_name": actual_file.filename,
        "thumbnail_url": thumbnail_url,
        "tags": [item.strip() for item in tags.split(",") if item.strip()],
        "views": 0,
        "downloads": 0,
        "rating": 0,
        "rating_count": 0,
        "unlocked_by": [],
        "created_at": datetime.now(timezone.utc),
        "approved_at": None,
        "rejected_at": None,
    }
    result = await db.notes.insert_one(note_doc)
    note_doc["_id"] = result.inserted_id

    return {
        "message": "Upload successful",
        "note": serialize_note(note_doc, current_user["id"]),
    }


@router.post("/upload-note")
async def upload_note_alias(
    title: str = Form(...),
    description: str = Form(""),
    subject: str = Form(...),
    topic: str = Form(""),
    tags: str = Form(""),
    unlock_cost: Optional[str] = Form("0"),
    price: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    pdf: Optional[UploadFile] = File(None),
    thumbnail: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
):
    return await upload_note(
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

@router.post("/{note_id}/unlock")
async def unlock_note(note_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    try:
        note_object_id = ObjectId(note_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    note = await db.notes.find_one({"_id": note_object_id})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.get("uploaded_by") == current_user["id"]:
        return {"message": "You own this note"}
    unlocked_by = [str(uid) for uid in note.get("unlocked_by", [])]
    if current_user["id"] in unlocked_by:
        return {"message": "Already unlocked", "alreadyUnlocked": True}
    if note.get("status") != "approved":
        raise HTTPException(status_code=409, detail="This note is not available for purchase yet")
    cost = max(0, int(note.get("unlock_cost", 0) or 0))
    if cost <= 0:
        return {"message": "Free note already accessible", "alreadyUnlocked": True}

    client = get_client()
    try:
        async with await client.start_session() as session:
            async with session.start_transaction():
                updated_note = await db.notes.find_one_and_update(
                    {"_id": note_object_id, "status": "approved", "unlocked_by": {"$ne": current_user["id"]}},
                    {"$addToSet": {"unlocked_by": current_user["id"]}, "$inc": {"credit_purchase_sequence": 1}},
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if not updated_note:
                    raise HTTPException(status_code=409, detail="Note is already unlocked or unavailable")
                sequence = int(updated_note.get("credit_purchase_sequence", 1) or 1)
                result = await apply_credit_change(
                    db,
                    user_id=current_user["id"],
                    amount=-cost,
                    transaction_type="CREDIT_SPEND",
                    description=f"Premium note unlock: {note.get('title', 'Note')}",
                    idempotency_key=f"note-purchase:{current_user['id']}:{note_id}:{sequence}",
                    reference_id=note_id,
                    metadata={"note_title": note.get("title", ""), "purchase_sequence": sequence, "price_credits": cost},
                    session=session,
                )
                await qualify_referral(db, current_user["id"], session=session)
    except InsufficientCredits as exc:
        raise HTTPException(status_code=400, detail="Not enough Tokens") from exc
    return {"message": "Note unlocked successfully", "tokens_spent": cost, "tokens_balance": result["balance"]}


@router.post("/buy")
async def buy_note(payload: dict, current_user=Depends(get_current_user)):
    note_id = str(payload.get("noteId") or payload.get("note_id") or "").strip()
    if not note_id:
        raise HTTPException(status_code=400, detail="note_id is required")
    return await unlock_note(note_id, current_user)

@router.post("/{note_id}/rate")
async def rate_note(note_id: str, rating: float, current_user=Depends(get_current_user)):
    db = get_db()
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be 1-5")
    note = await db.notes.find_one({"_id": ObjectId(note_id)})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    new_count = note.get("rating_count", 0) + 1
    new_rating = ((note.get("rating", 0) * note.get("rating_count", 0)) + rating) / new_count
    await db.notes.update_one(
        {"_id": ObjectId(note_id)},
        {"$set": {"rating": round(new_rating, 1), "rating_count": new_count}}
    )
    return {"message": "Rated successfully", "new_rating": round(new_rating, 1)}

@router.post("/{note_id}/download")
async def download_note(note_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    try:
        note_object_id = ObjectId(note_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    note = await db.notes.find_one({"_id": note_object_id})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    is_unlocked = (
        (note.get("unlock_cost", 0) or 0) <= 0
        or current_user["id"] in [str(uid) for uid in note.get("unlocked_by", [])]
        or note.get("uploaded_by") == current_user["id"]
    )
    if not is_unlocked:
        raise HTTPException(status_code=403, detail="Note not unlocked")
    if note.get("status") != "approved" and note.get("uploaded_by") != current_user["id"]:
        raise HTTPException(status_code=404, detail="Note not found")
    if not note.get("file_url") and not note.get("pdf_url") and not note.get("cloudinary_public_id"):
        raise HTTPException(status_code=404, detail="PDF is unavailable")
    return {"file_url": f"/api/notes/{note_id}/content", "file_name": note.get("file_name") or "note.pdf"}


@router.post("/{note_id}/download/complete")
async def complete_note_download(note_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    try:
        note_object_id = ObjectId(note_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    note = await db.notes.find_one({"_id": note_object_id})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    is_unlocked = (int(note.get("unlock_cost", 0) or 0) <= 0 or current_user["id"] in [str(uid) for uid in note.get("unlocked_by", [])] or note.get("uploaded_by") == current_user["id"] or current_user.get("role") == "admin")
    if not is_unlocked:
        raise HTTPException(status_code=403, detail="Note not unlocked")
    try:
        await db.note_downloads.insert_one({"note_id": note_id, "user_id": current_user["id"], "downloaded_at": datetime.now(timezone.utc)})
        await db.notes.update_one({"_id": note_object_id}, {"$inc": {"downloads": 1}})
        await db.users.update_one({"_id": ObjectId(current_user["id"])}, {"$inc": {"xp": 10}})
    except Exception as exc:
        from pymongo.errors import DuplicateKeyError
        if not isinstance(exc, DuplicateKeyError):
            raise
    return {"message": "Offline download recorded"}
