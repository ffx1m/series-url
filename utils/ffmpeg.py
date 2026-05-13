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

def update_task_status(task_id, update_data, force_db=False):
    # Update memory (Fastest)
    if task_id not in tasks:
        tasks[task_id] = {}
    
    # Save old status to check for changes
    old_status = tasks[task_id].get('status')
    tasks[task_id].update(update_data)
    new_status = tasks[task_id].get('status')

    # Update MongoDB (With throttling to prevent lag)
    from utils.r2 import db
    if db is not None:
        try:
            task_type = tasks[task_id].get('type')
            
            # AUTO-CLEANUP logic:
            # 1. Always delete if canceled
            # 2. Delete video tasks if completed (original behavior)
            # 3. KEEP image tasks if completed (so UI can get the result_url)
            should_delete = (new_status == 'canceled') or (new_status == 'completed' and task_type == 'video')
            
            if should_delete:
                db.tasks.delete_one({'task_id': task_id})
                if task_id in tasks:
                    del tasks[task_id]
                return

            # Throttling: Only write to DB if significant change or routine interval
            is_significant = force_db or (old_status != new_status) or ('progress' in update_data)
            
            if is_significant or (len(tasks[task_id].get('logs', [])) % 20 == 0):
                def perform_db_update(tid, data):
                    try:
                        db.tasks.update_one({'task_id': tid}, {'$set': data}, upsert=True)
                    except: pass
                
                if is_significant:
                    db.tasks.update_one({'task_id': task_id}, {'$set': tasks[task_id]}, upsert=True)
                else:
                    threading.Thread(target=perform_db_update, args=(task_id, tasks[task_id]), daemon=True).start()

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
    }, force_db=True)
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

    update_task_status(task_id, task_data, force_db=True)
    return task_id

def _process_video_task(task_id, series_name, ep_name, m3u8_url):
    task_lock.acquire() 

    try:
        update_task_status(task_id, {
            'status': 'processing',
            'message': 'กำลังเริ่มประมวลผล...'
        }, force_db=True)
        
        tmp_dir = f"data/tmp_{task_id}"
        os.makedirs(tmp_dir, exist_ok=True)
        output_m3u8 = os.path.join(tmp_dir, "playlist.m3u8")
        total_duration = get_video_duration(m3u8_url)
        update_task_status(task_id, {'message': 'กำลังวิเคราะห์วิดีโอและเริ่มแปลงไฟล์...'})

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
        
        # Enhanced regex to capture frame, fps, and speed
        progress_regex = re.compile(r"frame=\s*(\d+)\s+fps=\s*([\d.]+).*time=(\d{2}:\d{2}:\d{2}\.\d{2}).*speed=\s*([\d.]+x)")
        
        current_progress_val = 0
        task_logs = []

        for line in process.stdout:
            line = line.strip()
            if not line: continue
            task_logs.append(line)
            if len(task_logs) > 100: task_logs.pop(0)
            
            match = progress_regex.search(line)
            status_update = {'logs': task_logs}
            
            if match and total_duration > 0:
                frame = match.group(1)
                time_str = match.group(3)
                speed = match.group(4)
                
                current_time = time_to_seconds(time_str)
                percent = min(100, int((current_time / total_duration) * 100))
                overall_percent = int(percent * 0.85)
                
                if overall_percent > current_progress_val or len(task_logs) % 10 == 0:
                    current_progress_val = overall_percent
                    status_update['progress'] = f"{current_progress_val}%"
                    status_update['message'] = f"กำลังแปลงวิดีโอ: {percent}% (เฟรม: {frame}, ความเร็ว: {speed})"
                    update_task_status(task_id, status_update)
            else:
                if len(task_logs) % 15 == 0:
                    update_task_status(task_id, status_update)

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
            }, force_db=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        # Byte-based upload progress
        files_to_upload = [f for f in os.listdir(tmp_dir) if f.endswith('.ts') or f.endswith('.m3u8')]
        total_bytes = sum(os.path.getsize(os.path.join(tmp_dir, f)) for f in files_to_upload)
        bytes_uploaded = 0
        
        update_task_status(task_id, {
            'message': 'การแปลงไฟล์เสร็จสิ้น กำลังเริ่มอัพโหลด...',
            'progress': '85%',
            'logs': task_logs + ["Starting upload to R2..."]
        }, force_db=True)

        if not files_to_upload:
            update_task_status(task_id, {'status': 'error', 'message': 'ไม่พบไฟล์ที่จะอัพโหลด'}, force_db=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        for filename in files_to_upload:
            local_path = os.path.join(tmp_dir, filename)
            file_size = os.path.getsize(local_path)
            s3_key = f"series/{series_name}/{ep_name}/{filename}"
            content_type = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/MP2T'
            
            upload_file_to_r2(local_path, s3_key, content_type=content_type)
            
            bytes_uploaded += file_size
            task_logs.append(f"Uploaded: {filename} ({round(file_size/1024, 1)} KB)")
            if len(task_logs) > 100: task_logs.pop(0)
            
            upload_percent = (bytes_uploaded / total_bytes) if total_bytes > 0 else 1
            overall_percent = 85 + int(upload_percent * 15)
            
            update_task_status(task_id, {
                'progress': f"{overall_percent}%",
                'message': f"กำลังอัพโหลด: {round(bytes_uploaded/(1024*1024), 2)} MB / {round(total_bytes/(1024*1024), 2)} MB",
                'logs': task_logs
            })

        shutil.rmtree(tmp_dir, ignore_errors=True)
        update_task_status(task_id, {
            'status': 'completed',
            'progress': '100%',
            'message': 'สำเร็จ!'
        }, force_db=True)
    except Exception as e:
        update_task_status(task_id, {'status': 'error', 'message': str(e)}, force_db=True)
    finally:
        task_lock.release()

def start_image_conversion(series_name, input_image_path):
    task_id = str(uuid.uuid4())
    task_data = {
        'task_id': task_id, 'status': 'processing', 'progress': '10%', 
        'message': 'กำลังเริ่มแปลงรูปภาพ...', 'type': 'image', 'logs': [],
        'name': f"Image for {series_name}"
    }
    update_task_status(task_id, task_data, force_db=True)
    thread = threading.Thread(target=_process_image_task, args=(task_id, series_name, input_image_path))
    thread.start()
    return task_id

def _process_image_task(task_id, series_name, input_image_path):
    if not task_lock.acquire(blocking=False):
        update_task_status(task_id, {'status': 'error', 'message': 'ระบบไม่ว่าง'}, force_db=True)
        return
    try:
        output_webp = f"{input_image_path}.webp"
        task_logs = []
        update_task_status(task_id, {'message': 'กำลังบีบอัดรูปภาพเป็น WebP...', 'progress': '20%'})
        
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
            update_task_status(task_id, {'status': 'error', 'message': 'แปลงรูปไม่สำเร็จ (FFmpeg)', 'logs': task_logs}, force_db=True)
            return
            
        file_size = os.path.getsize(output_webp)
        update_task_status(task_id, {
            'progress': '60%', 
            'message': f'กำลังอัพโหลดรูปภาพ ({round(file_size/1024, 1)} KB)...', 
            'logs': task_logs
        })
        
        filename = os.path.basename(input_image_path).split('.')[0] + ".webp"
        s3_key = f"series/{series_name}/{filename}"
        upload_success = upload_file_to_r2(output_webp, s3_key, content_type='image/webp')
        
        try: os.remove(input_image_path); os.remove(output_webp)
        except: pass
        
        if not upload_success:
            update_task_status(task_id, {'status': 'error', 'message': 'อัพโหลด R2 ไม่สำเร็จ'}, force_db=True)
            return
            
        config = load_config()
        domain = config.get('worker_domain', 'https://series.film01-thirx.workers.dev')
        result_url = f"{domain}/{s3_key}"
            
        update_task_status(task_id, {
            'status': 'completed', 
            'progress': '100%', 
            'message': 'สำเร็จ!',
            'result_url': result_url
        }, force_db=True)
    except Exception as e:
        update_task_status(task_id, {'status': 'error', 'message': str(e)}, force_db=True)
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
    all_tasks_list = []
    task_ids = list(tasks.keys())
    for tid in task_ids:
        if tid in tasks:
            all_tasks_list.append(tasks[tid])
    
    from utils.r2 import db
    if db is not None:
        try:
            known_ids = [t['task_id'] for t in all_tasks_list]
            db_tasks = db.tasks.find({
                'task_id': {'$nin': known_ids},
                'status': {'$in': ['processing', 'queued', 'error']}
            }).sort('_id', 1)
            
            for task in db_tasks:
                task_data = dict(task)
                task_data.pop('_id', None)
                all_tasks_list.append(task_data)
        except Exception as e:
            print(f"Error fetching tasks for Monitor: {e}")
    
    def sort_key(t):
        status_order = {'processing': 0, 'queued': 1, 'error': 2, 'completed': 3}
        return status_order.get(t.get('status'), 99)
        
    all_tasks_list.sort(key=sort_key)
    return all_tasks_list
