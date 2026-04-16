import os
from motor.motor_asyncio import AsyncIOMotorClient

_client = None
_db = None

def get_db():
    global _client, _db
    if _db is None:
        mongo_url = os.environ["MONGO_URL"]
        db_name = os.environ["DB_NAME"]
        _client = AsyncIOMotorClient(mongo_url)
        _db = _client[db_name]
    return _db

def get_client():
    global _client
    if _client is None:
        get_db()
    return _client
