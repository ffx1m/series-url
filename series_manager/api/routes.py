import os
import shutil

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

from series_manager.db import get_db
from series_manager.jobs.store import create_job, delete_job, get_job, list_jobs, request_cancel
from series_manager.jobs.worker import terminate_task
from series_manager.services.r2_service import (
    create_series_folder,
    delete_ep_folder,
    delete_object,
    delete_series_folder,
    list_folder_contents,
    list_series_folders,
    public_domain,
)
from series_manager.services.validation import validate_http_url, validate_name

api_bp = Blueprint("api", __name__)


@api_bp.route("/db/status")
def db_status():
    return jsonify({"connected": get_db() is not None})


@api_bp.route("/series", methods=["GET"])
def get_series():
    return jsonify(list_series_folders())


@api_bp.route("/series", methods=["POST"])
def create_series():
    data = request.get_json(silent=True) or {}
    try:
        series_name = validate_name(data.get("name"), "Series name")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if create_series_folder(series_name):
        return jsonify({"message": "Created successfully"})
    return jsonify({"error": "Failed to create folder"}), 500


@api_bp.route("/series/<series_name>", methods=["GET"])
def series_contents(series_name):
    try:
        series_name = validate_name(series_name, "Series name")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"contents": list_folder_contents(series_name), "domain": public_domain()})


@api_bp.route("/convert/video", methods=["POST"])
def convert_video():
    data = request.get_json(silent=True) or {}
    try:
        series_name = validate_name(data.get("series_name"), "Series name")
        ep_name = validate_name(data.get("ep_name"), "Episode name")
        m3u8_url = validate_http_url(data.get("url"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job = create_job(
        "video",
        f"[{series_name}] - {ep_name}",
        {"series_name": series_name, "ep_name": ep_name, "m3u8_url": m3u8_url},
    )
    return jsonify({"task_id": job["task_id"], "status": job["status"], "message": job["message"]})


@api_bp.route("/convert/image", methods=["POST"])
def convert_image():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    upload = request.files["file"]
    try:
        series_name = validate_name(request.form.get("series_name"), "Series name")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if upload.filename == "":
        return jsonify({"error": "Missing file"}), 400

    filename = secure_filename(upload.filename)
    file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
    upload.save(file_path)

    job = create_job(
        "image",
        f"Image for {series_name}",
        {"series_name": series_name, "input_image_path": file_path},
    )
    return jsonify({"task_id": job["task_id"], "status": job["status"], "message": job["message"]})


@api_bp.route("/task/<task_id>", methods=["GET"])
def task_status(task_id):
    return jsonify(get_job(task_id))


@api_bp.route("/tasks", methods=["GET"])
def all_tasks():
    return jsonify(list_jobs())


@api_bp.route("/jobs", methods=["GET"])
def all_jobs():
    return jsonify(list_jobs())


@api_bp.route("/task/<task_id>/cancel", methods=["POST"])
def cancel_task(task_id):
    request_cancel(task_id)
    terminate_task(task_id)
    return jsonify({"message": "Task cancellation requested"})


@api_bp.route("/jobs/<task_id>", methods=["GET"])
def job_status(task_id):
    return jsonify(get_job(task_id))


@api_bp.route("/jobs/<task_id>/cancel", methods=["POST"])
def cancel_job(task_id):
    request_cancel(task_id)
    terminate_task(task_id)
    return jsonify({"message": "Job cancellation requested"})


@api_bp.route("/jobs/<task_id>", methods=["DELETE"])
def delete_job_api(task_id):
    terminate_task(task_id)
    tmp_dir = f"data/tmp_{task_id}"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    delete_job(task_id)
    return jsonify({"message": "Job stopped, cleaned up, and deleted"})


@api_bp.route("/task/<task_id>/delete", methods=["POST"])
def delete_task(task_id):
    terminate_task(task_id)
    tmp_dir = f"data/tmp_{task_id}"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    delete_job(task_id)
    return jsonify({"message": "Task stopped, cleaned up, and deleted"})


@api_bp.route("/jobs/video", methods=["POST"])
def create_video_job():
    return convert_video()


@api_bp.route("/jobs/image", methods=["POST"])
def create_image_job():
    return convert_image()


@api_bp.route("/delete/object", methods=["POST"])
def delete_item():
    data = request.get_json(silent=True) or {}
    if delete_object(data.get("key")):
        return jsonify({"message": "Deleted successfully"})
    return jsonify({"error": "Failed to delete"}), 500


@api_bp.route("/delete/ep", methods=["POST"])
def delete_ep():
    data = request.get_json(silent=True) or {}
    try:
        series_name = validate_name(data.get("series_name"), "Series name")
        ep_name = validate_name(data.get("ep_name"), "Episode name")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if delete_ep_folder(series_name, ep_name):
        return jsonify({"message": "Deleted successfully"})
    return jsonify({"error": "Failed to delete"}), 500


@api_bp.route("/delete/series", methods=["POST"])
def delete_series():
    data = request.get_json(silent=True) or {}
    try:
        series_name = validate_name(data.get("series_name"), "Series name")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if delete_series_folder(series_name):
        return jsonify({"message": "Deleted successfully"})
    return jsonify({"error": "Failed to delete"}), 500


@api_bp.route("/stats", methods=["GET"])
def get_stats():
    from series_manager.services.cloudflare_service import get_cloudflare_stats

    return jsonify(get_cloudflare_stats())


@api_bp.route("/billing", methods=["GET"])
def get_billing():
    from series_manager.services.cloudflare_service import get_cloudflare_billing

    return jsonify(get_cloudflare_billing())
