from series_manager.jobs.store import (
    create_job,
    delete_job,
    get_job,
    list_jobs,
    request_cancel,
)
from series_manager.jobs.worker import terminate_task
from series_manager.services.validation import validate_http_url, validate_name

tasks = {}
running_processes = {}


def start_video_conversion(series_name, ep_name, m3u8_url):
    series_name = validate_name(series_name, "Series name")
    ep_name = validate_name(ep_name, "Episode name")
    m3u8_url = validate_http_url(m3u8_url)
    job = create_job(
        "video",
        f"[{series_name}] - {ep_name}",
        {"series_name": series_name, "ep_name": ep_name, "m3u8_url": m3u8_url},
    )
    return job["task_id"]


def start_image_conversion(series_name, input_image_path):
    series_name = validate_name(series_name, "Series name")
    job = create_job(
        "image",
        f"Image for {series_name}",
        {"series_name": series_name, "input_image_path": input_image_path},
    )
    return job["task_id"]


def get_task_status(task_id):
    return get_job(task_id)


def get_all_tasks():
    return list_jobs()


def cancel_task(task_id):
    request_cancel(task_id)
    return terminate_task(task_id)
