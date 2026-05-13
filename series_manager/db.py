import os
import time

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi

if os.environ.get("DISABLE_DOTENV", "false").lower() != "true":
    load_dotenv()

_client = None
db = None
_last_connect_attempt = 0
_connect_cooldown_seconds = 30


def connect_mongodb(uri=None):
    global _client, db, _last_connect_attempt

    if db is not None:
        return True

    now = time.monotonic()
    if _last_connect_attempt and now - _last_connect_attempt < _connect_cooldown_seconds:
        return False
    _last_connect_attempt = now

    uri = uri or os.environ.get("MONGODB_URI")
    if not uri:
        db = None
        return False

    try:
        _client = MongoClient(
            uri,
            server_api=ServerApi("1"),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
        )
        _client.admin.command("ping")
        db = _client.get_database(os.environ.get("MONGODB_DB", "url_series"))
        ensure_indexes()
        print("Successfully connected to MongoDB")
        return True
    except Exception as exc:
        print(f"MongoDB Connection Error: {exc}")
        _client = None
        db = None
        return False


def get_db():
    if db is None:
        connect_mongodb()
    return db


def ensure_indexes():
    if db is None:
        return
    db.tasks.create_index("task_id", unique=True)
    db.tasks.create_index([("status", 1), ("type", 1), ("created_at", 1)])
    db.settings.create_index("type", unique=True)
