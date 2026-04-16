/**
 * Common utilities - shared functions for the pipeline
 * Loaded by base.html, available on all pages
 */

// ============ Command Modal Functions ============
function showCommandModal(title, message, command) {
    document.getElementById('commandModalTitle').textContent = title;
    document.getElementById('commandModalMessage').innerHTML = message;
    document.getElementById('commandModalCode').textContent = command;
    document.getElementById('commandModal').style.display = 'flex';
}

function closeCommandModal() {
    document.getElementById('commandModal').style.display = 'none';
}

function copyCommand(btn) {
    const code = document.getElementById('commandModalCode').textContent;
    const targetBtn = btn || document.querySelector('#commandModal button[onclick^="copyCommand"]');
    const originalLabel = targetBtn ? targetBtn.textContent : null;

    copyTextToClipboard(code)
        .then(() => {
            showToast('Command copied to clipboard');
            if (targetBtn) {
                targetBtn.textContent = 'Copied';
                setTimeout(() => { targetBtn.textContent = originalLabel || 'Copy'; }, 1200);
            }
        })
        .catch((err) => {
            console.error('Copy error:', err);
            showToast('Copy failed, please select and copy manually');
            if (targetBtn) {
                targetBtn.textContent = 'Copy Failed';
                setTimeout(() => { targetBtn.textContent = originalLabel || 'Copy'; }, 1200);
            }
        });
}

// ============ Model Path Display Functions ============
function showModelPath(modelId) {
    fetch(`/api/models/${modelId}/path`)
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                showToast('Error: ' + data.error);
                return;
            }
            document.getElementById('modelPathModalTitle').textContent = `Server Path - ${data.model_id}`;
            document.getElementById('modelAbsolutePath').value = data.absolute_path;
            document.getElementById('modelRelativePath').value = data.relative_path;
            document.getElementById('modelFilename').value = data.filename;
            document.getElementById('modelPathModal').style.display = 'flex';
        })
        .catch(err => {
            showToast('Error loading path: ' + err.message);
        });
}

function closeModelPathModal() {
    document.getElementById('modelPathModal').style.display = 'none';
}

function copyToClipboard(inputId) {
    const input = document.getElementById(inputId);
    if (!input) {
        showToast('Input element not found');
        return;
    }
    copyTextToClipboard(input.value)
        .then(() => showToast('Copied to clipboard!'))
        .catch(() => showToast('Copy failed'));
}

// ============ Delete Asset Function ============
function deleteAsset(assetId, options) {
    const defaults = {
        confirmMessage: 'Delete this asset AND all related assets?',
        onSuccess: null
    };
    const opts = Object.assign({}, defaults, options);

    if (!confirm(opts.confirmMessage)) return;
    if (!confirm('This action is irreversible. Confirm delete?')) return;

    fetch(`/api/assets/${assetId}`, { method: 'DELETE' })
        .then(async (res) => {
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
            showToast('Deleted successfully');

            if (opts.onSuccess) {
                opts.onSuccess();
            } else {
                const card = document.getElementById('card-' + assetId);
                if (card) {
                    card.style.transition = 'opacity 0.3s, transform 0.3s';
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.9)';
                    setTimeout(() => card.remove(), 300);
                }
                const tocItem = document.querySelector(`#tocList .toc-item[data-id="${assetId}"]`);
                if (tocItem) tocItem.remove();
                if (typeof updateSelectedCount === 'function') updateSelectedCount();
            }
        })
        .catch(err => showToast('Error: ' + err.message));
}

// ============ Enhanced escapeHtml ============
function escapeHtmlEnhanced(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML
        .replace(/"/g, '&quot;')
        .replace(/`/g, '&#96;')
        .replace(/\$/g, '&#36;');
}