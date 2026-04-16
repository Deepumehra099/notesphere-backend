from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from bson import ObjectId
from utils.auth_utils import get_current_user
from utils.db import get_db
import os
import uuid
from openai import OpenAI

router = APIRouter(prefix="/api/ai", tags=["ai"])

api_key = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None

class ChatMessageInput(BaseModel):
    message: str
    conversation_id: str = ""

@router.post("/chat")
async def ai_chat(data: ChatMessageInput, current_user=Depends(get_current_user)):
    if client is None:
        return {
            "response": f"Study helper: {data.message.strip() or 'Please ask a question.'}",
            "conversation_id": data.conversation_id or str(uuid.uuid4())
        }

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful study assistant."},
                {"role": "user", "content": data.message}
            ]
        )

        answer = response.choices[0].message.content

        return {
            "response": answer,
            "conversation_id": data.conversation_id or str(uuid.uuid4())
        }

    except Exception:
        return {
            "response": f"I could not reach the AI service right now. Here is a simple answer starter: {data.message.strip()}",
            "conversation_id": data.conversation_id or str(uuid.uuid4())
        }


class SummarizeInput(BaseModel):
    note_id: str


@router.post("/summarize")
async def summarize_note(data: SummarizeInput, current_user=Depends(get_current_user)):
    db = get_db()
    note = await db.notes.find_one({"_id": ObjectId(data.note_id)})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    title = note.get("title", "Untitled note")
    subject = note.get("subject", "General")
    description = (note.get("description") or "").strip()
    topic = (note.get("topic") or "").strip()

    summary = f"{title} is a {subject} note"
    if topic:
        summary += f" focused on {topic}"
    if description:
        summary += f". Summary: {description}"
    else:
        summary += ". Open the note to review the main concepts and examples."

    return {"summary": summary}
