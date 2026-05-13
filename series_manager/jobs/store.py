import uuid
from datetime import datetime, timezone

from pymongo import ReturnDocument

from series_manager.db import get_db


TERMINAL_STATUSES = {"completed", "error", "canceled"}


def now_utc():
    return datetime.now(timezone.utc)


def create_job(job_type, name, payload):
    db = get_db()
    task_id = str(uuid.uuid4())
    job = {
        "task_id": task_id,
        "id": task_id,
        "type": job_type,
        "name": name,
        "status": "queued",
        "stage": "queued",
        "progress": "0%",
        "progress_value": 0,
        "message": "Queued",
        "logs": [],
        "payload": payload,
        "cancel_requested": False,
        "error": None,
        "result": {},
        "created_at": now_utc(),
        "started_at": None,
        "finished_at": None,
    }
    job.update(payload)

    if db is None:
        job["status"] = "error"
        job["message"] = "Cannot create job: MongoDB is not connected"
        return job

    db.tasks.insert_one(job)
    job.pop("_id", None)
    return job


def claim_next_job():
    db = get_db()
    if db is None:
        return None
    job = db.tasks.find_one_and_update(
        {"status": "queued", "cancel_requested": {"$ne": True}},
        {"$set": {"status": "processing", "stage": "starting", "started_at": now_utc(), "message": "Starting"}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if job:
        job.pop("_id", None)
    return job


def update_job(task_id, **updates):
    db = get_db()
    if db is None:
        return None
    if updates.get("status") in TERMINAL_STATUSES:
        updates.setdefault("finished_at", now_utc())
    db.tasks.update_one({"task_id": task_id}, {"$set": updates})
    return get_job(task_id)


def append_log(task_id, line, limit=150):
    db = get_db()
    if db is None or not line:
        return
    db.tasks.update_one(
        {"task_id": task_id},
        {
            "$push": {"logs": {"$each": [line], "$slice": -limit}},
            "$set": {"updated_at": now_utc()},
        },
    )


def replace_logs(task_id, logs):
    update_job(task_id, logs=logs[-150:])


def get_job(task_id):
    db = get_db()
    if db is None:
        return {"status": "not_found"}
    job = db.tasks.find_one({"task_id": task_id})
    if not job:
        return {"status": "not_found"}
    job.pop("_id", None)
    return job


def list_jobs():
    db = get_db()
    if db is None:
        return []
    jobs = []
    cursor = db.tasks.find({"status": {"$in": ["queued", "processing", "error", "completed"]}}).sort("created_at", 1)
    for job in cursor:
        job.pop("_id", None)
        jobs.append(job)

    status_order = {"processing": 0, "queued": 1, "error": 2, "completed": 3}
    jobs.sort(key=lambda item: status_order.get(item.get("status"), 99))
    return jobs


def request_cancel(task_id):
    db = get_db()
    if db is None:
        return False
    result = db.tasks.update_one(
        {"task_id": task_id},
        {"$set": {"cancel_requested": True, "message": "Cancellation requested"}},
    )
    return result.matched_count > 0


def delete_job(task_id):
    db = get_db()
    if db is None:
        return False
    db.tasks.delete_one({"task_id": task_id})
    return True


def should_cancel(task_id):
    job = get_job(task_id)
    return bool(job.get("cancel_requested")) or job.get("status") == "canceled"
