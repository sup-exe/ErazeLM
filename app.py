#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ErazeLM — Web UI (Flask)
"""

import os
import uuid
import time
import json
import tempfile
import shutil
import threading
import re
import unicodedata
from flask import (
    Flask, render_template, request, jsonify,
    send_file, Response, make_response
)
from remover import WatermarkRemover, WatermarkConfig


def safe_filename(filename):
    """Sanitize filename while preserving Turkish/unicode characters."""
    # Normalize unicode
    filename = unicodedata.normalize('NFC', filename)
    # Keep only safe characters: letters (including Turkish), digits, spaces, dots, hyphens, underscores
    name, ext = os.path.splitext(filename)
    # Remove path separators and dangerous chars
    name = re.sub(r'[\\/:*?"<>|]', '', name)
    name = name.strip('. ')
    if not name:
        return ''
    return name + ext.lower()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB max
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Disable static file caching in dev

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), 'wm_remover_uploads')
OUTPUT_DIR = os.path.join(tempfile.gettempdir(), 'wm_remover_outputs')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# In-memory job status tracker
jobs = {}
jobs_lock = threading.Lock()

SUPPORTED_EXTENSIONS = {'.pdf', '.pptx', '.png', '.jpg', '.jpeg', '.webp'}

MIME_TYPES = {
    '.pdf': 'application/pdf',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
}


def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in SUPPORTED_EXTENSIONS


def process_file_task(job_id, input_path, output_path, overlay_path, ext):
    """Background task to process file."""
    remover = WatermarkRemover(WatermarkConfig())

    def update_progress(current, total, name=""):
        with jobs_lock:
            jobs[job_id]['progress'] = int((current / total) * 100)
            jobs[job_id]['status_text'] = f"İşleniyor: {name} ({current}/{total})"

    try:
        with jobs_lock:
            jobs[job_id]['status'] = 'processing'
            jobs[job_id]['status_text'] = 'İşlem başlıyor...'
            jobs[job_id]['progress'] = 0

        success = False
        if ext == '.pdf':
            with jobs_lock:
                jobs[job_id]['status_text'] = 'PDF işleniyor...'
            success = remover.process_pdf(input_path, output_path, preview=False)
            # Overlay for PDF is not directly supported in the same way,
            # but the PDF processing already does a clean inpainting
        elif ext == '.pptx':
            success = remover.process_pptx(
                input_path, output_path,
                overlay_path=overlay_path,
                progress_callback=update_progress
            )
        else:
            with jobs_lock:
                jobs[job_id]['status_text'] = 'Görsel işleniyor...'
            success = remover.process_image(
                input_path, output_path,
                overlay_path=overlay_path
            )

        with jobs_lock:
            if success:
                jobs[job_id]['status'] = 'completed'
                jobs[job_id]['progress'] = 100
                jobs[job_id]['status_text'] = 'Tamamlandı!'
                jobs[job_id]['output_path'] = output_path
            else:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['status_text'] = 'Watermark bulunamadı veya işlem başarısız.'

    except Exception as e:
        with jobs_lock:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['status_text'] = f'Hata: {str(e)}'


@app.route('/')
def index():
    return render_template('index.html', cache_bust=int(time.time()))


@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'Dosya çok büyük. Maksimum 200MB desteklenir.'}), 413


@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Sunucu hatası oluştu.'}), 500


@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'Dosya seçilmedi'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Dosya seçilmedi'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': 'Desteklenmeyen dosya formatı. PDF, PPTX, PNG, JPG, WEBP desteklenir.'}), 400

    # Generate unique job ID
    job_id = str(uuid.uuid4())[:8]
    clean_name = safe_filename(file.filename) or f"file_{job_id}.pdf"
    ext = os.path.splitext(clean_name)[1].lower()
    original_name = os.path.splitext(clean_name)[0]

    # Save uploaded file
    input_filename = f"{job_id}_input{ext}"
    input_path = os.path.join(UPLOAD_DIR, input_filename)
    file.save(input_path)

    # Output path
    output_filename = f"{original_name}_cleaned{ext}"
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}_output{ext}")

    # Handle overlay file
    overlay_path = None
    if 'overlay' in request.files:
        overlay_file = request.files['overlay']
        if overlay_file.filename:
            overlay_clean_name = safe_filename(overlay_file.filename) or f"overlay_{job_id}.png"
            overlay_ext = os.path.splitext(overlay_clean_name)[1].lower()
            overlay_filename = f"{job_id}_overlay{overlay_ext}"
            overlay_path = os.path.join(UPLOAD_DIR, overlay_filename)
            overlay_file.save(overlay_path)

    # Initialize job
    with jobs_lock:
        jobs[job_id] = {
            'status': 'queued',
            'progress': 0,
            'status_text': 'Sıraya alındı...',
            'filename': clean_name,
            'output_filename': output_filename,
            'output_path': None,
            'input_path': input_path,
            'ext': ext
        }

    # Start processing in background thread
    thread = threading.Thread(
        target=process_file_task,
        args=(job_id, input_path, output_path, overlay_path, ext),
        daemon=True
    )
    thread.start()

    return jsonify({
        'job_id': job_id,
        'filename': clean_name,
        'status': 'queued'
    })


@app.route('/api/status/<job_id>')
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'İş bulunamadı'}), 404
    return jsonify({
        'job_id': job_id,
        'status': job['status'],
        'progress': job['progress'],
        'status_text': job['status_text'],
        'filename': job['filename'],
        'output_filename': job.get('output_filename', '')
    })


@app.route('/api/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({'error': 'İş bulunamadı'}), 404
    if job['status'] != 'completed':
        return jsonify({'error': 'Dosya henüz hazır değil'}), 400

    output_path = job['output_path']
    if not output_path or not os.path.exists(output_path):
        return jsonify({'error': 'Çıktı dosyası bulunamadı'}), 404

    download_name = job.get('output_filename', os.path.basename(output_path))

    from urllib.parse import quote
    encoded_name = quote(download_name)

    response = send_file(
        output_path,
        as_attachment=True,
        download_name=download_name,
        mimetype='application/octet-stream'
    )
    # Force download — explicit headers override any browser caching/display behavior
    response.headers['Content-Disposition'] = (
        f'attachment; filename="{download_name}"; '
        f"filename*=UTF-8''{encoded_name}"
    )
    response.headers['Content-Type'] = 'application/octet-stream'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/api/preview/<job_id>')
def preview_file(job_id):
    """Return the original uploaded file as a preview (for images only)."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'İş bulunamadı'}), 404

    input_path = job.get('input_path')
    if not input_path or not os.path.exists(input_path):
        return jsonify({'error': 'Dosya bulunamadı'}), 404

    return send_file(input_path)


@app.route('/api/preview-output/<job_id>')
def preview_output(job_id):
    """Return the processed output file as a preview (for images only)."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'İş bulunamadı'}), 404

    if job['status'] != 'completed':
        return jsonify({'error': 'Henüz hazır değil'}), 400

    output_path = job.get('output_path')
    if not output_path or not os.path.exists(output_path):
        return jsonify({'error': 'Dosya bulunamadı'}), 404

    return send_file(output_path)


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  ErazeLM - Advanced Watermark Remover")
    print("  http://localhost:5000 adresinden erişebilirsiniz")
    print("=" * 60 + "\n")
    app.run(debug=True, host='0.0.0.0', port=5000)
