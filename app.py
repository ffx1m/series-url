import os
from flask import Flask, render_template, request, jsonify, redirect, url_for
from werkzeug.utils import secure_filename

from utils.r2 import (
    list_series_folders, create_series_folder, list_folder_contents,
    delete_object, delete_ep_folder, delete_series_folder, load_config, save_config
)
from utils.ffmpeg import (
    start_video_conversion, start_image_conversion, get_task_status, 
    get_all_tasks, cancel_task
)
from utils.cloudflare import get_cloudflare_stats, get_cloudflare_billing

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'data/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB limit for images
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('data', exist_ok=True)

@app.route('/')
def dashboard_page():
    return render_template('dashboard.html')

@app.route('/video')
def video_page():
    return render_template('video.html')

@app.route('/image')
def image_page():
    return render_template('image.html')

@app.route('/manage')
def manage_page():
    return render_template('manage.html')

@app.route('/queue')
def queue_page():
    return render_template('queue.html')

@app.route('/logs')
def logs_page():
    return render_template('logs.html')

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        config = {
            'cloudflare_account_id': request.form.get('cloudflare_account_id'),
            'cloudflare_access_key': request.form.get('cloudflare_access_key'),
            'cloudflare_secret_key': request.form.get('cloudflare_secret_key'),
            'cloudflare_api_token': request.form.get('cloudflare_api_token'),
            'r2_bucket_name': request.form.get('r2_bucket_name', 'data-series'),
            'worker_domain': request.form.get('worker_domain', 'https://series.film01-thirx.workers.dev')
        }
        save_config(config)
        return redirect(url_for('settings_page', success='1'))
    
    config = load_config()
    success = request.args.get('success') == '1'
    return render_template('settings.html', config=config, success=success)

@app.route('/api/db/status')
def db_status():
    from utils.r2 import db
    return jsonify({'connected': db is not None})

# API Endpoints
@app.route('/api/series', methods=['GET'])
def get_series():
    folders = list_series_folders()
    return jsonify(folders)

@app.route('/api/series', methods=['POST'])
def create_series():
    data = request.json
    series_name = data.get('name')
    if not series_name:
        return jsonify({'error': 'Series name required'}), 400
    
    success = create_series_folder(series_name)
    if success:
        return jsonify({'message': 'Created successfully'})
    return jsonify({'error': 'Failed to create folder'}), 500

@app.route('/api/convert/video', methods=['POST'])
def convert_video():
    data = request.json
    series_name = data.get('series_name')
    ep_name = data.get('ep_name')
    m3u8_url = data.get('url')
    
    if not all([series_name, ep_name, m3u8_url]):
        return jsonify({'error': 'Missing parameters'}), 400
        
    task_id = start_video_conversion(series_name, ep_name, m3u8_url)
    return jsonify({'task_id': task_id})

@app.route('/api/convert/image', methods=['POST'])
def convert_image():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    series_name = request.form.get('series_name')
    
    if file.filename == '' or not series_name:
        return jsonify({'error': 'Missing file or series name'}), 400
        
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)
    
    task_id = start_image_conversion(series_name, file_path)
    return jsonify({'task_id': task_id})

@app.route('/api/task/<task_id>', methods=['GET'])
def task_status(task_id):
    status = get_task_status(task_id)
    return jsonify(status)

@app.route('/api/tasks', methods=['GET'])
def all_tasks():
    tasks = get_all_tasks()
    return jsonify(tasks)

@app.route('/api/task/<task_id>/cancel', methods=['POST'])
def cancel_task_api(task_id):
    success = cancel_task(task_id)
    if success:
        return jsonify({'message': 'Task cancellation requested'})
    return jsonify({'error': 'Failed to cancel task'}), 500

@app.route('/api/task/<task_id>/delete', methods=['POST'])
def delete_task_api(task_id):
    from utils.ffmpeg import tasks, cancel_task
    from utils.r2 import db
    import shutil

    # 1. Stop the task if it's running (this also handles process kill and lock release)
    cancel_task(task_id)

    # 2. Cleanup files
    tmp_dir = f"data/tmp_{task_id}"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 3. Explicitly remove from BOTH memory and DB to ensure it doesn't reappear
    if task_id in tasks:
        del tasks[task_id]

    if db is not None:
        try:
            db.tasks.delete_one({'task_id': task_id})
        except: pass

    return jsonify({'message': 'Task stopped, cleaned up, and deleted'})
@app.route('/api/series/<series_name>', methods=['GET'])
def series_contents(series_name):
    contents = list_folder_contents(series_name)
    config = load_config()
    domain = config.get('worker_domain', 'https://series.film01-thirx.workers.dev')
    return jsonify({'contents': contents, 'domain': domain})

@app.route('/api/delete/object', methods=['POST'])
def delete_item():
    data = request.json
    key = data.get('key')
    if delete_object(key):
        return jsonify({'message': 'Deleted successfully'})
    return jsonify({'error': 'Failed to delete'}), 500

@app.route('/api/delete/ep', methods=['POST'])
def delete_ep():
    data = request.json
    series_name = data.get('series_name')
    ep_name = data.get('ep_name')
    if delete_ep_folder(series_name, ep_name):
        return jsonify({'message': 'Deleted successfully'})
    return jsonify({'error': 'Failed to delete'}), 500

@app.route('/api/delete/series', methods=['POST'])
def delete_series():
    data = request.json
    series_name = data.get('series_name')
    if delete_series_folder(series_name):
        return jsonify({'message': 'Deleted successfully'})
    return jsonify({'error': 'Failed to delete'}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    stats = get_cloudflare_stats()
    return jsonify(stats)

@app.route('/api/billing', methods=['GET'])
def get_billing():
    billing = get_cloudflare_billing()
    return jsonify(billing)

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, port=int(os.environ.get('PORT', 10000)))
