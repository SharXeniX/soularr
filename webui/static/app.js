let autoScroll = true;
const log = document.getElementById('log');
const scrollBtn = document.getElementById('scroll-btn');

const cmEditor = CodeMirror.fromTextArea(document.getElementById('config-editor'), {
    mode: 'properties',
    theme: 'soularr',
    lineNumbers: true,
    indentWithTabs: false,
    lineWrapping: false,
    extraKeys: { Tab: false },
});

const mobileQuery = window.matchMedia('(pointer: coarse) and (hover: none), (max-width: 768px)');

function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('.sidebar-overlay');
    const opening = !sidebar.classList.contains('open');
    sidebar.classList.toggle('open', opening);
    overlay.classList.toggle('visible', opening);
}

function closeSidebar() {
    document.querySelector('.sidebar').classList.remove('open');
    document.querySelector('.sidebar-overlay').classList.remove('visible');
}

const VIEW_NAMES = ['logs', 'failed-imports', 'orphans', 'settings'];

function _findNavBtn(name) {
    const buttons = Array.from(document.querySelectorAll('.nav-btn'));
    return buttons.find(b => (b.getAttribute('onclick') || '').includes("'" + name + "'"));
}

function showView(name, btn) {
    if (!VIEW_NAMES.includes(name)) name = 'logs';
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('view-' + name).classList.add('active');
    const targetBtn = btn || _findNavBtn(name);
    if (targetBtn) targetBtn.classList.add('active');

    // Reflect the current view in the URL so refresh / bookmarks / back-button
    // restore the same page.
    const desired = name === 'logs' ? '/' : '/' + name;
    if (window.location.pathname !== desired) {
        history.pushState({view: name}, '', desired);
    }

    if (name === 'settings') {
        loadConfig();
        cmEditor.refresh();
    }
    if (name === 'failed-imports') {
        loadFailedImports();
    }
    if (name === 'orphans') {
        loadOrphans();
    }
    if (mobileQuery.matches) closeSidebar();
}

// Restore view based on the URL on initial load and on back/forward navigation.
function _viewFromPath() {
    const seg = (window.location.pathname || '/').replace(/^\/+|\/+$/g, '');
    return VIEW_NAMES.includes(seg) ? seg : 'logs';
}

window.addEventListener('popstate', () => {
    const name = _viewFromPath();
    // Don't push another history entry while reacting to popstate
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('view-' + name).classList.add('active');
    const btn = _findNavBtn(name);
    if (btn) btn.classList.add('active');
    if (name === 'settings') { loadConfig(); cmEditor.refresh(); }
    if (name === 'failed-imports') { loadFailedImports(); }
    if (name === 'orphans') { loadOrphans(); }
});

// Initial restore on page load — but only if it's not the default 'logs'
// (default markup already has Logs active).
document.addEventListener('DOMContentLoaded', () => {
    const initial = _viewFromPath();
    if (initial !== 'logs') {
        showView(initial);
    }
});

function loadConfig() {
    fetch('/api/config')
        .then(r => r.json())
        .then(data => {
            document.getElementById('config-path').textContent = data.path;
            cmEditor.setValue(data.content);
            if (!data.exists) {
                cmEditor.setOption('placeholder', 'Config file not found at the path above.');
            }
        });
}

function saveConfig() {
    const btn = document.getElementById('save-btn');
    const content = cmEditor.getValue();
    btn.disabled = true;

    let dotCount = 1;
    btn.textContent = 'Saving.';
    btn.classList.add('saving');
    const dotAnim = setInterval(() => {
        if (dotCount < 3) {
            dotCount++;
            btn.textContent = 'Saving' + '.'.repeat(dotCount);
        }
    }, 400);

    const minDelay = new Promise(resolve => setTimeout(resolve, 1200));
    const request = fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content })
    }).then(r => r.json());

    Promise.all([request, minDelay])
        .then(([data]) => {
            clearInterval(dotAnim);
            btn.classList.remove('saving');
            btn.textContent = data.ok ? 'Saved' : 'Error';
            btn.classList.toggle('active', data.ok);
            btn.classList.toggle('save-error', !data.ok);
            setTimeout(() => {
                btn.textContent = 'Save';
                btn.disabled = false;
                btn.classList.remove('active', 'save-error');
            }, 1000);
        })
        .catch(() => {
            clearInterval(dotAnim);
            btn.classList.remove('saving');
            btn.textContent = 'Error';
            btn.classList.add('save-error');
            setTimeout(() => {
                btn.textContent = 'Save';
                btn.disabled = false;
                btn.classList.remove('save-error');
            }, 1000);
        });
}

function toggleScroll() {
    autoScroll = !autoScroll;
    scrollBtn.classList.toggle('active', autoScroll);
    if (autoScroll) log.scrollTop = log.scrollHeight;
}

function clearLog() {
    fetch('/api/log/clear', { method: 'POST' })
        .then(r => r.json())
        .then(() => {
            log.innerHTML = '';
            // Force the EventSource to reconnect so the new (post-offset)
            // stream starts from the just-cleared point. Without this, the
            // existing connection would keep sending the lines it already had
            // queued upstream of the new offset.
            try { es.close(); } catch (e) {}
            es = new EventSource('/stream');
            es.onmessage = e => appendLine(e.data);
            es.onerror = () => appendLine('--- connection lost, retrying... ---');
        })
        .catch(err => console.error('Clear failed', err));
}

const LOG_FONT_MIN = 4;
const LOG_FONT_MAX = 28;
let logFontSize = parseFloat(localStorage.getItem('soularr-log-font-size') || (mobileQuery.matches ? '7' : '12'));

function applyLogZoom() {
    log.style.fontSize = logFontSize + 'px';
    localStorage.setItem('soularr-log-font-size', logFontSize);
}

log.addEventListener('wheel', e => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    logFontSize = Math.min(LOG_FONT_MAX, Math.max(LOG_FONT_MIN, logFontSize + (e.deltaY > 0 ? -0.5 : 0.5)));
    applyLogZoom();
}, { passive: false });

let lastPinchDist = null;
log.addEventListener('touchstart', e => {
    if (e.touches.length === 2)
        lastPinchDist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
}, { passive: true });
log.addEventListener('touchmove', e => {
    if (e.touches.length !== 2 || lastPinchDist === null) return;
    e.preventDefault();
    const dist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
    logFontSize = Math.min(LOG_FONT_MAX, Math.max(LOG_FONT_MIN, logFontSize * (dist / lastPinchDist)));
    lastPinchDist = dist;
    applyLogZoom();
}, { passive: false });
log.addEventListener('touchend', () => { lastPinchDist = null; });

applyLogZoom();

function classify(line) {
    if (line.includes('[ERROR|'))   return 'level-error';
    if (line.includes('[WARNING|')) return 'level-warn';
    if (line.includes('[DEBUG|'))   return 'level-debug';
    return 'level-info';
}

const lineQueue = [];
let flushScheduled = false;

function flushQueue() {
    if (lineQueue.length === 0) {
        flushScheduled = false;
        return;
    }
    const fragment = document.createDocumentFragment();
    while (lineQueue.length > 0) {
        const text = lineQueue.shift();
        const div = document.createElement('div');
        div.className = 'log-line ' + classify(text);
        div.textContent = text;
        fragment.appendChild(div);
    }
    log.appendChild(fragment);
    if (autoScroll) log.scrollTop = log.scrollHeight;
    flushScheduled = false;
}

function appendLine(text) {
    lineQueue.push(text);
    if (!flushScheduled) {
        flushScheduled = true;
        requestAnimationFrame(flushQueue);
    }
}

function loadFailedImports() {
    fetch('/api/failed-imports')
        .then(r => r.json())
        .then(data => {
            const tbody = document.getElementById('failed-imports-body');
            const empty = document.getElementById('failed-imports-empty');
            const count = document.getElementById('failed-imports-count');
            tbody.innerHTML = '';
            if (!Array.isArray(data) || data.length === 0) {
                empty.classList.remove('hidden');
                count.textContent = '';
            } else {
                empty.classList.add('hidden');
                count.textContent = `${data.length} entr${data.length === 1 ? 'y' : 'ies'}`;
                data.forEach(entry => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${entry.artist || '—'}</td>
                        <td>
                            <div>${entry.title || '—'}</div>
                            <button class="toolbar-btn remove-btn fi-remove-mobile" onclick="removeFailedImport(${entry.album_id})">Delete</button>
                        </td>
                        <td>
                            <div class="failed-imports-date-cell">
                                <span class="failed-imports-date">${entry.failed_at || '—'}</span>
                                <span class="failed-imports-sep"></span>
                                <button class="toolbar-btn remove-btn fi-remove-desktop" onclick="removeFailedImport(${entry.album_id})">Delete</button>
                            </div>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        });
}

function removeFailedImport(albumId) {
    fetch(`/api/failed-imports/${albumId}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(() => loadFailedImports());
}

// ---------------------------------------------------------------------------
// Orphans
// ---------------------------------------------------------------------------

const ORPHAN_STATUS_LABEL = {
    pending: 'Pending',
    partial_imported: 'Partial',
    no_match: 'No match',
    error: 'Error',
    ignored: 'Ignored',
    empty: 'Empty',
    deleted: 'Deleted',
};

function loadOrphans() {
    const filter = document.getElementById('orphan-filter').value;
    fetch('/api/orphans')
        .then(r => r.json())
        .then(data => {
            const tbody = document.getElementById('orphans-body');
            const empty = document.getElementById('orphans-empty');
            const count = document.getElementById('orphans-count');
            tbody.innerHTML = '';
            const items = (Array.isArray(data) ? data : []).filter(it => filter === 'all' || it.status === filter);
            if (items.length === 0) {
                empty.classList.remove('hidden');
                count.textContent = '';
                return;
            }
            empty.classList.add('hidden');
            count.textContent = `${items.length} entr${items.length === 1 ? 'y' : 'ies'}`;
            items.forEach(it => {
                const tr = document.createElement('tr');
                const id = it.id || '';
                const folder = it.folder_path || '';
                const label = ORPHAN_STATUS_LABEL[it.status] || it.status;
                const audioCount = it.audio_file_count || 0;
                const folderExists = it.folder_exists;

                const artist = it.artist || '—';
                const baseAlbum = it.album_title
                    ? (it.year ? `${it.album_title} (${it.year})` : it.album_title)
                    : (it.matched_album_id ? `#${it.matched_album_id}` : '—');
                const format = it.format || '—';

                // "Already in Lidarr" annotation
                let inLibNote = '';
                const tfc = it.track_file_count || 0;
                const tt = it.total_tracks || 0;
                if (tfc > 0) {
                    const qualities = (it.existing_qualities || []).join(', ') || 'unknown';
                    inLibNote = `<div class="orphan-inlib" title="Lidarr already has ${tfc}/${tt} tracks in quality: ${qualities}">In Lidarr: ${tfc}/${tt} ${qualities}</div>`;
                }

                const rejections = it.rejections || [];
                const rejectionText = rejections.length === 0
                    ? '<span class="orphan-rejection-empty">—</span>'
                    : rejections.map(r => `<div class="orphan-rejection">${r}</div>`).join('');

                const forceTooltip = 'Force: include files Lidarr would normally reject for soft reasons (Has missing tracks, Album match too low, quality below profile…). Hard rejections like "Already imported" are not overridden.';
                const folderTip = `${folder} — status: ${label}`;

                const statusBadge = `<span class="orphan-status orphan-status-${it.status}">${label}</span>`;
                tr.dataset.orphanId = id;
                tr.innerHTML = `
                    <td class="orphan-clickable" onclick="previewOrphan('${id}')" title="${folderTip}">${statusBadge} ${artist}</td>
                    <td class="orphan-clickable" onclick="previewOrphan('${id}')" title="${folderTip}">${baseAlbum}${inLibNote}</td>
                    <td class="orphan-clickable" onclick="previewOrphan('${id}')" title="${folderTip}">${format}</td>
                    <td class="orphan-clickable" onclick="previewOrphan('${id}')" title="${folderTip}">${audioCount}${folderExists ? '' : ' <em>(folder gone)</em>'}</td>
                    <td class="orphan-clickable" onclick="previewOrphan('${id}')" title="${folderTip}">${rejectionText}</td>
                    <td>
                        <div class="row-actions">
                            <button class="toolbar-btn" onclick="importOrphan('${id}', false, this)" ${audioCount === 0 ? 'disabled' : ''}>Import</button>
                            <button class="toolbar-btn" title="${forceTooltip}" onclick="importOrphan('${id}', true, this)" ${audioCount === 0 ? 'disabled' : ''}>Force</button>
                            <span class="actions-divider"></span>
                            <button class="toolbar-btn" onclick="rescanOrphan('${id}', this)">Re-scan</button>
                            <button class="toolbar-btn" onclick="ignoreOrphan('${id}', this)">Ignore</button>
                            <button class="toolbar-btn remove-btn" onclick="deleteOrphan('${id}', this)">Delete</button>
                        </div>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        })
        .catch(err => console.error('loadOrphans failed', err));
}

function _orphanAction(orphanId, endpoint, body) {
    const payload = Object.assign({ id: orphanId }, body || {});
    return fetch(`/api/orphans/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json());
}

function importOrphan(id, force, btn) {
    _withButtonBusy(btn, force ? 'Forcing…' : 'Importing…', () =>
        _orphanAction(id, 'import', { force: !!force })
    ).then(r => {
        if (r.error) {
            alert(`Import error: ${r.error}`);
        } else if (r.imported_count > 0) {
            alert(`Imported ${r.imported_count} of ${r.accepted_count} files`);
        } else {
            alert(`No files imported. ${r.message || ''}\nTry "Force" to override soft rejections.`);
        }
        loadOrphans();
    });
}

function _withButtonBusy(btn, label, fn) {
    if (!btn) return fn();
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = label;
    return fn().finally(() => {
        btn.disabled = false;
        btn.textContent = orig;
    });
}

function ignoreOrphan(id, btn) {
    _withButtonBusy(btn, 'Ignoring…', () => _orphanAction(id, 'ignore'))
        .then(() => loadOrphans());
}

function rescanOrphan(id, btn) {
    _withButtonBusy(btn, 'Scanning…', () => _orphanAction(id, 'rescan'))
        .then(r => {
            if (r && r.error) alert('Re-scan failed: ' + r.error);
            loadOrphans();
        });
}

function deleteOrphan(id, btn) {
    if (!confirm('Delete the orphan folder from disk? This removes all files in the folder.')) return;
    _withButtonBusy(btn, 'Deleting…', () => _orphanAction(id, 'delete'))
        .then(() => loadOrphans());
}

function previewOrphan(id) {
    const body = document.getElementById('orphan-modal-body');
    body.innerHTML = '<em>Loading…</em>';
    document.getElementById('orphan-modal-title').textContent = '';
    document.getElementById('orphan-detail-modal').classList.remove('hidden');
    fetch('/api/orphans/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                body.innerHTML = `<div class="level-error">Preview error: ${data.error}</div>`;
                return;
            }
            if (data.folder_path) {
                document.getElementById('orphan-modal-title').textContent = data.folder_path;
            }
            const files = data.files || [];
            if (files.length === 0) {
                body.innerHTML = '<em>Lidarr returned no candidates for this folder.</em>';
                return;
            }
            files.sort((a, b) => {
                const an = (a.name || '').split('/').pop().toLowerCase();
                const bn = (b.name || '').split('/').pop().toLowerCase();
                return an.localeCompare(bn);
            });
            const rows = files.map(f => {
                const rej = (f.rejections || []).map(r => r.reason || r).join('; ');
                const album = f.album ? f.album.title : '—';
                const quality = f.quality && f.quality.quality ? f.quality.quality.name : '—';
                return `<tr>
                    <td>${(f.name || '').split('/').pop()}</td>
                    <td>${quality}</td>
                    <td>${album}</td>
                    <td class="${rej ? 'level-warn' : ''}">${rej || '<span class="level-info">accepted</span>'}</td>
                </tr>`;
            }).join('');
            body.innerHTML = `<table class="data-table">
                <thead><tr><th>File</th><th>Quality</th><th>Album</th><th>Rejections</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
        });
}

function closeOrphanModal() {
    document.getElementById('orphan-detail-modal').classList.add('hidden');
}

const es = new EventSource('/stream');
es.onmessage = e => appendLine(e.data);
es.onerror = () => appendLine('--- connection lost, retrying... ---');
