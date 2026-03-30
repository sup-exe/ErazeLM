/**
 * NotebookLM Watermark Remover — Frontend Logic
 */

(function () {
    'use strict';

    // -------- DOM Elements --------
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('file-input');
    const fileInfo = document.getElementById('file-info');
    const fileName = document.getElementById('file-name');
    const fileSize = document.getElementById('file-size');
    const fileRemove = document.getElementById('file-remove');

    const overlayToggle = document.getElementById('overlay-toggle');
    const overlayUpload = document.getElementById('overlay-upload');
    const overlayDropzone = document.getElementById('overlay-dropzone');
    const overlayInput = document.getElementById('overlay-input');
    const overlayPreview = document.getElementById('overlay-preview');
    const overlayPreviewImg = document.getElementById('overlay-preview-img');
    const overlayRemove = document.getElementById('overlay-remove');

    const processBtn = document.getElementById('process-btn');
    const uploadSection = document.getElementById('upload-section');
    const overlaySection = document.getElementById('overlay-section');
    const actionWrap = document.getElementById('action-wrap');
    const progressSection = document.getElementById('progress-section');
    const progressText = document.getElementById('progress-text');
    const progressFill = document.getElementById('progress-fill');
    const progressPercent = document.getElementById('progress-percent');

    const resultSection = document.getElementById('result-section');
    const resultText = document.getElementById('result-text');
    const downloadBtn = document.getElementById('download-btn');
    const newFileBtn = document.getElementById('new-file-btn');
    const previewContainer = document.getElementById('preview-container');

    const errorSection = document.getElementById('error-section');
    const errorText = document.getElementById('error-text');
    const retryBtn = document.getElementById('retry-btn');

    const toastContainer = document.getElementById('toast-container');

    // -------- State --------
    let selectedFile = null;
    let overlayFile = null;
    let currentJobId = null;
    let pollInterval = null;

    const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.webp'];

    // -------- Helpers --------
    function formatBytes(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    function getFileExt(name) {
        return '.' + name.split('.').pop().toLowerCase();
    }

    function isImageFile(name) {
        return IMAGE_EXTS.includes(getFileExt(name));
    }

    function showToast(message, type = 'success') {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `<span class="toast-dot"></span><span>${message}</span>`;
        toastContainer.appendChild(toast);

        setTimeout(() => {
            toast.style.animation = 'toastOut 0.3s ease forwards';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    }

    // -------- File Upload --------
    function handleFile(file) {
        const allowed = ['.pdf', '.pptx', '.png', '.jpg', '.jpeg', '.webp'];
        const ext = getFileExt(file.name);
        if (!allowed.includes(ext)) {
            showToast('Desteklenmeyen dosya formatı!', 'error');
            return;
        }

        if (file.size > 200 * 1024 * 1024) {
            showToast('Dosya çok büyük. Maksimum 200MB desteklenir.', 'error');
            return;
        }

        selectedFile = file;
        fileName.textContent = file.name;
        fileSize.textContent = formatBytes(file.size);
        fileInfo.classList.remove('hidden');
        dropzone.style.display = 'none';
        processBtn.disabled = false;
    }

    function clearFile() {
        selectedFile = null;
        fileInput.value = '';
        fileInfo.classList.add('hidden');
        dropzone.style.display = '';
        processBtn.disabled = true;
    }

    // Dropzone events
    dropzone.addEventListener('click', () => fileInput.click());

    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('drag-over');
    });

    dropzone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropzone.classList.remove('drag-over');
    });

    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('drag-over');
        if (e.dataTransfer.files.length) {
            handleFile(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length) {
            handleFile(fileInput.files[0]);
        }
    });

    fileRemove.addEventListener('click', clearFile);

    // -------- Overlay --------
    overlayToggle.addEventListener('change', () => {
        if (overlayToggle.checked) {
            overlayUpload.classList.remove('hidden');
        } else {
            overlayUpload.classList.add('hidden');
            clearOverlay();
        }
    });

    function handleOverlay(file) {
        const ext = getFileExt(file.name);
        if (!['.png', '.jpg', '.jpeg', '.webp'].includes(ext)) {
            showToast('Overlay için PNG, JPG veya WEBP kullanın', 'error');
            return;
        }
        overlayFile = file;
        const reader = new FileReader();
        reader.onload = (e) => {
            overlayPreviewImg.src = e.target.result;
            overlayPreview.classList.remove('hidden');
            overlayDropzone.style.display = 'none';
        };
        reader.readAsDataURL(file);
    }

    function clearOverlay() {
        overlayFile = null;
        overlayInput.value = '';
        overlayPreview.classList.add('hidden');
        overlayDropzone.style.display = '';
    }

    overlayDropzone.addEventListener('click', () => overlayInput.click());

    overlayDropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        overlayDropzone.style.borderColor = 'var(--accent-secondary)';
    });

    overlayDropzone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        overlayDropzone.style.borderColor = '';
    });

    overlayDropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        overlayDropzone.style.borderColor = '';
        if (e.dataTransfer.files.length) {
            handleOverlay(e.dataTransfer.files[0]);
        }
    });

    overlayInput.addEventListener('change', () => {
        if (overlayInput.files.length) {
            handleOverlay(overlayInput.files[0]);
        }
    });

    overlayRemove.addEventListener('click', clearOverlay);

    // -------- Process --------
    processBtn.addEventListener('click', startProcessing);

    async function startProcessing() {
        if (!selectedFile) {
            showToast('Lütfen önce bir dosya seçin', 'error');
            return;
        }

        // Show progress, hide others
        uploadSection.classList.add('hidden');
        overlaySection.classList.add('hidden');
        actionWrap.classList.add('hidden');
        errorSection.classList.add('hidden');
        resultSection.classList.add('hidden');
        progressSection.classList.remove('hidden');
        progressFill.style.width = '0%';
        progressPercent.textContent = '0%';
        progressText.textContent = 'Dosya yükleniyor...';

        // Build form data
        const formData = new FormData();
        formData.append('file', selectedFile);
        if (overlayToggle.checked && overlayFile) {
            formData.append('overlay', overlayFile);
        }

        try {
            const resp = await fetch('/api/upload', {
                method: 'POST',
                body: formData
            });

            if (!resp.ok) {
                let errMsg = 'Yükleme başarısız';
                try {
                    const err = await resp.json();
                    errMsg = err.error || errMsg;
                } catch (e) {}
                throw new Error(errMsg);
            }

            const data = await resp.json();
            currentJobId = data.job_id;
            progressText.textContent = 'İşlem başlatıldı...';

            // Start polling
            startPolling();

        } catch (err) {
            console.error('Upload error:', err);
            const msg = err.message === 'Failed to fetch'
                ? 'Sunucuya bağlanılamadı. Sunucunun çalıştığından emin olun.'
                : (err.message || 'Bilinmeyen bir hata oluştu');
            showError(msg);
        }
    }

    function startPolling() {
        if (pollInterval) clearTimeout(pollInterval);
        
        const poll = async () => {
            if (!currentJobId) return;
            try {
                const resp = await fetch(`/api/status/${currentJobId}`);
                if (!resp.ok) throw new Error('Durum alınamadı');

                const data = await resp.json();

                progressFill.style.width = data.progress + '%';
                progressPercent.textContent = data.progress + '%';
                progressText.textContent = data.status_text;

                if (data.status === 'completed') {
                    pollInterval = null;
                    showResult(data);
                    return;
                } else if (data.status === 'error') {
                    pollInterval = null;
                    showError(data.status_text);
                    return;
                }
            } catch (e) {
                // silently retry
            }
            // Loop with setTimeout to avoid pileup
            pollInterval = setTimeout(poll, 1000);
        };
        
        pollInterval = setTimeout(poll, 500);
    }

    function showResult(data) {
        progressSection.classList.add('hidden');
        resultSection.classList.remove('hidden');
        resultSection.style.animation = 'none';
        resultSection.offsetHeight; // trigger reflow
        resultSection.style.animation = 'fadeSlideUp 0.5s ease-out';

        resultText.textContent = `${data.filename} başarıyla temizlendi!`;
        currentOutputFilename = data.output_filename || data.filename;
        showToast('Watermark başarıyla kaldırıldı!', 'success');

        // Show before/after preview for images
        if (isImageFile(data.filename)) {
            previewContainer.classList.remove('hidden');
            const beforeImg = document.getElementById('preview-before-img');
            const afterImg = document.getElementById('preview-after-img');
            beforeImg.src = `/api/preview/${currentJobId}?t=${Date.now()}`;
            afterImg.src = `/api/preview-output/${currentJobId}?t=${Date.now()}`;

            // Init comparison slider
            initComparisonSlider();
        } else {
            previewContainer.classList.add('hidden');
        }
    }

    function showError(message) {
        progressSection.classList.add('hidden');
        errorSection.classList.remove('hidden');
        errorSection.style.animation = 'none';
        errorSection.offsetHeight;
        errorSection.style.animation = 'fadeSlideUp 0.5s ease-out';
        errorText.textContent = message;
        showToast(message, 'error');
    }

    // Download
    let currentOutputFilename = '';

    downloadBtn.addEventListener('click', () => {
        if (!currentJobId) return;
        downloadBtn.disabled = true;
        downloadBtn.querySelector('span').textContent = 'İndiriliyor...';

        // Direct window.open with cache-bust — server forces Content-Disposition: attachment
        // This is the most reliable cross-browser download method
        const downloadUrl = `/api/download/${currentJobId}?t=${Date.now()}`;
        window.open(downloadUrl, '_blank');

        setTimeout(() => {
            downloadBtn.disabled = false;
            downloadBtn.querySelector('span').textContent = 'İndir';
        }, 3000);

        showToast('Dosya indiriliyor...', 'success');
    });

    // New File
    newFileBtn.addEventListener('click', resetAll);
    retryBtn.addEventListener('click', resetAll);

    function resetAll() {
        clearFile();
        clearOverlay();
        overlayToggle.checked = false;
        overlayUpload.classList.add('hidden');
        currentJobId = null;

        if (pollInterval) {
            clearTimeout(pollInterval);
            pollInterval = null;
        }

        uploadSection.classList.remove('hidden');
        overlaySection.classList.remove('hidden');
        actionWrap.classList.remove('hidden');
        progressSection.classList.add('hidden');
        resultSection.classList.add('hidden');
        errorSection.classList.add('hidden');
        previewContainer.classList.add('hidden');

        // Re-animate
        uploadSection.style.animation = 'none';
        overlaySection.style.animation = 'none';
        uploadSection.offsetHeight;
        uploadSection.style.animation = 'fadeSlideUp 0.5s ease-out';
        overlaySection.style.animation = 'fadeSlideUp 0.5s ease-out 0.1s both';
    }

    // -------- Comparison Slider --------
    function initComparisonSlider() {
        const slider = document.getElementById('comparison-slider');
        const before = document.getElementById('comparison-before');
        const handle = document.getElementById('comparison-handle');

        if (!slider) return;

        let isDragging = false;

        function updatePosition(x) {
            const rect = slider.getBoundingClientRect();
            let pos = (x - rect.left) / rect.width;
            pos = Math.max(0.02, Math.min(0.98, pos));

            before.style.width = (pos * 100) + '%';
            handle.style.left = (pos * 100) + '%';
        }

        slider.addEventListener('mousedown', (e) => {
            isDragging = true;
            updatePosition(e.clientX);
        });

        window.addEventListener('mousemove', (e) => {
            if (isDragging) {
                e.preventDefault();
                updatePosition(e.clientX);
            }
        });

        window.addEventListener('mouseup', () => {
            isDragging = false;
        });

        // Touch support
        slider.addEventListener('touchstart', (e) => {
            isDragging = true;
            updatePosition(e.touches[0].clientX);
        });

        window.addEventListener('touchmove', (e) => {
            if (isDragging) {
                updatePosition(e.touches[0].clientX);
            }
        });

        window.addEventListener('touchend', () => {
            isDragging = false;
        });

        // Initial position
        updatePosition(slider.getBoundingClientRect().left + slider.getBoundingClientRect().width / 2);
    }

})();
