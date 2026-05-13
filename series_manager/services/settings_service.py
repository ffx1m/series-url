from series_manager.db import get_db


def load_config():
    db = get_db()
    if db is None:
        return {}
    try:
        config = db.settings.find_one({"type": "config"})
        if not config:
            return {}
        config.pop("_id", None)
        config.pop("type", None)
        return config
    except Exception as exc:
        print(f"Error loading config from MongoDB: {exc}")
        return {}


def save_config(config_data):
    db = get_db()
    if db is None:
        return False
    try:
        db.settings.update_one({"type": "config"}, {"$set": config_data}, upsert=True)
        return True
    except Exception as exc:
        print(f"Error saving config to MongoDB: {exc}")
        return False
