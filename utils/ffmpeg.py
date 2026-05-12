import subprocess
import os
import threading
import uuid
import shutil
import re
from utils.r2 import upload_file_to_r2, load_config, db

# Store global task statuses (In-memory for fast access, but mirrored to MongoDB)
tasks = {}
# Store running process objects to allow cancellation
running_processes = {}
# Global lock to ensure only one heavy task starts at a time
task_lock = threading.Lock()

def update_task_status(task_id, update_data):
    # Update memory
    if task_id not in tasks:
        tasks[task_id] = {}
    tasks[task_id].update(update_data)

    # Update MongoDB
    if db is not None:
        try:
            db.tasks.update_one(
                {'task_id': task_id},
                {'$set': tasks[task_id]},
                upsert=True
            )
        except Exception as e:
            print(f"Error updating task in MongoDB: {e}")

def cancel_task(task_id):
    if task_id in running_processes:
        try:
            process = running_processes[task_id]
            process.terminate() # or process.kill()
            return True
        except Exception as e:
            print(f"Error killing process {task_id}: {e}")

    # Even if process not found, we mark as canceled in DB
    update_task_status(task_id, {
        'status': 'canceled',
        'message': 'งานถูกยกเลิกโดยผู้ใช้'
    })
    return True

def get_video_duration(m3u8_url):

    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            m3u8_url
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        duration = float(result.stdout.strip())
        return duration
    except Exception:
        return 0

def time_to_seconds(time_str):
    # time_str format: HH:MM:SS.ms
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0

def start_video_conversion(series_name, ep_name, m3u8_url):
    task_id = str(uuid.uuid4())
    task_data = {
        'task_id': task_id,
        'status': 'processing', 
        'progress': '0%', 
        'message': 'กำลังดึงข้อมูลความยาววิดีโอ...', 
        'type': 'video',
        'logs': [],
        'name': f"{series_name} - {ep_name}"
    }
    update_task_status(task_id, task_data)

    thread = threading.Thread(target=_process_video_task, args=(task_id, series_name, ep_name, m3u8_url))
    thread.start()
    return task_id

def _process_video_task(task_id, series_name, ep_name, m3u8_url):
    # Use lock to ensure only one heavy task runs at a time
    if not task_lock.acquire(blocking=False):
        update_task_status(task_id, {
            'status': 'error',
            'message': 'ระบบไม่สามารถเริ่มงานได้เนื่องจากมีงานอื่นกำลังดำเนินการอยู่'
        })
        return

    try:
        tmp_dir = f"data/tmp_{task_id}"
        os.makedirs(tmp_dir, exist_ok=True)

        output_m3u8 = os.path.join(tmp_dir, "playlist.m3u8")

        # Get duration for progress calculation
        total_duration = get_video_duration(m3u8_url)
        update_task_status(task_id, {'message': 'กำลังดาวน์โหลดและแปลงไฟล์ด้วย FFmpeg...'})

        cmd = [
            'ffmpeg', '-y', '-i', m3u8_url,
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '22',
            '-vf', "scale='min(540,iw)':-2",
            '-c:a', 'aac',
            '-b:a', '96k',
            '-ar', '48000',
            '-g', '72',
            '-keyint_min', '72',
            '-sc_threshold', '0',
            '-hls_time', '3',
            '-hls_playlist_type', 'vod',
            '-hls_flags', 'independent_segments',
            '-hls_segment_type', 'mpegts',
            '-hls_segment_filename', os.path.join(tmp_dir, "segment_%03d.ts"),
            '-start_number', '0',
            output_m3u8
        ]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        running_processes[task_id] = process

        # Regex to find time=HH:MM:SS.ms
        time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")

        current_progress_val = 0
        task_logs = []

        for line in process.stdout:
            # Store logs
            task_logs.append(line.strip())
            if len(task_logs) > 100:
                task_logs.pop(0)

            match = time_regex.search(line)
            status_update = {'logs': task_logs}

            if match and total_duration > 0:
                current_time = time_to_seconds(match.group(1))
                percent = min(100, int((current_time / total_duration) * 100))
                # FFmpeg phase represents 85% of the total task
                overall_percent = int(percent * 0.85)
                
                # Prevent progress from going backwards
                if overall_percent > current_progress_val:
                    current_progress_val = overall_percent
                    status_update['progress'] = f"{current_progress_val}%"
                    status_update['message'] = f"กำลังแปลงไฟล์วิดีโอ... {percent}%"
                    update_task_status(task_id, status_update)
            elif 'frame=' in line:
                # Fallback if no duration: start at 5% and stay until actual progress takes over
                if current_progress_val < 5:
                    current_progress_val = 5
                    status_update['progress'] = '5%'
                    status_update['message'] = 'กำลังเริ่มประมวลผลวิดีโอ...'
                    update_task_status(task_id, status_update)
            else:
                # Just update logs periodically
                if len(task_logs) % 15 == 0:
                    update_task_status(task_id, status_update)

        process.wait()
        
        # Remove from running processes
        if task_id in running_processes:
            del running_processes[task_id]

        # Check if task was canceled
        if tasks.get(task_id, {}).get('status') == 'canceled':
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        if process.returncode != 0:
            update_task_status(task_id, {
                'status': 'error',
                'message': 'FFmpeg ล้มเหลว! กรุณาตรวจสอบ URL วิดีโอต้นทาง',
                'logs': task_logs
            })
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        update_task_status(task_id, {
            'message': 'การแปลงไฟล์เสร็จสิ้น กำลังเริ่มอัพโหลด...',
            'progress': '85%',
            'logs': task_logs + ["Starting upload to R2..."]
        })

        # Upload files in tmp_dir to R2
        files_to_upload = [f for f in os.listdir(tmp_dir) if f.endswith('.ts') or f.endswith('.m3u8')]
        total_files = len(files_to_upload)

        if total_files == 0:
            update_task_status(task_id, {
                'status': 'error',
                'message': 'ไม่พบไฟล์ที่จะอัพโหลด อาจเกิดข้อผิดพลาดในการประมวลผล'
            })
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        for idx, filename in enumerate(files_to_upload):
            local_path = os.path.join(tmp_dir, filename)
            s3_key = f"series/{series_name}/{ep_name}/{filename}"
            content_type = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/MP2T'

            upload_file_to_r2(local_path, s3_key, content_type=content_type)

            # Update logs and progress
            task_logs.append(f"Uploaded: {filename}")
            if len(task_logs) > 100: task_logs.pop(0)

            # Upload phase represents 85% to 100%
            percent = 85 + int(((idx + 1) / total_files) * 15)
            update_task_status(task_id, {
                'progress': f"{percent}%",
                'message': f"กำลังอัพโหลด: {filename} ({idx+1}/{total_files})",
                'logs': task_logs
            })

        # Cleanup
        shutil.rmtree(tmp_dir, ignore_errors=True)

        config = load_config()
        domain = config.get('worker_domain', 'https://series.film01-thirx.workers.dev').rstrip('/')
        final_url = f"{domain}/series/{series_name}/{ep_name}/playlist.m3u8"

        task_logs.append("Task completed successfully.")
        update_task_status(task_id, {
            'status': 'completed',
            'progress': '100%',
            'message': 'อัพโหลดและแปลงไฟล์สำเร็จ 100%!',
            'result_url': final_url,
            'logs': task_logs
        })

    except Exception as e:
        update_task_status(task_id, {
            'status': 'error',
            'message': str(e)
        })
    finally:
        # Always release the lock so next task can start
        task_lock.release()


def start_image_conversion(series_name, input_image_path):
    task_id = str(uuid.uuid4())
    task_data = {
        'task_id': task_id,
        'status': 'processing', 
        'progress': '10%', 
        'message': 'กำลังเริ่มแปลงรูปภาพ...', 
        'type': 'image',
        'logs': [],
        'name': f"Image for {series_name}"
    }
    update_task_status(task_id, task_data)

    thread = threading.Thread(target=_process_image_task, args=(task_id, series_name, input_image_path))
    thread.start()
    return task_id

def _process_image_task(task_id, series_name, input_image_path):
    # Use lock to ensure only one heavy task runs at a time
    if not task_lock.acquire(blocking=False):
        update_task_status(task_id, {
            'status': 'error',
            'message': 'ระบบไม่สามารถเริ่มงานได้เนื่องจากมีงานอื่นกำลังดำเนินการอยู่'
        })
        return

    try:
        output_webp = f"{input_image_path}.webp"
        task_logs = []

        update_task_status(task_id, {
            'message': 'กำลังบีบอัดรูปภาพด้วย WebP...',
            'progress': '30%'
        })

        cmd = [
            'ffmpeg', '-y', '-i', input_image_path,
            '-c:v', 'libwebp',
            '-quality', '80',
            '-compression_level', '6',
            '-preset', 'picture',
            output_webp
        ]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        running_processes[task_id] = process
        
        for line in process.stdout:
            task_logs.append(line.strip())
            if len(task_logs) > 100: task_logs.pop(0)

        process.wait()
        
        # Remove from running processes
        if task_id in running_processes:
            del running_processes[task_id]
            
        if tasks.get(task_id, {}).get('status') == 'canceled':
            try: os.remove(input_image_path)
            except: pass
            try: os.remove(output_webp)
            except: pass
            return

        if process.returncode != 0:
            update_task_status(task_id, {
                'status': 'error',
                'message': 'FFmpeg image conversion failed.',
                'logs': task_logs
            })
            return

        update_task_status(task_id, {
            'progress': '70%',
            'message': 'กำลังอัพโหลดรูปภาพขึ้น Cloudflare R2...',
            'logs': task_logs
        })

        filename = os.path.basename(input_image_path).split('.')[0] + ".webp"
        s3_key = f"series/{series_name}/{filename}"

        upload_success = upload_file_to_r2(output_webp, s3_key, content_type='image/webp')
        task_logs.append(f"Uploaded to R2: {s3_key}")

        # Cleanup
        try: os.remove(input_image_path)
        except: pass
        try: os.remove(output_webp)
        except: pass

        if not upload_success:
            update_task_status(task_id, {
                'status': 'error',
                'message': 'ล้มเหลวในการอัพโหลดไปยัง R2 ตรวจสอบการตั้งค่า API',
                'logs': task_logs
            })
            return

        config = load_config()
        domain = config.get('worker_domain', 'https://series.film01-thirx.workers.dev').rstrip('/')
        final_url = f"{domain}/{s3_key}"

        task_logs.append("Task completed successfully.")
        update_task_status(task_id, {
            'status': 'completed',
            'progress': '100%',
            'message': 'อัพโหลดและแปลงรูปภาพสำเร็จ!',
            'result_url': final_url,
            'logs': task_logs
        })

    except Exception as e:
        update_task_status(task_id, {
            'status': 'error',
            'message': str(e)
        })
    finally:
        # Always release the lock
        task_lock.release()

def get_task_status(task_id):
    # Try memory first
    if task_id in tasks:
        return tasks[task_id]

    # Fallback to MongoDB
    if db is not None:
        try:
            task = db.tasks.find_one({'task_id': task_id})
            if task:
                task_data = dict(task)
                task_data.pop('_id', None)
                tasks[task_id] = task_data # Cache it
                return task_data
        except Exception as e:
            print(f"Error fetching task from MongoDB: {e}")

    return {'status': 'not_found'}

def get_all_tasks():
    # Merge memory and MongoDB
    all_tasks = dict(tasks)

    if db is not None:
        try:
            # Get last 50 tasks from MongoDB
            db_tasks = db.tasks.find().sort('_id', -1).limit(50)
            for task in db_tasks:
                tid = task.get('task_id')
                if tid and tid not in all_tasks:
                    task_data = dict(task)
                    task_data.pop('_id', None)
                    all_tasks[tid] = task_data
        except Exception as e:
            print(f"Error fetching all tasks from MongoDB: {e}")

    return all_tasks