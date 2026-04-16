/**
 * Download utilities - reusable download functions for the pipeline
 */

// Download a single file
function downloadFile(url, filename) {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || '';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

// Download a single image by ID
function downloadImage(imageId) {
    // Use the lightweight TOC endpoint instead of the full image payload.
    fetch(`/api/images/toc`)
        .then(res => res.json())
        .then(data => {
            const images = Array.isArray(data.items) ? data.items : [];
            const img = images.find(i => i.id === imageId);
            if (img && img.path) {
                downloadFile('/' + img.path, `${imageId}.png`);
                if (typeof showToast === 'function') showToast('Downloading image...');
            } else {
                if (typeof showToast === 'function') showToast('Image not found');
            }
        })
        .catch(err => {
            console.error('Download error:', err);
            if (typeof showToast === 'function') showToast('Download error: ' + err.message);
        });
}

// Download a single 3D model by ID
function downloadModel(modelId) {
    fetch(`/api/models/${modelId}/path`)
        .then(res => res.json())
        .then(model => {
            if (model && model.relative_path) {
                downloadFile('/' + model.relative_path, `${modelId}.glb`);
                if (typeof showToast === 'function') showToast('Downloading model...');
            } else {
                if (typeof showToast === 'function') showToast('Model not found');
            }
        })
        .catch(err => {
            console.error('Download error:', err);
            if (typeof showToast === 'function') showToast('Download error: ' + err.message);
        });
}

// Batch download images as ZIP
function downloadImagesZip(imageIds, btn) {
    const originalText = btn ? btn.textContent : null;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Preparing...';
    }
    
    fetch('/api/download/images', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: imageIds || [] })
    })
    .then(response => {
        if (!response.ok) {
            return response.json().then(data => { throw new Error(data.error || 'Download failed'); });
        }
        return response.blob();
    })
    .then(blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `images_${new Date().toISOString().slice(0,10)}.zip`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();
        if (typeof showToast === 'function') {
            showToast(imageIds && imageIds.length > 0 ? `Downloaded ${imageIds.length} images` : 'Downloaded all images');
        }
    })
    .catch(err => {
        if (typeof showToast === 'function') showToast('Download error: ' + err.message);
    })
    .finally(() => {
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    });
}

// Batch download models as ZIP
function downloadModelsZip(modelIds, includeViews, btn) {
    const originalText = btn ? btn.textContent : null;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Preparing...';
    }
    
    fetch('/api/download/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
            ids: modelIds || [],
            include_views: includeViews || false
        })
    })
    .then(response => {
        if (!response.ok) {
            return response.json().then(data => { throw new Error(data.error || 'Download failed'); });
        }
        return response.blob();
    })
    .then(blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `models_${new Date().toISOString().slice(0,10)}.zip`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();
        if (typeof showToast === 'function') {
            showToast(modelIds && modelIds.length > 0 ? `Downloaded ${modelIds.length} models` : 'Downloaded all models');
        }
    })
    .catch(err => {
        if (typeof showToast === 'function') showToast('Download error: ' + err.message);
    })
    .finally(() => {
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    });
}

// Download all assets as ZIP
function downloadAllAssets(btn) {
    const originalText = btn ? btn.textContent : null;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Preparing ZIP...';
    }
    
    fetch('/api/download/all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    })
    .then(response => {
        if (!response.ok) {
            return response.json().then(data => { throw new Error(data.error || 'Download failed'); });
        }
        return response.blob();
    })
    .then(blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `pipeline_assets_${new Date().toISOString().slice(0,10)}.zip`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();
        if (typeof showToast === 'function') showToast('Downloaded all pipeline assets');
    })
    .catch(err => {
        if (typeof showToast === 'function') showToast('Download error: ' + err.message);
    })
    .finally(() => {
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    });
}
