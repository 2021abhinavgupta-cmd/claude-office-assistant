/**
 * project.js — Powers the Claude-style Project Workspace page
 * Handles: loading project data, instructions editor, file upload/delete,
 *          memory display, conversation list, and starting new chats.
 */

const API = (() => {
  const h = window.location.hostname;
  if (!h || h === 'localhost' || h === '127.0.0.1') return 'http://localhost:5000';
  return window.location.origin;
})();

// ── State ─────────────────────────────────────────────────────────────────────
const params     = new URLSearchParams(window.location.search);
const PROJECT_ID = params.get('id');
let   projectData = null;
let   currentUser = null;

// ── DOM refs ──────────────────────────────────────────────────────────────────
const projTitle       = document.getElementById('proj-title');
const projDesc        = document.getElementById('proj-desc');
const projMemory      = document.getElementById('proj-memory-display');
const projConvs       = document.getElementById('proj-convs');
const instDisplay     = document.getElementById('inst-display');
const instInput       = document.getElementById('inst-input');
const instActions     = document.getElementById('inst-actions');
const editInstBtn     = document.getElementById('edit-inst-btn');
const instSaveBtn     = document.getElementById('inst-save-btn');
const instCancelBtn   = document.getElementById('inst-cancel-btn');
const fileDropZone    = document.getElementById('file-drop-zone');
const fileInput       = document.getElementById('file-input');
const fileList        = document.getElementById('file-list');
const projChatInput   = document.getElementById('proj-chat-input');
const projSendBtn     = document.getElementById('proj-send-btn');
const userNameText    = document.getElementById('user-name-text');
const userAvatar      = document.getElementById('user-avatar');

// ── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function loadUserFromStorage() {
  try { return JSON.parse(localStorage.getItem('claude_office_user')) || null; }
  catch { return null; }
}

function showToast(msg, type = 'ok') {
  let t = document.getElementById('proj-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'proj-toast';
    t.style.cssText = 'position:fixed;bottom:24px;right:24px;padding:10px 18px;border-radius:8px;font-size:.85rem;z-index:999;font-family:inherit;transition:opacity .3s;';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.background  = type === 'err' ? 'rgba(255,80,80,0.15)' : 'rgba(34,211,160,0.15)';
  t.style.border      = type === 'err' ? '1px solid #ff5050' : '1px solid #22d3a0';
  t.style.color       = type === 'err' ? '#ff8080' : '#22d3a0';
  t.style.opacity     = '1';
  setTimeout(() => t.style.opacity = '0', 2500);
}

function relativeDate(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  const diff = Math.floor((now - d) / 86400000);
  if (diff < 1)  return 'Today';
  if (diff < 2)  return 'Yesterday';
  if (diff < 7)  return `${diff} days ago`;
  return d.toLocaleDateString();
}

// ── Redirect guard ────────────────────────────────────────────────────────────
if (!PROJECT_ID) {
  window.location.href = 'index.html';
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Load user
  currentUser = loadUserFromStorage();
  if (currentUser) {
    if (userNameText) userNameText.textContent = currentUser.user_name || currentUser.user_id;
    if (userAvatar)   userAvatar.textContent   = (currentUser.user_name || 'U')[0].toUpperCase();
  }

  // Load project
  await loadProject();
  setupInstructions();
  setupFileUpload();
  setupChat();

  // Sidebar: load conversations & projects for nav
  if (typeof loadConversations === 'function') loadConversations();
  if (typeof loadProjects === 'function') {
    loadProjects();
    // Also expand projects panel
    const panel = document.getElementById('projects-list');
    if (panel) panel.style.display = '';
  }
});

// ── Load Project ──────────────────────────────────────────────────────────────
async function loadProject() {
  try {
    const res  = await fetch(`${API}/api/projects/${PROJECT_ID}`);
    const data = await res.json();
    if (!res.ok) { projTitle.textContent = 'Project not found'; return; }

    projectData = data;

    // Title
    projTitle.textContent = data.name || 'Untitled Project';
    document.title = `${data.name} — Claude Office`;

    // Memory
    if (data.memory) {
      projMemory.textContent = data.memory;
    }

    // Instructions
    if (data.instructions) {
      instDisplay.textContent = data.instructions;
    }

    // Files
    renderFiles(data.files || []);

    // Conversations
    renderConversations(data.conversations || []);

  } catch (e) {
    projTitle.textContent = 'Error loading project';
    console.error(e);
  }
}

// ── Conversations ─────────────────────────────────────────────────────────────
function renderConversations(convs) {
  if (!convs.length) {
    projConvs.innerHTML = `<div style="color:var(--muted);font-size:.9rem;margin-top:12px;">This project has no activity yet.</div>`;
    return;
  }
  projConvs.innerHTML = convs.map(c => `
    <div class="conv-item-proj" onclick="openConv('${c.id}')">
      <div>
        <div class="conv-item-proj-title">${escHtml(c.title || 'Untitled Chat')}</div>
        <div class="conv-item-proj-date">${relativeDate(c.updated_at)}</div>
      </div>
    </div>
  `).join('');
}

function openConv(convId) {
  window.location.href = `index.html?conv_id=${convId}`;
}

// ── Instructions ──────────────────────────────────────────────────────────────
function setupInstructions() {
  editInstBtn.addEventListener('click', () => {
    instInput.value   = projectData?.instructions || '';
    instInput.style.display   = 'block';
    instActions.style.display = 'flex';
    instDisplay.style.display = 'none';
    instInput.focus();
  });

  instCancelBtn.addEventListener('click', () => {
    instInput.style.display   = 'none';
    instActions.style.display = 'none';
    instDisplay.style.display = '';
  });

  instSaveBtn.addEventListener('click', async () => {
    const text = instInput.value.trim();
    instSaveBtn.textContent = 'Saving...';
    instSaveBtn.disabled    = true;
    try {
      const res = await fetch(`${API}/api/projects/${PROJECT_ID}/instructions`, {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ instructions: text })
      });
      if (res.ok) {
        projectData.instructions    = text;
        instDisplay.textContent     = text || 'Add instructions to tailor Claude\'s responses';
        instInput.style.display     = 'none';
        instActions.style.display   = 'none';
        instDisplay.style.display   = '';
        showToast('Instructions saved!');
      } else {
        showToast('Failed to save', 'err');
      }
    } catch {
      showToast('Error saving instructions', 'err');
    } finally {
      instSaveBtn.textContent = 'Save';
      instSaveBtn.disabled    = false;
    }
  });
}

// ── File Upload ───────────────────────────────────────────────────────────────
function setupFileUpload() {
  fileDropZone.addEventListener('click', () => fileInput.click());

  fileDropZone.addEventListener('dragover', e => {
    e.preventDefault();
    fileDropZone.classList.add('dragover');
  });
  fileDropZone.addEventListener('dragleave', () => fileDropZone.classList.remove('dragover'));
  fileDropZone.addEventListener('drop', e => {
    e.preventDefault();
    fileDropZone.classList.remove('dragover');
    uploadFiles(e.dataTransfer.files);
  });

  fileInput.addEventListener('change', () => uploadFiles(fileInput.files));
}

async function uploadFiles(files) {
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    form.append('project_id', PROJECT_ID);
    try {
      const res  = await fetch(`${API}/api/projects/${PROJECT_ID}/files`, { method: 'POST', body: form });
      const data = await res.json();
      if (res.ok) {
        showToast(`✅ ${file.name} uploaded`);
        await loadProject();
      } else {
        showToast(data.error || 'Upload failed', 'err');
      }
    } catch {
      showToast('Upload error', 'err');
    }
  }
}

function renderFiles(files) {
  if (!files.length) {
    fileList.innerHTML = '';
    return;
  }
  fileList.innerHTML = files.map(f => `
    <div style="display:flex;align-items:center;justify-content:space-between;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-size:.85rem;margin-top:6px;">
      <span>📄 ${escHtml(f.filename)}</span>
      <button onclick="deleteFile('${f.id}')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:1rem;" title="Remove">✕</button>
    </div>
  `).join('');
}

async function deleteFile(fileId) {
  if (!confirm('Remove this file from the project?')) return;
  try {
    const res = await fetch(`${API}/api/projects/${PROJECT_ID}/files/${fileId}`, { method: 'DELETE' });
    if (res.ok) { showToast('File removed'); await loadProject(); }
    else showToast('Failed to remove file', 'err');
  } catch { showToast('Error', 'err'); }
}

// ── Chat (start new conv scoped to this project) ──────────────────────────────
function setupChat() {
  // Auto-expand textarea
  projChatInput.addEventListener('input', () => {
    projChatInput.style.height = 'auto';
    projChatInput.style.height = projChatInput.scrollHeight + 'px';
  });

  projChatInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      startChat();
    }
  });

  projSendBtn.addEventListener('click', startChat);
}

async function startChat() {
  const msg = projChatInput.value.trim();
  if (!msg) return;
  if (!currentUser) { showToast('Please log in first', 'err'); return; }

  projSendBtn.disabled = true;
  projSendBtn.textContent = '...';

  try {
    // Create a new conversation scoped to this project
    const res = await fetch(`${API}/api/conversations`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        user_id:    currentUser.user_id,
        project_id: PROJECT_ID,
        title:      msg.slice(0, 60)
      })
    });
    const data = await res.json();
    if (res.ok && data.id) {
      // Redirect to index.html with this conv + the message as a draft
      window.location.href = `index.html?conv_id=${data.id}&draft=${encodeURIComponent(msg)}`;
    } else {
      showToast(data.error || 'Could not start chat', 'err');
    }
  } catch {
    showToast('Error starting chat', 'err');
  } finally {
    projSendBtn.disabled = false;
    projSendBtn.textContent = 'Send';
  }
}
