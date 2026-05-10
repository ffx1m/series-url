import subprocess
import os
import threading
import uuid
import shutil
import re
from utils.r2 import upload_file_to_r2, load_config

# Store global task statuses
tasks = {}

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
    tasks[task_id] = {'status': 'processing', 'progress': '0%', 'message': 'กำลังดึงข้อมูลความยาววิดีโอ...', 'type': 'video'}
    
    thread = threading.Thread(target=_process_video_task, args=(task_id, series_name, ep_name, m3u8_url))
    thread.start()
    return task_id

def _process_video_task(task_id, series_name, ep_name, m3u8_url):
    try:
        tmp_dir = f"data/tmp_{task_id}"
        os.makedirs(tmp_dir, exist_ok=True)
        
        output_m3u8 = os.path.join(tmp_dir, "playlist.m3u8")
        
        # Get duration for progress calculation
        total_duration = get_video_duration(m3u8_url)
        tasks[task_id]['message'] = 'กำลังดาวน์โหลดและแปลงไฟล์ด้วย FFmpeg...'
        
        cmd = [
            'ffmpeg', '-y', '-i', m3u8_url,
            '-c', 'copy',
            '-f', 'hls',
            '-hls_time', '6',
            '-hls_list_size', '0',
            output_m3u8
        ]
        
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        
        # Regex to find time=HH:MM:SS.ms
        time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")
        
        for line in process.stdout:
            match = time_regex.search(line)
            if match and total_duration > 0:
                current_time = time_to_seconds(match.group(1))
                percent = min(100, int((current_time / total_duration) * 100))
                # FFmpeg phase represents 50% of the total task
                overall_percent = int(percent / 2)
                tasks[task_id]['progress'] = f"{overall_percent}%"
                tasks[task_id]['message'] = f"กำลังแปลงไฟล์วิดีโอ... ({percent}%)"
            elif 'frame=' in line:
                # Fallback if no duration but we see frames processing
                if tasks[task_id]['progress'] == '0%':
                    tasks[task_id]['progress'] = '25%'
                    tasks[task_id]['message'] = 'กำลังดึงข้อมูลและแปลงวิดีโออย่างต่อเนื่อง...'
        
        process.wait()
        
        if process.returncode != 0:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = 'FFmpeg process failed. ตรวจสอบ URL หรือความสมบูรณ์ของไฟล์ต้นทาง'
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
            
        tasks[task_id]['message'] = 'กำลังอัพโหลดไฟล์ขึ้นสู่ Cloudflare R2...'
        tasks[task_id]['progress'] = '50%'
        
        # Upload files in tmp_dir to R2
        files_to_upload = [f for f in os.listdir(tmp_dir) if f.endswith('.ts') or f.endswith('.m3u8')]
        total_files = len(files_to_upload)
        
        if total_files == 0:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = 'ไม่พบไฟล์ที่จะอัพโหลด อาจเกิดข้อผิดพลาดในการแปลงไฟล์'
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        for idx, filename in enumerate(files_to_upload):
            local_path = os.path.join(tmp_dir, filename)
            s3_key = f"series/{series_name}/{ep_name}/{filename}"
            content_type = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/MP2T'
            
            upload_file_to_r2(local_path, s3_key, content_type=content_type)
            
            # Upload phase represents 50% to 100%
            percent = 50 + int(((idx + 1) / total_files) * 50)
            tasks[task_id]['progress'] = f"{percent}%"
            tasks[task_id]['message'] = f"อัพโหลดไฟล์ {idx+1}/{total_files} ชิ้น"
            
        # Cleanup
        shutil.rmtree(tmp_dir, ignore_errors=True)
        
        config = load_config()
        domain = config.get('worker_domain', 'https://series.film01-thirx.workers.dev').rstrip('/')
        final_url = f"{domain}/series/{series_name}/{ep_name}/playlist.m3u8"
        
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['progress'] = '100%'
        tasks[task_id]['message'] = 'เสร็จสิ้นกระบวนการทั้งหมด!'
        tasks[task_id]['result_url'] = final_url
        
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['message'] = str(e)


def start_image_conversion(series_name, input_image_path):
    task_id = str(uuid.uuid4())
    tasks[task_id] = {'status': 'processing', 'progress': '10%', 'message': 'กำลังเริ่มแปลงรูปภาพ...', 'type': 'image'}
    
    thread = threading.Thread(target=_process_image_task, args=(task_id, series_name, input_image_path))
    thread.start()
    return task_id

def _process_image_task(task_id, series_name, input_image_path):
    try:
        output_webp = f"{input_image_path}.webp"
        
        tasks[task_id]['message'] = 'กำลังบีบอัดรูปภาพด้วย WebP...'
        tasks[task_id]['progress'] = '30%'
        
        cmd = [
            'ffmpeg', '-y', '-i', input_image_path,
            '-c:v', 'libwebp',
            '-quality', '80',
            '-compression_level', '6',
            '-preset', 'picture',
            output_webp
        ]
        
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        process.wait()
        
        if process.returncode != 0:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = 'FFmpeg image conversion failed.'
            return
            
        tasks[task_id]['progress'] = '70%'
        tasks[task_id]['message'] = 'กำลังอัพโหลดรูปภาพขึ้น Cloudflare R2...'
        
        filename = os.path.basename(input_image_path).split('.')[0] + ".webp"
        s3_key = f"series/{series_name}/{filename}"
        
        upload_success = upload_file_to_r2(output_webp, s3_key, content_type='image/webp')
        
        # Cleanup
        try: os.remove(input_image_path)
        except: pass
        try: os.remove(output_webp)
        except: pass
        
        if not upload_success:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['message'] = 'ล้มเหลวในการอัพโหลดไปยัง R2 ตรวจสอบการตั้งค่า API'
            return
            
        config = load_config()
        domain = config.get('worker_domain', 'https://series.film01-thirx.workers.dev').rstrip('/')
        final_url = f"{domain}/{s3_key}"
        
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['progress'] = '100%'
        tasks[task_id]['message'] = 'อัพโหลดและแปลงรูปภาพสำเร็จ!'
        tasks[task_id]['result_url'] = final_url
        
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['message'] = str(e)

def get_task_status(task_id):
    return tasks.get(task_id, {'status': 'not_found'})