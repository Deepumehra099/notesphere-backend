import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_client = None
_db = None

def _load_backend_env():
    load_dotenv()
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
      load_dotenv(env_path, override=False)

def _normalize_mongo_url(raw_url: str) -> str:
    if "://" not in raw_url or "@" not in raw_url:
        return raw_url

    scheme, remainder = raw_url.split("://", 1)
    if "@" not in remainder:
        return raw_url

    credentials, host_part = remainder.rsplit("@", 1)
    if ":" not in credentials:
        return raw_url

    username, password = credentials.split(":", 1)
    normalized_username = quote(username, safe="")
    normalized_password = quote(password, safe="")
    return f"{scheme}://{normalized_username}:{normalized_password}@{host_part}"

def get_db():
    global _client, _db
    if _db is None:
        _load_backend_env()
        mongo_url = _normalize_mongo_url(os.environ["MONGO_URL"])
        db_name = os.environ["DB_NAME"]
        _client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
        _db = _client[db_name]
    return _db

def get_client():
    global _client
    if _client is None:
        get_db()
    return _client
