from pathlib import Path
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.ai import router as ai_router
from routes.auth import router as auth_router
from routes.chat import router as chat_router
from routes.notes import router as notes_router
from routes.tokens import router as tokens_router

load_dotenv()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


load_env_file(Path(__file__).resolve().parent / ".env")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(notes_router)
app.include_router(ai_router)
app.include_router(chat_router)
app.include_router(tokens_router)


@app.get("/")
def home():
    return {"message": "Backend working 🚀"}


@app.get("/api")
def api():
    return {"status": "ok"}


@app.get("/api/health")
def health():
    return {"status": "healthy"}
