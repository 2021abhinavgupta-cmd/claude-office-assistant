// ──Projects Feature ────────────────────────────────────────────────────────
let currentProjectId = null;
let currentProject = null;

async function loadProjects() {
  if (!currentUser) return;
  try {
    const res = await fetch(`${API}/api/projects?user_id=${encodeURIComponent(currentUser.user_id)}`);
    const data = await res.json();
    window.allProjectsData = data.projects || [];
    renderAllProjectsGrid(window.allProjectsData);
  } catch (e) {
    const grid = document.getElementById("projects-grid");
    if (grid) grid.innerHTML = `<div style="color:var(--red);">Could not load projects.</div>`;
  }
}

function renderAllProjectsGrid(projects) {
  const grid = document.getElementById("projects-grid");
  if (!grid) return;
  
  if (!projects.length) {
    grid.innerHTML = `<div style="color:var(--muted); font-size:0.95rem;">No projects yet. Create one to get started.</div>`;
    return;
  }
  
  grid.innerHTML = projects.map(p => {
    const date = new Date(p.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    return `
      <a href="#" onclick="openProject('${p.id}'); return false;" style="display:flex; flex-direction:column; justify-content:space-between; background:var(--surface2); border:1px solid rgba(255,255,255,0.05); border-radius:12px; padding:24px; text-decoration:none; color:var(--text); min-height:160px; transition:border-color 0.2s, background 0.2s;" onmouseover="this.style.background='var(--surface)'; this.style.borderColor='var(--border)'" onmouseout="this.style.background='var(--surface2)'; this.style.borderColor='rgba(255,255,255,0.05)'">
        <div>
          <h3 style="font-size:1.1rem; font-weight:600; margin-bottom:8px;">${escHtml(p.name)}</h3>
          ${p.custom_instructions ? `<p style="font-size:0.85rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical;">${escHtml(p.custom_instructions)}</p>` : ''}
        </div>
        <div style="font-size:0.8rem; color:var(--text-2); margin-top:16px;">
          Updated ${date}
        </div>
      </a>
    `;
  }).join("");
}

window.showAllProjectsView = function() {
  document.getElementById("welcome-screen")?.classList.add("hidden");
  document.getElementById("chat-view")?.classList.add("hidden");
  document.getElementById("project-view")?.classList.add("hidden");
  
  const allProjView = document.getElementById("all-projects-view");
  if (allProjView) {
    allProjView.classList.remove("hidden");
  }
  loadProjects();
};

document.addEventListener("DOMContentLoaded", () => {
  const searchInput = document.getElementById("projects-search-input");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      const q = e.target.value.toLowerCase();
      if (!q) {
        renderAllProjectsGrid(window.allProjectsData || []);
        return;
      }
      const filtered = (window.allProjectsData || []).filter(p => p.name.toLowerCase().includes(q) || (p.custom_instructions || "").toLowerCase().includes(q));
      renderAllProjectsGrid(filtered);
    });
  }
  
  const mainNewProjBtn = document.getElementById("main-new-proj-btn");
  if (mainNewProjBtn) {
    mainNewProjBtn.addEventListener("click", () => {
      const modal = document.getElementById("project-modal");
      if (modal) {
        modal.style.display = "flex";
        modal.classList.remove("hidden");
        const nameInput = document.getElementById("project-name-input");
        if (nameInput) {
          nameInput.value = "";
          setTimeout(() => nameInput.focus(), 50);
        }
        const descInput = document.getElementById("project-desc-input");
        if (descInput) descInput.value = "";
      }
    });
  }
  
  // Wire up modal logic from index.html
  const modal = document.getElementById("project-modal");
  const cancelBtn = document.getElementById("project-cancel-btn");
  const createBtn = document.getElementById("project-create-btn");
  const nameInput = document.getElementById("project-name-input");
  const descInput = document.getElementById("project-desc-input");
  
  if (cancelBtn && modal) {
    cancelBtn.addEventListener("click", () => {
      modal.style.display = "none";
      modal.classList.add("hidden");
    });
  }
  
  if (createBtn) {
    createBtn.addEventListener("click", async () => {
      const name = nameInput.value.trim();
      if (!name) return;
      
      createBtn.disabled = true;
      createBtn.textContent = "Creating...";

      try {
        const res = await fetch(`${API}/api/projects`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: currentUser.user_id,
            name: name,
            custom_instructions: descInput.value.trim()
          })
        });
        if (!res.ok) throw new Error("Failed to create project");
        const p = await res.json();
        modal.style.display = "none";
        modal.classList.add("hidden");
        openProject(p.id);
      } catch (e) {
        console.error(e);
        showToast("Error creating project", "error");
      } finally {
        createBtn.disabled = false;
        createBtn.textContent = "Create project";
      }
    });
  }
});

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
    
    document.getElementById("welcome-screen")?.classList.add("hidden");
    document.getElementById("chat-view")?.classList.add("hidden");
    document.getElementById("all-projects-view")?.classList.add("hidden");
    const projView = document.getElementById("project-view");
    if (projView) projView.classList.remove("hidden");
    
    const titleEl = document.getElementById("project-title-header");
    if (titleEl) titleEl.textContent = p.name;
    
    const descEl = document.getElementById("project-desc-header");
    if (descEl) descEl.textContent = p.custom_instructions || "";
    
    const instEl = document.getElementById("project-custom-instructions");
    if (instEl) instEl.value = p.custom_instructions || "";
    
    renderProjectKb(p.knowledge_base || []);
    
    // Focus the chat input
    const pInput = document.getElementById("project-chat-input");
    if (pInput) setTimeout(() => pInput.focus(), 50);

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

// --- New SPA Logic for Project Dashboard ---

// Chat Input logic
const pChatInput = document.getElementById("project-chat-input");
const pChatSendBtn = document.getElementById("project-chat-send-btn");

if (pChatInput) {
  pChatInput.addEventListener("input", function() {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
    if (pChatSendBtn) pChatSendBtn.disabled = !this.value.trim();
  });

  pChatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!pChatSendBtn.disabled) pChatSendBtn.click();
    }
  });
}

if (pChatSendBtn) {
  pChatSendBtn.addEventListener("click", () => {
    const text = pChatInput.value.trim();
    if (!text || !currentProjectId) return;
    
    // Hide project view, trigger a new chat bound to this project, and pre-fill the prompt
    document.getElementById("project-view")?.classList.add("hidden");
    
    // We can simulate starting a new chat
    startNewChat(null, currentProjectId).then(() => {
       const mainInput = document.getElementById("msg-input");
       const mainSendBtn = document.getElementById("send-btn");
       if (mainInput && mainSendBtn) {
         mainInput.value = text;
         mainSendBtn.disabled = false;
         mainSendBtn.click();
       }
    });
    
    pChatInput.value = "";
    pChatInput.style.height = "auto";
  });
}

// Accordion Toggles
const toggleInstBtn = document.getElementById("toggle-instructions-btn");
const instExpanded = document.getElementById("project-instructions-expanded");
const instIcon = document.getElementById("instructions-icon");

if (toggleInstBtn && instExpanded && instIcon) {
  toggleInstBtn.addEventListener("click", () => {
    instExpanded.classList.toggle("hidden");
    instIcon.textContent = instExpanded.classList.contains("hidden") ? "+" : "−";
  });
}

const toggleFilesBtn = document.getElementById("toggle-files-btn");
const filesExpanded = document.getElementById("project-files-expanded");
const filesIcon = document.getElementById("files-icon");
const filesSubtitle = document.getElementById("files-subtitle");

if (toggleFilesBtn && filesExpanded && filesIcon) {
  toggleFilesBtn.addEventListener("click", () => {
    filesExpanded.classList.toggle("hidden");
    filesIcon.textContent = filesExpanded.classList.contains("hidden") ? "+" : "−";
    if (filesSubtitle) filesSubtitle.style.display = filesExpanded.classList.contains("hidden") ? "block" : "none";
  });
}

