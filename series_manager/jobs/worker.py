import os
import re
import shutil
import subprocess
import threading
import time

from werkzeug.utils import secure_filename

from series_manager.jobs.store import (
    append_log,
    claim_next_job,
    delete_job,
    get_job,
    replace_logs,
    should_cancel,
    update_job,
)
from series_manager.services.r2_service import public_domain, upload_file_to_r2

running_processes = {}
worker_lock = threading.Lock()


def worker_loop(poll_interval=3):
    print("Job worker loop started")
    while True:
        try:
            job = claim_next_job()
            if job:
                process_job(job)
                continue
        except Exception as exc:
            print(f"Worker loop error: {exc}")
        time.sleep(poll_interval)


def process_job(job):
    with worker_lock:
        if job.get("type") == "video":
            process_video_job(job)
        elif job.get("type") == "image":
            process_image_job(job)
        else:
            update_job(job["task_id"], status="error", message=f"Unknown job type: {job.get('type')}")


def terminate_task(task_id):
    process = running_processes.get(task_id)
    if process:
        try:
            process.terminate()
        except Exception as exc:
            print(f"Error terminating process {task_id}: {exc}")
    update_job(task_id, status="canceled", message="Task canceled")
    return True


def get_video_duration(m3u8_url):
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            m3u8_url,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        return float(result.stdout.strip())
    except Exception:
        return 0


def time_to_seconds(time_str):
    try:
        hours, minutes, seconds = time_str.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:
        return 0


def process_video_job(job):
    task_id = job["task_id"]
    series_name = job.get("series_name") or job.get("payload", {}).get("series_name")
    ep_name = job.get("ep_name") or job.get("payload", {}).get("ep_name")
    m3u8_url = job.get("m3u8_url") or job.get("payload", {}).get("m3u8_url")
    tmp_dir = f"data/tmp_{task_id}"
    task_logs = []

    try:
        os.makedirs(tmp_dir, exist_ok=True)
        output_m3u8 = os.path.join(tmp_dir, "playlist.m3u8")
        update_job(task_id, stage="probe", message="Analyzing source video", progress="1%", progress_value=1)
        total_duration = get_video_duration(m3u8_url)

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            m3u8_url,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "22",
            "-vf",
            "scale='min(540,iw)':-2",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            "48000",
            "-g",
            "72",
            "-keyint_min",
            "72",
            "-sc_threshold",
            "0",
            "-hls_time",
            "3",
            "-hls_playlist_type",
            "vod",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            os.path.join(tmp_dir, "segment_%03d.ts"),
            "-start_number",
            "0",
            output_m3u8,
        ]

        update_job(task_id, stage="transcoding", message="Transcoding video", progress="2%", progress_value=2)
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        running_processes[task_id] = process
        progress_regex = re.compile(r"frame=\s*(\d+)\s+fps=\s*([\d.]+).*time=(\d{2}:\d{2}:\d{2}\.\d{2}).*speed=\s*([\d.]+x)")
        current_progress = 2

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            task_logs.append(line)
            task_logs = task_logs[-150:]
            append_log(task_id, line)

            if should_cancel(task_id):
                process.terminate()
                raise RuntimeError("Task canceled")

            match = progress_regex.search(line)
            if match and total_duration > 0:
                frame = match.group(1)
                current_time = time_to_seconds(match.group(3))
                speed = match.group(4)
                source_percent = min(100, int((current_time / total_duration) * 100))
                overall_percent = max(2, int(source_percent * 0.85))
                if overall_percent > current_progress:
                    current_progress = overall_percent
                    update_job(
                        task_id,
                        progress=f"{overall_percent}%",
                        progress_value=overall_percent,
                        message=f"Transcoding video: {source_percent}% (frame: {frame}, speed: {speed})",
                    )

        process.wait()
        running_processes.pop(task_id, None)

        if process.returncode != 0:
            replace_logs(task_id, task_logs)
            update_job(task_id, status="error", stage="failed", message="FFmpeg failed. Check the source video URL.")
            return

        files_to_upload = [name for name in os.listdir(tmp_dir) if name.endswith((".ts", ".m3u8"))]
        if not files_to_upload:
            update_job(task_id, status="error", stage="failed", message="No output files were generated")
            return

        total_bytes = sum(os.path.getsize(os.path.join(tmp_dir, name)) for name in files_to_upload)
        uploaded_bytes = 0
        update_job(task_id, stage="uploading", message="Uploading video files to R2", progress="85%", progress_value=85)

        for filename in files_to_upload:
            if should_cancel(task_id):
                raise RuntimeError("Task canceled")
            local_path = os.path.join(tmp_dir, filename)
            file_size = os.path.getsize(local_path)
            s3_key = f"series/{series_name}/{ep_name}/{filename}"
            content_type = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/MP2T"
            if not upload_file_to_r2(local_path, s3_key, content_type=content_type):
                update_job(task_id, status="error", stage="failed", message=f"Failed to upload {filename}")
                return

            uploaded_bytes += file_size
            uploaded_mb = round(uploaded_bytes / (1024 * 1024), 2)
            total_mb = round(total_bytes / (1024 * 1024), 2)
            upload_percent = uploaded_bytes / total_bytes if total_bytes else 1
            overall_percent = 85 + int(upload_percent * 15)
            append_log(task_id, f"Uploaded: {filename}")
            update_job(
                task_id,
                progress=f"{overall_percent}%",
                progress_value=overall_percent,
                message=f"Uploading: {uploaded_mb} MB / {total_mb} MB",
            )

        update_job(
            task_id,
            status="completed",
            stage="completed",
            progress="100%",
            progress_value=100,
            message="Completed",
            result={"url": f"{public_domain()}/series/{series_name}/{ep_name}/playlist.m3u8"},
        )
        # Keep completed video jobs briefly out of the queue UI by deleting them, preserving old behavior.
        delete_job(task_id)
    except RuntimeError as exc:
        running_processes.pop(task_id, None)
        if "canceled" in str(exc).lower():
            update_job(task_id, status="canceled", stage="canceled", message="Task canceled")
        else:
            update_job(task_id, status="error", stage="failed", message=str(exc))
    except Exception as exc:
        running_processes.pop(task_id, None)
        update_job(task_id, status="error", stage="failed", message=str(exc))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def process_image_job(job):
    task_id = job["task_id"]
    series_name = job.get("series_name") or job.get("payload", {}).get("series_name")
    input_image_path = job.get("input_image_path") or job.get("payload", {}).get("input_image_path")
    output_webp = f"{input_image_path}.webp"
    task_logs = []

    try:
        update_job(task_id, stage="transcoding", message="Converting image to WebP", progress="20%", progress_value=20)
        cmd = ["ffmpeg", "-y", "-i", input_image_path, "-c:v", "libwebp", "-quality", "80", output_webp]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        running_processes[task_id] = process

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            task_logs.append(line)
            task_logs = task_logs[-150:]
            append_log(task_id, line)
            if should_cancel(task_id):
                process.terminate()
                raise RuntimeError("Task canceled")

        process.wait()
        running_processes.pop(task_id, None)

        if process.returncode != 0:
            replace_logs(task_id, task_logs)
            update_job(task_id, status="error", stage="failed", message="Image conversion failed")
            return

        update_job(task_id, stage="uploading", message="Uploading image to R2", progress="60%", progress_value=60)
        filename = f"{secure_filename(os.path.basename(input_image_path)).rsplit('.', 1)[0]}.webp"
        s3_key = f"series/{series_name}/{filename}"
        if not upload_file_to_r2(output_webp, s3_key, content_type="image/webp"):
            update_job(task_id, status="error", stage="failed", message="R2 upload failed")
            return

        result_url = f"{public_domain()}/{s3_key}"
        update_job(
            task_id,
            status="completed",
            stage="completed",
            progress="100%",
            progress_value=100,
            message="Completed",
            result={"url": result_url},
            result_url=result_url,
        )
    except RuntimeError as exc:
        running_processes.pop(task_id, None)
        if "canceled" in str(exc).lower():
            update_job(task_id, status="canceled", stage="canceled", message="Task canceled")
        else:
            update_job(task_id, status="error", stage="failed", message=str(exc))
    except Exception as exc:
        running_processes.pop(task_id, None)
        update_job(task_id, status="error", stage="failed", message=str(exc))
    finally:
        for path in (input_image_path, output_webp):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
