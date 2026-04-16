import logging
import os
from pathlib import Path

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from openai import APIConnectionError, APIError, AuthenticationError, NotFoundError, OpenAI, RateLimitError
from pydantic import BaseModel

from utils.auth_utils import get_current_user
from utils.db import get_db

# Load backend/.env explicitly so keys are available even if app startup does not do it.
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger(__name__)

class ChatMessageInput(BaseModel):
    message: str
    conversation_id: str = ""


def get_ai_client_and_model():
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if openrouter_api_key:
        logger.info("AI provider selected: OpenRouter")
        return (
            OpenAI(
                api_key=openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
            ),
            "meta-llama/llama-3-8b-instruct",
            "openrouter",
        )

    if openai_api_key:
        logger.info("AI provider selected: OpenAI")
        return (
            OpenAI(api_key=openai_api_key),
            "gpt-4o-mini",
            "openai",
        )

    logger.error("No AI API key configured. Checked OPENROUTER_API_KEY and OPENAI_API_KEY.")
    raise HTTPException(
        status_code=500,
        detail="No AI API key configured. Set OPENROUTER_API_KEY or OPENAI_API_KEY.",
    )


@router.post("/chat")
async def ai_chat(data: ChatMessageInput, current_user=Depends(get_current_user)):
    if not data.message or not data.message.strip():
        raise HTTPException(status_code=400, detail="Message is required")

    user_message = data.message.strip()

    try:
        client, model, provider = get_ai_client_and_model()
        logger.info(
            "Generating AI response using provider=%s model=%s conversation_id=%s",
            provider,
            model,
            data.conversation_id or "new",
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful study assistant. Answer the user's question clearly and directly. "
                        "Do not repeat or paraphrase the user's message as the full reply. "
                        "If the question is unclear, ask a brief clarifying question."
                    ),
                },
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
        )

        answer = (response.choices[0].message.content or "").strip()

        if not answer or answer == user_message:
            logger.warning(
                "AI returned empty or repeated content. provider=%s model=%s message=%s",
                provider,
                model,
                user_message,
            )
            raise HTTPException(
                status_code=502,
                detail="AI returned an empty or invalid response. Please try again.",
            )

        logger.info("AI response generated successfully. provider=%s model=%s", provider, model)

        return {"reply": answer}

    except AuthenticationError as e:
        logger.exception("AI authentication failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid API key for AI provider.")
    except NotFoundError as e:
        logger.exception("AI model or endpoint not found: %s", e)
        raise HTTPException(status_code=404, detail="AI model or endpoint not found.")
    except RateLimitError as e:
        logger.exception("AI rate limit or quota exceeded: %s", e)
        raise HTTPException(status_code=429, detail="AI quota exceeded or rate limit reached.")
    except APIConnectionError as e:
        logger.exception("AI connection error: %s", e)
        raise HTTPException(status_code=502, detail="Unable to connect to AI provider.")
    except APIError as e:
        status_code = getattr(e, "status_code", None)
        logger.exception("AI API error (status=%s): %s", status_code, e)

        if status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid API key for AI provider.")
        if status_code == 404:
            raise HTTPException(status_code=404, detail="AI model or endpoint not found.")
        if status_code == 429:
            raise HTTPException(status_code=429, detail="AI quota exceeded or rate limit reached.")

        raise HTTPException(status_code=502, detail="AI provider returned an error.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected AI error: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error while generating AI response.")


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
