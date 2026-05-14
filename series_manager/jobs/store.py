import os
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from series_manager.db import get_db


TERMINAL_STATUSES = {"completed", "error", "canceled"}
RETRYABLE_TYPES = {"video", "image"}


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
        "updated_at": now_utc(),
        "heartbeat_at": None,
        "started_at": None,
        "finished_at": None,
        "retry_count": 0,
        "recovered_count": 0,
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
        {
            "$set": {
                "status": "processing",
                "stage": "starting",
                "started_at": now_utc(),
                "heartbeat_at": now_utc(),
                "updated_at": now_utc(),
                "message": "Starting",
            }
        },
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
    updates.setdefault("updated_at", now_utc())
    if updates.get("status") in TERMINAL_STATUSES:
        updates.setdefault("finished_at", now_utc())
    db.tasks.update_one({"task_id": task_id}, {"$set": updates})
    return get_job(task_id)


def heartbeat_job(task_id):
    db = get_db()
    if db is None:
        return False
    now = now_utc()
    result = db.tasks.update_one(
        {"task_id": task_id, "status": "processing"},
        {"$set": {"heartbeat_at": now, "updated_at": now}},
    )
    return result.matched_count > 0


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
    cursor = db.tasks.find({"status": {"$in": ["queued", "processing", "error", "completed", "canceled"]}}).sort("created_at", 1)
    for job in cursor:
        job.pop("_id", None)
        jobs.append(job)

    status_order = {"processing": 0, "queued": 1, "error": 2, "completed": 3, "canceled": 4}
    jobs.sort(key=lambda item: status_order.get(item.get("status"), 99))
    return jobs


def recover_stale_processing_jobs(stale_after_minutes=60, max_recoveries=2):
    db = get_db()
    if db is None:
        return 0

    cutoff = now_utc() - timedelta(minutes=stale_after_minutes)
    stale_filter = {
        "status": "processing",
        "type": {"$in": list(RETRYABLE_TYPES)},
        "$or": [
            {"heartbeat_at": {"$lt": cutoff}},
            {"heartbeat_at": None, "started_at": {"$lt": cutoff}},
            {"heartbeat_at": {"$exists": False}, "started_at": {"$lt": cutoff}},
        ],
    }

    recovered = 0
    for job in db.tasks.find(stale_filter):
        recovered_count = int(job.get("recovered_count", 0))
        task_id = job["task_id"]
        if job.get("type") == "image":
            input_path = job.get("input_image_path") or job.get("payload", {}).get("input_image_path")
            if not input_path or not os.path.exists(input_path):
                db.tasks.update_one(
                    {"task_id": task_id},
                    {
                        "$set": {
                            "status": "error",
                            "stage": "failed",
                            "message": "Image job was interrupted and the uploaded source file is no longer available",
                            "error": "missing_upload_after_recovery",
                            "finished_at": now_utc(),
                            "updated_at": now_utc(),
                        }
                    },
                )
                continue

        if recovered_count >= max_recoveries:
            db.tasks.update_one(
                {"task_id": task_id},
                {
                    "$set": {
                        "status": "error",
                        "stage": "failed",
                        "message": "Job stopped responding and exceeded recovery limit",
                        "error": "stale_worker",
                        "finished_at": now_utc(),
                        "updated_at": now_utc(),
                    }
                },
            )
            continue

        db.tasks.update_one(
            {"task_id": task_id},
            {
                "$set": {
                    "status": "queued",
                    "stage": "queued",
                    "message": "Recovered from interrupted worker and queued again",
                    "heartbeat_at": None,
                    "started_at": None,
                    "updated_at": now_utc(),
                    "cancel_requested": False,
                },
                "$inc": {"recovered_count": 1, "retry_count": 1},
                "$push": {
                    "logs": {
                        "$each": ["Recovered stale processing job after worker interruption"],
                        "$slice": -150,
                    }
                },
            },
        )
        recovered += 1
    return recovered


def cleanup_terminal_jobs(completed_hours=24, canceled_hours=24, error_days=7):
    db = get_db()
    if db is None:
        return 0

    now = now_utc()
    cleanup_filter = {
        "$or": [
            {"status": "completed", "finished_at": {"$lt": now - timedelta(hours=completed_hours)}},
            {"status": "canceled", "finished_at": {"$lt": now - timedelta(hours=canceled_hours)}},
            {"status": "error", "finished_at": {"$lt": now - timedelta(days=error_days)}},
        ]
    }
    result = db.tasks.delete_many(cleanup_filter)
    return result.deleted_count


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
