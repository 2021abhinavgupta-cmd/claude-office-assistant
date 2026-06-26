// ──Projects Feature ────────────────────────────────────────────────────────
let currentProjectId = null;
let currentProject = null;

async function loadProjects() {
  if (!currentUser) return;
  try {
    const res = await fetch(`${API}/api/projects?user_id=${encodeURIComponent(currentUser.user_id)}`);
    const data = await res.json();
    renderProjectsList(data.projects || []);
  } catch (e) {
    document.getElementById("projects-list").innerHTML = "<div class='conv-empty'>Could not load projects.</div>";
  }
}

function renderProjectsList(projects) {
  const list = document.getElementById("projects-list");
  if (!projects.length) {
    list.innerHTML = "<div class='conv-empty'>No projects yet</div>";
    return;
  }
  let html = "";
  projects.forEach(p => {
    html += `
      <div class="conv-item${p.id === currentProjectId ? " active" : ""}" data-id="${p.id}" onclick="openProject('${p.id}')">
        <span class="conv-item-icon"></span>
        <div class="conv-item-body">
          <div class="conv-item-title">${escHtml(p.name)}</div>
        </div>
      </div>`;
  });
  list.innerHTML = html;
}

function showCreateProjectModal() {
  return new Promise((resolve) => {
    let modalOverlay = document.getElementById('create-project-modal-overlay');
    if (!modalOverlay) {
      modalOverlay = document.createElement('div');
      modalOverlay.id = 'create-project-modal-overlay';
      modalOverlay.className = 'claude-modal-overlay';
      
      if (!document.getElementById('claude-modal-styles')) {
        const style = document.createElement('style');
        style.id = 'claude-modal-styles';
        style.innerHTML = `
          .claude-modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.6); display: none; justify-content: center; align-items: center; z-index: 9999; }
          .claude-modal { background: var(--surface2, #2a2a2a); border: 1px solid var(--border, #444); border-radius: 16px; padding: 24px; width: 450px; max-width: 90%; box-shadow: 0 10px 40px rgba(0,0,0,0.5); display: flex; flex-direction: column; gap: 16px; font-family: inherit; }
          .claude-modal-header { font-size: 1.15rem; font-weight: 500; color: #fff; }
          .claude-modal-body input { width: 100%; background: transparent; border: 1px solid var(--border, #555); border-radius: 8px; padding: 12px; color: #fff; font-family: inherit; font-size: 0.95rem; }
          .claude-modal-body input:focus { outline: 1px solid rgba(255,255,255,0.4); }
          .claude-modal-footer { display: flex; justify-content: flex-end; gap: 12px; }
          .btn-cancel { background: transparent; border: none; color: #e0e0e0; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-family: inherit; font-size: 0.9rem; }
          .btn-cancel:hover { background: rgba(255,255,255,0.05); }
          .btn-save { background: #e0e0e0; color: #000; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-weight: 500; font-family: inherit; font-size: 0.9rem; }
          .btn-save:hover { opacity: 0.9; }
        `;
        document.head.appendChild(style);
      }

      modalOverlay.innerHTML = `
        <div class="claude-modal" onclick="event.stopPropagation()">
          <div class="claude-modal-header">Create Project</div>
          <div class="claude-modal-body">
            <input type="text" id="create-project-input" placeholder="Enter project name..." autocomplete="off">
          </div>
          <div class="claude-modal-footer">
            <button class="btn-cancel" id="cp-cancel-btn">Cancel</button>
            <button class="btn-save" id="cp-save-btn">OK</button>
          </div>
        </div>
      `;
      document.body.appendChild(modalOverlay);
    }

    const input = document.getElementById('create-project-input');
    const saveBtn = document.getElementById('cp-save-btn');
    const cancelBtn = document.getElementById('cp-cancel-btn');

    input.value = '';
    modalOverlay.style.display = 'flex';
    setTimeout(() => input.focus(), 50);

    const handleSave = () => {
      resolve(input.value.trim());
      modalOverlay.style.display = 'none';
      cleanup();
    };
    const handleCancel = () => {
      resolve(null);
      modalOverlay.style.display = 'none';
      cleanup();
    };
    const handleKey = (e) => {
      if (e.key === 'Enter') handleSave();
      if (e.key === 'Escape') handleCancel();
    };
    
    saveBtn.addEventListener('click', handleSave);
    cancelBtn.addEventListener('click', handleCancel);
    input.addEventListener('keydown', handleKey);
    modalOverlay.addEventListener('click', handleCancel);

    function cleanup() {
      saveBtn.removeEventListener('click', handleSave);
      cancelBtn.removeEventListener('click', handleCancel);
      input.removeEventListener('keydown', handleKey);
      modalOverlay.removeEventListener('click', handleCancel);
    }
  });
}

const newProjBtn = document.getElementById("new-project-btn");
if (newProjBtn) {
  newProjBtn.addEventListener("click", async () => {
    const name = await showCreateProjectModal();
    if (!name) return;
    try {
      const res = await fetch(`${API}/api/projects`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({user_id: currentUser.user_id, name})
      });
      if (res.ok) {
        loadProjects();
      }
    } catch (e) { showToast("Failed to create project", "error"); }
  });
}

window.openProject = async function(projectId) {
  currentProjectId = projectId;
  currentConvId = null;
  loadProjects(); // update active state
  
  try {
    const res = await fetch(`${API}/api/projects/${projectId}?user_id=${encodeURIComponent(currentUser.user_id)}`);
    if (!res.ok) throw new Error();
    const p = await res.json();
    currentProject = p;
    
    document.getElementById("welcome-screen").classList.add("hidden");
    document.getElementById("chat-view").classList.add("hidden");
    const projView = document.getElementById("project-view");
    if (projView) projView.classList.remove("hidden");
    
    const titleEl = document.getElementById("project-title-header");
    if (titleEl) titleEl.textContent = p.name;
    
    const instEl = document.getElementById("project-custom-instructions");
    if (instEl) instEl.value = p.custom_instructions || "";
    
    renderProjectKb(p.knowledge_base || []);
    
    // Filter chats for this project
    const convRes = await fetch(`${API}/api/conversations?user_id=${encodeURIComponent(currentUser.user_id)}`);
    const cData = await convRes.json();
    const pConvs = (cData.conversations || []).filter(c => c.project_id === projectId);
    
    const chatsList = document.getElementById("project-chats-list");
    if (chatsList) {
      if (!pConvs.length) {
        chatsList.innerHTML = "<div class='conv-empty'>No chats in this project yet.</div>";
      } else {
        chatsList.innerHTML = pConvs.map(c => `
          <div class="conv-item" style="cursor:pointer; background:var(--surface2); border:1px solid var(--border); padding:12px; border-radius:8px; display:flex; justify-content:space-between;" onclick="openConversation('${c.id}')">
            <div>
              <div class="conv-item-title" style="color:var(--text); font-weight:500;"> ${escHtml(c.title)}</div>
              <div class="conv-item-sub" style="margin-top:4px;">${new Date(c.updated_at).toLocaleString()}</div>
            </div>
          </div>
        `).join("");
      }
    }
  } catch (e) {
    showToast("Could not load project", "error");
  }
};

const projInstEl = document.getElementById("project-custom-instructions");
if (projInstEl) {
  projInstEl.addEventListener("blur", async (e) => {
    if (!currentProjectId) return;
    try {
      await fetch(`${API}/api/projects/${currentProjectId}`, {
        method: "PATCH", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({user_id: currentUser.user_id, custom_instructions: e.target.value})
      });
      showToast("Instructions auto-saved", "info");
    } catch (_) { showToast("Failed to save instructions", "error"); }
  });
}

const pNewChatBtn = document.getElementById("project-new-chat-btn");
if (pNewChatBtn) {
  pNewChatBtn.addEventListener("click", () => {
    // Hide project view, start a chat bound to currentProjectId
    const projView = document.getElementById("project-view");
    if (projView) projView.classList.add("hidden");
    startNewChat(null, currentProjectId);
  });
}

const pFileInput = document.getElementById("project-file-input");
if (pFileInput) {
  const kbDrop = document.getElementById("project-kb-upload");
  if (kbDrop) {
    kbDrop.addEventListener("click", () => {
      if (!currentProjectId) {
        showToast("Open a project first", "info");
        return;
      }
      pFileInput.click();
    });

    // Optional: drag/drop a single file into KB
    kbDrop.addEventListener("dragover", (e) => {
      e.preventDefault();
      kbDrop.style.borderColor = "var(--accent)";
      kbDrop.style.background = "rgba(245,166,35,.08)";
    });
    kbDrop.addEventListener("dragleave", () => {
      kbDrop.style.borderColor = "";
      kbDrop.style.background = "";
    });
    kbDrop.addEventListener("drop", (e) => {
      e.preventDefault();
      kbDrop.style.borderColor = "";
      kbDrop.style.background = "";
      if (!currentProjectId) {
        showToast("Open a project first", "info");
        return;
      }
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      // Trigger existing change handler logic by setting files is not possible directly;
      // so we call the upload routine by assigning and dispatching change in a safe way.
      // Most browsers disallow programmatically setting input.files; fallback to click.
      showToast("Drop detected — opening file picker to confirm", "info");
      pFileInput.click();
    });
  }

  pFileInput.addEventListener("change", async (e) => {
    if (!currentProjectId || !e.target.files.length) return;
    const file = e.target.files[0];
    try {
      // 1) Extract text using the same backend pipeline as chat attachments
      const form = new FormData();
      form.append("file", file);
      const up = await fetch(`${API}/api/upload`, { method: "POST", body: form });
      if (!up.ok) {
        const err = await up.json().catch(() => ({}));
        showToast(err.error || "Upload failed", "error");
        return;
      }
      const extracted = await up.json();
      if (!extracted || extracted.type !== "document") {
        showToast("This file type can’t be stored in the Project Knowledge Base yet (needs text extraction).", "info");
        return;
      }

      // 2) Save extracted text into the project KB (bounded to keep prompts sane)
      const content = (extracted.content || "").slice(0, 50000);
      const res = await fetch(`${API}/api/projects/${currentProjectId}/knowledge`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          user_id: currentUser.user_id,
          filename: extracted.filename || file.name,
          content,
        })
      });
      if (res.ok) {
        showToast("Document added to Knowledge Base", "success");
        window.openProject(currentProjectId); // reload
      } else {
        const err = await res.json().catch(() => ({}));
        showToast(err.error || "Failed to save to Knowledge Base", "error");
      }
    } catch (_) {
      showToast("Failed to upload document", "error");
    }
    pFileInput.value = ""; // clear
  });
}

function renderProjectKb(docs) {
  const list = document.getElementById("project-kb-list");
  if (!list) return;
  if (!docs.length) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = docs.map(d => `
    <div style="display:flex; justify-content:space-between; align-items:center; background:var(--surface2); padding:12px 16px; border-radius:8px; border:1px solid var(--border);">
      <div style="color:var(--text)"> ${escHtml(d.filename)}</div>
      <button onclick="deleteProjectKb('${d.id}')" style="background:none; border:none; color:#ef4444; cursor:pointer; font-size:1rem;" title="Delete">✕</button>
    </div>
  `).join("");
}

window.deleteProjectKb = async function(docId) {
  try {
    const res = await fetch(`${API}/api/projects/${currentProjectId}/knowledge/${docId}?user_id=${encodeURIComponent(currentUser.user_id)}`, {
      method: "DELETE"
    });
    if (res.ok) {
      window.openProject(currentProjectId);
    }
  } catch (e) { showToast("Failed to delete document", "error"); }
};

const _oldOpenConversation = window.openConversation;
window.openConversation = async function(convId) {
  currentProjectId = null;
  const projView = document.getElementById("project-view");
  if (projView) projView.classList.add("hidden");
  return _oldOpenConversation ? _oldOpenConversation(convId) : null;
};

