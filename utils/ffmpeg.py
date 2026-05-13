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
    from utils.r2 import db
    if db is not None:
        try:
            # AUTO-CLEANUP: If task is completed or canceled, remove it from DB immediately
            if update_data.get('status') in ['completed', 'canceled']:
                db.tasks.delete_one({'task_id': task_id})
                if task_id in tasks:
                    del tasks[task_id]
                return

            db.tasks.update_one(
                {'task_id': task_id},
                {'$set': tasks[task_id]},
                upsert=True
            )
        except Exception as e:
            print(f"Error updating task in MongoDB: {e}")

def cancel_task(task_id):
    """Cancels a running task or removes a queued task."""
    if task_id in running_processes:
        try:
            process = running_processes[task_id]
            process.terminate()
        except Exception as e:
            print(f"Error killing process {task_id}: {e}")

    update_task_status(task_id, {
        'status': 'canceled',
        'message': 'งานถูกยกเลิกโดยผู้ใช้'
    })
    return True

# --- Queue System ---
def worker_loop():
    """Background worker that processes video tasks from the queue via MongoDB."""
    print("Worker loop started and waiting for MongoDB...")
    import time
    
    while True:
        try:
            from utils.r2 import db
            if db is not None:
                # Find the oldest queued task
                queued_task = db.tasks.find_one({'status': 'queued', 'type': 'video'}, sort=[('_id', 1)])
                
                if queued_task:
                    task_id = queued_task['task_id']
                    series_name = queued_task.get('series_name')
                    ep_name = queued_task.get('ep_name')
                    m3u8_url = queued_task.get('m3u8_url')
                    
                    print(f"[*] Worker found task: {series_name} - {ep_name} ({task_id})")
                    _process_video_task(task_id, series_name, ep_name, m3u8_url)
                    print(f"[*] Worker finished task: {task_id}")
                    continue 
            
        except Exception as e:
            print(f"[!] Error in worker_loop: {e}")
        
        time.sleep(3) 

threading.Thread(target=worker_loop, daemon=True).start()

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
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0

def start_video_conversion(series_name, ep_name, m3u8_url):
    task_id = str(uuid.uuid4())
    task_data = {
        'task_id': task_id,
        'status': 'queued', 
        'progress': '0%', 
        'message': 'รอคิวใน MongoDB...', 
        'type': 'video',
        'logs': [],
        'name': f"[{series_name}] - {ep_name}",
        'series_name': series_name,
        'ep_name': ep_name,
        'm3u8_url': m3u8_url
    }
    
    from utils.r2 import db
    if db is None:
        task_data['status'] = 'error'
        task_data['message'] = 'ไม่สามารถเพิ่มเข้าคิวได้: ยังไม่ได้เชื่อมต่อ MongoDB'
        tasks[task_id] = task_data
        return task_id

    update_task_status(task_id, task_data)
    return task_id

def _process_video_task(task_id, series_name, ep_name, m3u8_url):
    task_lock.acquire() 

    try:
        update_task_status(task_id, {
            'status': 'processing',
            'message': 'กำลังเริ่มประมวลผล...'
        })
        
        tmp_dir = f"data/tmp_{task_id}"
        os.makedirs(tmp_dir, exist_ok=True)
        output_m3u8 = os.path.join(tmp_dir, "playlist.m3u8")
        total_duration = get_video_duration(m3u8_url)
        update_task_status(task_id, {'message': 'กำลังดาวน์โหลดและแปลงไฟล์ด้วย FFmpeg...'})

        cmd = [
            'ffmpeg', '-y', '-i', m3u8_url,
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '22',
            '-vf', "scale='min(540,iw)':-2",
            '-c:a', 'aac', '-b:a', '96k', '-ar', '48000',
            '-g', '72', '-keyint_min', '72', '-sc_threshold', '0',
            '-hls_time', '3', '-hls_playlist_type', 'vod',
            '-hls_flags', 'independent_segments', '-hls_segment_type', 'mpegts',
            '-hls_segment_filename', os.path.join(tmp_dir, "segment_%03d.ts"),
            '-start_number', '0', output_m3u8
        ]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        running_processes[task_id] = process
        time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")
        current_progress_val = 0
        task_logs = []

        for line in process.stdout:
            task_logs.append(line.strip())
            if len(task_logs) > 100: task_logs.pop(0)
            match = time_regex.search(line)
            status_update = {'logs': task_logs}
            if match and total_duration > 0:
                current_time = time_to_seconds(match.group(1))
                percent = min(100, int((current_time / total_duration) * 100))
                overall_percent = int(percent * 0.85)
                if overall_percent > current_progress_val:
                    current_progress_val = overall_percent
                    status_update['progress'] = f"{current_progress_val}%"
                    status_update['message'] = f"กำลังแปลงไฟล์วิดีโอ... {percent}%"
                    update_task_status(task_id, status_update)
            elif 'frame=' in line:
                if current_progress_val < 5:
                    current_progress_val = 5
                    status_update['progress'] = '5%'
                    status_update['message'] = 'กำลังเริ่มประมวลผลวิดีโอ...'
                    update_task_status(task_id, status_update)
            else:
                if len(task_logs) % 15 == 0: update_task_status(task_id, status_update)

        process.wait()
        if task_id in running_processes: del running_processes[task_id]
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

        files_to_upload = [f for f in os.listdir(tmp_dir) if f.endswith('.ts') or f.endswith('.m3u8')]
        total_files = len(files_to_upload)
        if total_files == 0:
            update_task_status(task_id, {'status': 'error', 'message': 'ไม่พบไฟล์ที่จะอัพโหลด'})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        for idx, filename in enumerate(files_to_upload):
            local_path = os.path.join(tmp_dir, filename)
            s3_key = f"series/{series_name}/{ep_name}/{filename}"
            content_type = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/MP2T'
            upload_file_to_r2(local_path, s3_key, content_type=content_type)
            task_logs.append(f"Uploaded: {filename}")
            if len(task_logs) > 100: task_logs.pop(0)
            percent = 85 + int(((idx + 1) / total_files) * 15)
            update_task_status(task_id, {
                'progress': f"{percent}%",
                'message': f"กำลังอัพโหลด: {filename} ({idx+1}/{total_files})",
                'logs': task_logs
            })

        shutil.rmtree(tmp_dir, ignore_errors=True)
        update_task_status(task_id, {
            'status': 'completed',
            'progress': '100%',
            'message': 'สำเร็จ!'
        })
    except Exception as e:
        update_task_status(task_id, {'status': 'error', 'message': str(e)})
    finally:
        task_lock.release()


def start_image_conversion(series_name, input_image_path):
    task_id = str(uuid.uuid4())
    task_data = {
        'task_id': task_id, 'status': 'processing', 'progress': '10%', 
        'message': 'กำลังเริ่มแปลงรูปภาพ...', 'type': 'image', 'logs': [],
        'name': f"Image for {series_name}"
    }
    update_task_status(task_id, task_data)
    thread = threading.Thread(target=_process_image_task, args=(task_id, series_name, input_image_path))
    thread.start()
    return task_id

def _process_image_task(task_id, series_name, input_image_path):
    if not task_lock.acquire(blocking=False):
        update_task_status(task_id, {'status': 'error', 'message': 'ระบบไม่ว่าง'})
        return
    try:
        output_webp = f"{input_image_path}.webp"
        task_logs = []
        update_task_status(task_id, {'message': 'กำลังบีบอัดรูปภาพ...', 'progress': '30%'})
        cmd = ['ffmpeg', '-y', '-i', input_image_path, '-c:v', 'libwebp', '-quality', '80', output_webp]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        running_processes[task_id] = process
        for line in process.stdout:
            task_logs.append(line.strip())
            if len(task_logs) > 100: task_logs.pop(0)
        process.wait()
        if task_id in running_processes: del running_processes[task_id]
        if tasks.get(task_id, {}).get('status') == 'canceled':
            try: os.remove(input_image_path); os.remove(output_webp)
            except: pass
            return
        if process.returncode != 0:
            update_task_status(task_id, {'status': 'error', 'message': 'แปลงรูปไม่สำเร็จ', 'logs': task_logs})
            return
        update_task_status(task_id, {'progress': '70%', 'message': 'กำลังอัพโหลด...', 'logs': task_logs})
        filename = os.path.basename(input_image_path).split('.')[0] + ".webp"
        s3_key = f"series/{series_name}/{filename}"
        upload_success = upload_file_to_r2(output_webp, s3_key, content_type='image/webp')
        try: os.remove(input_image_path); os.remove(output_webp)
        except: pass
        if not upload_success:
            update_task_status(task_id, {'status': 'error', 'message': 'อัพโหลด R2 ไม่สำเร็จ'})
            return
        update_task_status(task_id, {'status': 'completed', 'progress': '100%', 'message': 'สำเร็จ!'})
    except Exception as e:
        update_task_status(task_id, {'status': 'error', 'message': str(e)})
    finally:
        task_lock.release()

def get_task_status(task_id):
    if task_id in tasks: return tasks[task_id]
    from utils.r2 import db
    if db is not None:
        try:
            task = db.tasks.find_one({'task_id': task_id})
            if task:
                task_data = dict(task); task_data.pop('_id', None)
                return task_data
        except: pass
    return {'status': 'not_found'}

def get_all_tasks():
    # Final cleanup of current memory tasks
    all_tasks = dict(tasks)
    from utils.r2 import db
    if db is not None:
        try:
            # LIVE MONITOR LOGIC: Explicitly fetch processing, queued, AND error
            db_tasks = db.tasks.find({
                'status': {'$in': ['processing', 'queued', 'error']}
            }).sort('_id', -1)
            for task in db_tasks:
                tid = task.get('task_id')
                # Overwrite/Add from DB to ensure freshest status
                task_data = dict(task)
                task_data.pop('_id', None)
                all_tasks[tid] = task_data
        except Exception as e:
            print(f"Error fetching tasks for Monitor: {e}")
    return all_tasks
