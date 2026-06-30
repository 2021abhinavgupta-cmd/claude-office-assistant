/**
 * Agency Portal Assistant — Multi-user, multi-conversation frontend
 * Each employee gets their own set of conversations.
 * Full System-like experience with task auto-detection and persistent history.
 */

// Auto-detect API base — handles localhost, file://, and production
const API = (() => {
  const h = window.location.hostname;
  // file:// protocol has empty hostname; also cover localhost variants
  if (!h || h === 'localhost' || h === '127.0.0.1') return 'http://localhost:5000';
  return window.location.origin;
})();

// ── State ────────────────────────────────────────────────────────────────────
let currentUser   = null;   // { user_id, user_name }
let currentConvId = null;   // active conversation ID
let isLoading     = false;

const TASK_ICONS = {
  coding: "⌨", html_design: "", presentations: "",
  captions: "✍", scripts: "", general: "",
};
const TASK_MODELS = {
  coding: "sonnet", html_design: "sonnet", presentations: "sonnet",
  captions: "haiku", scripts: "haiku", general: "haiku",
};
/** Max characters in main chat input (HTML maxlength). API limit is ~200k tokens total context, not this number. */
const MSG_MAX_CHARS = 500000;

// ── DOM refs ─────────────────────────────────────────────────────────────────

const newChatBtn     = document.getElementById("new-chat-btn");
const convList       = document.getElementById("conv-list");
const userPill       = document.getElementById("user-pill");
const userAvatar     = document.getElementById("user-avatar");
const userNameText   = document.getElementById("user-name-text");
const budgetMiniVal  = document.getElementById("budget-mini-val");
const budgetMiniFill = document.getElementById("budget-mini-fill");
const welcomeScreen  = document.getElementById("welcome-screen");
const chatView       = document.getElementById("chat-view");
const messagesEl     = document.getElementById("messages");
const msgInput       = document.getElementById("msg-input");
const sendBtn        = document.getElementById("send-btn");
const charCount      = document.getElementById("char-count");
const convTitleHeader = document.getElementById("conv-title-header");
const taskChip       = document.getElementById("task-chip");
const modelChip      = document.getElementById("model-chip");
const connStatus     = document.getElementById("conn-status");
const metaTask       = document.getElementById("meta-task");
const metaModel      = document.getElementById("meta-model");
const toastContainer = document.getElementById("toast-container");
const sidebarToggle  = document.getElementById("sidebar-toggle");
const sidebar        = document.getElementById("sidebar");
const welcomeInput   = document.getElementById("welcome-input");
const welcomeSend    = document.getElementById("welcome-send");

// ── Init ─────────────────────────────────────────────────────────────────────
// ── User Editing & Action Buttons ──────────────────────────────────────────
window.copyMessage = function(btn) {
  const text = btn.dataset.text || btn.getAttribute('data-text');
  if (!text) return;
  const decoded = text.replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
  navigator.clipboard.writeText(decoded).then(() => {
    const orig = btn.innerHTML;
    btn.innerHTML = " Copied";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.innerHTML = orig;
      btn.classList.remove("copied");
    }, 2000);
  });
};

window.dislikeMessage = function(btn) {
  btn.style.color = "var(--error)";
  btn.innerHTML = " Marked Bad";
  showToast("Feedback submitted. Thank you!", "info");
};

window.editMessage = function(btn) {
  const msgEl = btn.closest(".msg");
  if (!msgEl) return;
  const textContainer = msgEl.querySelector(".msg-text-container");
  const msgTextEl = msgEl.querySelector(".msg-text");
  const rawText = msgTextEl.dataset.raw ? msgTextEl.dataset.raw.replace(/&quot;/g, '"') : msgTextEl.innerText;

  // Create an edit textarea
  const textarea = document.createElement("textarea");
  textarea.className = "msg-edit-textarea";
  textarea.style.width = "100%";
  textarea.style.minHeight = "60px";
  textarea.style.padding = "8px";
  textarea.style.background = "var(--bg)";
  textarea.style.color = "var(--text)";
  textarea.style.border = "1px solid var(--border)";
  textarea.style.borderRadius = "4px";
  textarea.style.marginTop = "8px";
  textarea.style.fontFamily = "inherit";
  textarea.style.fontSize = "inherit";
  textarea.value = rawText;

  // Create action buttons
  const actionsDiv = document.createElement("div");
  actionsDiv.style.display = "flex";
  actionsDiv.style.gap = "8px";
  actionsDiv.style.marginTop = "8px";
  
  const saveBtn = document.createElement("button");
  saveBtn.textContent = "Save & Submit";
  saveBtn.className = "primary-btn";
  saveBtn.style.padding = "4px 12px";
  saveBtn.style.fontSize = "0.85rem";
  saveBtn.onclick = () => {
    const newText = textarea.value.trim();
    if (!newText) return;
    const idx = parseInt(msgEl.dataset.index);
    sendMessage(newText, idx);
  };

  const cancelBtn = document.createElement("button");
  cancelBtn.textContent = "Cancel";
  cancelBtn.className = "secondary-btn";
  cancelBtn.style.padding = "4px 12px";
  cancelBtn.style.fontSize = "0.85rem";
  cancelBtn.onclick = () => {
    textContainer.innerHTML = "";
    textContainer.appendChild(msgTextEl);
  };

  actionsDiv.appendChild(saveBtn);
  actionsDiv.appendChild(cancelBtn);

  // Replace text with editor
  textContainer.innerHTML = "";
  textContainer.appendChild(textarea);
  textContainer.appendChild(actionsDiv);
  textarea.focus();
};

document.addEventListener("DOMContentLoaded", () => {
  checkConnection();
  fetchBudget();

  const saved = loadUserFromStorage();
  if (saved) {

    currentUser = saved;
    applyUser(saved);
    loadConversations();
    setInterval(loadConversations, 5000); // Live poll for new huddle invites
    loadProjects();
    
    // Check for draft message from project.html
    const urlParams = new URLSearchParams(window.location.search);
    const convIdParam = urlParams.get("conv_id");
    const draftParam = urlParams.get("draft");

    if (convIdParam) {
      openConversation(convIdParam).then(() => {
        if (draftParam) {
          window.history.replaceState({}, document.title, window.location.pathname);
          msgInput.value = draftParam;
          sendMessage(draftParam);
        }
      });
    } else {
      showWelcomeScreen();
    }
  }

  setupInputs();
  setupSidebar();

  // Keyboard shortcut: Ctrl+K = new chat
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "k") {
      e.preventDefault();
      startNewChat();
    }
  });
});


function selectUser(userId, userName) {
  currentUser = { user_id: userId, user_name: userName };
  saveUserToStorage(currentUser);
  applyUser(currentUser);
  loadConversations();
  if (typeof loadProjects === "function") loadProjects();
}

function applyUser(user) {
  const displayName = user.user_name || user.name || "?";
  if (userAvatar) userAvatar.textContent = displayName.charAt(0).toUpperCase();
  if (userNameText) userNameText.textContent = displayName;
  
  const greetingEl = document.getElementById("welcome-greeting-text");
  if (greetingEl) {
    const hour = new Date().getHours();
    let greeting = "Good evening";
    if (hour < 12) greeting = "Good morning";
    else if (hour < 17) greeting = "Good afternoon";
    greetingEl.textContent = `${greeting}, ${displayName}`;
  }}

if (userPill) {
  userPill.addEventListener("click", () => {
    window.location.href = "login.html";
  });
}

// ── Project Management ────────────────────────────────────────────────────────
const projectModal = document.getElementById("project-modal");
const newProjectBtn = document.getElementById("new-project-btn");
const projectCancelBtn = document.getElementById("project-cancel-btn");
const projectCreateBtn = document.getElementById("project-create-btn");
const projectNameInput = document.getElementById("project-name-input");
const projectDescInput = document.getElementById("project-desc-input");
const projectsList = document.getElementById("projects-list");

if (newProjectBtn && projectModal) {
  newProjectBtn.addEventListener("click", () => {
    projectModal.style.display = "flex";
    projectModal.classList.remove("hidden");
    projectNameInput.value = "";
    projectDescInput.value = "";
    projectNameInput.focus();
  });

  projectCancelBtn.addEventListener("click", () => {
    projectModal.style.display = "none";
  });

  projectCreateBtn.addEventListener("click", async () => {
    const name = projectNameInput.value.trim();
    const desc = projectDescInput.value.trim();
    if (!name) {
      alert("Project name is required.");
      return;
    }
    projectCreateBtn.disabled = true;
    projectCreateBtn.textContent = "Creating...";
    try {
      const res = await fetch(`${API}/api/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: currentUser.user_id,
          name: name,
          description: desc
        })
      });
      const data = await res.json();
      if (res.ok) {
        window.location.href = `project.html?id=${data.id}`;
      } else {
        alert(data.error || "Failed to create project");
      }
    } catch (e) {
      alert("Error creating project.");
    } finally {
      projectCreateBtn.disabled = false;
      projectCreateBtn.textContent = "Create project";
    }
  });
}

async function loadProjects() {
  if (!currentUser || !projectsList) return;
  try {
    const res = await fetch(`${API}/api/projects?user_id=${encodeURIComponent(currentUser.user_id)}`);
    const data = await res.json();
    const projs = data.projects || [];
    
    if (projs.length === 0) {
      projectsList.innerHTML = `<div class="conv-empty">No projects yet</div>`;
      return;
    }
    
    projectsList.innerHTML = projs.map(p => `
      <a href="project.html?id=${p.id}" class="conv-item" style="text-decoration:none;">
        <span class="conv-item-icon"></span>
        <div class="conv-item-body">
          <div class="conv-item-title">${escHtml(p.name)}</div>
        </div>
      </a>
    `).join("");
    
  } catch (e) {
    projectsList.innerHTML = `<div class="conv-empty">Could not load projects</div>`;
  }
}

// ──Projects Panel Toggle (called from HTML onclick) ─────────────────────────
window.toggleProjectsPanel = function() {
  const list    = document.getElementById('projects-list');
  const chevron = document.getElementById('projects-chevron');
  if (!list) return;
  const isHidden = list.style.display === 'none' || list.style.display === '';
  list.style.display = isHidden ? 'block' : 'none';
  if (chevron) chevron.style.transform = isHidden ? 'rotate(0deg)' : 'rotate(-90deg)';
};

window.filterChats = function(q) {
  document.querySelectorAll('#conv-list .conv-item').forEach(el => {
    const title = el.querySelector('.conv-item-title');
    if (!title) return;
    el.style.display = title.textContent.toLowerCase().includes(q.toLowerCase()) ? '' : 'none';
  });
};


// ── Conversation Management ───────────────────────────────────────────────────
async function loadConversations() {
  if (!currentUser) return;
  try {
    const res  = await fetch(`${API}/api/conversations?user_id=${encodeURIComponent(currentUser.user_id)}`);
    const data = await res.json();
    renderConvList(data.conversations || []);
  } catch (_) {
    convList.innerHTML = "<div class='conv-empty'>Could not load conversations.</div>";
  }
}

function renderConvList(convs) {
  if (!convs.length) {
    convList.innerHTML = "<div class='conv-empty'>No chats yet.<br>Click <strong>New Chat</strong> to begin.</div>";
    return;
  }

  // Group by date
  const groups = { "Today": [], "Yesterday": [], "Earlier": [] };
  const now = new Date();
  convs.forEach(c => {
    const d   = new Date(c.updated_at);
    const diffDays = Math.floor((now - d) / 86400000);
    if (diffDays < 1)     groups["Today"].push(c);
    else if (diffDays < 2) groups["Yesterday"].push(c);
    else                   groups["Earlier"].push(c);
  });

  let html = "";
  for (const [label, items] of Object.entries(groups)) {
    if (!items.length) continue;
    html += `<div class="conv-group-label">${label}</div>`;
    items.forEach(c => {
      const icon    = TASK_ICONS[c.task_type] || "";
      const isActive = c.id === currentConvId ? " active" : "";
      const msgCount = c.message_count || "";
      
      let badges = "";
      if (c.project_name) badges += ` <span style="background:var(--accent);color:#000;border-radius:4px;padding:0 4px;font-size:10px;margin-left:4px;">${escHtml(c.project_name)}</span>`;
      if (c.client_name) badges += ` <span style="background:#10b981;color:#000;border-radius:4px;padding:0 4px;font-size:10px;margin-left:4px;">${escHtml(c.client_name)}</span>`;
      
      html += `
        <div class="conv-item${isActive}" data-id="${c.id}">
          <span class="conv-item-icon">${icon}</span>
          <div class="conv-item-body">
            <div class="conv-item-title">${escHtml(c.title)}</div>
            <div class="conv-item-sub">${c.task_type || "general"}${msgCount ? ` · ${msgCount} msgs` : ""}${badges}</div>
          </div>
          <button class="conv-del" data-id="${c.id}" title="Delete">✕</button>
        </div>`;
    });
  }
  convList.innerHTML = html;

  // Bind click events
  convList.querySelectorAll(".conv-item").forEach(item => {
    item.addEventListener("click", e => {
      if (e.target.classList.contains("conv-del")) return;
      if (e.target.classList.contains("conv-item-title") && e.detail === 2) return; // handled by dblclick
      openConversation(item.dataset.id);
    });
    // Double-click title to inline rename
    const titleEl = item.querySelector(".conv-item-title");
    titleEl.addEventListener("dblclick", e => {
      e.stopPropagation();
      const convId = item.dataset.id;
      const input = document.createElement("input");
      input.value = titleEl.textContent;
      input.className = "conv-rename-input";
      input.style.cssText = "background:var(--surface3);border:1px solid var(--accent);border-radius:4px;padding:2px 6px;font-size:inherit;color:var(--text);width:100%;outline:none;";
      titleEl.replaceWith(input);
      input.focus();
      input.select();
      const save = async () => {
        const newTitle = input.value.trim();
        if (newTitle) {
          await fetch(`${API}/api/conversations/${convId}/title`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({title: newTitle})
          });
          if (currentConvId === convId) convTitleHeader.textContent = newTitle;
        }
        loadConversations();
      };
      input.addEventListener("blur", save);
      input.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { loadConversations(); }
      });
    });
  });
  convList.querySelectorAll(".conv-del").forEach(btn => {
    btn.addEventListener("click", () => deleteConversation(btn.dataset.id));
  });
}

async function openConversation(convId) {
  currentConvId = convId;
  showChatView();

  // Close any existing huddle SSE connection
  if (window._huddleSSE) { window._huddleSSE.close(); window._huddleSSE = null; }

  try {
    const res  = await fetch(`${API}/api/conversations/${convId}`);
    const conv = await res.json();

    convTitleHeader.textContent = conv.title;
    
    // UI updates for project chat header
    const projNameEl = document.getElementById("chat-project-name");
    const projSepEl = document.getElementById("chat-title-separator");
    if (conv.project_id && window.getProjectNameById) {
       const pName = window.getProjectNameById(conv.project_id);
       if (projNameEl && projSepEl) {
          projNameEl.textContent = pName || "Project";
          projNameEl.style.display = "inline";
          projSepEl.style.display = "inline";
       }
    } else {
       if (projNameEl) projNameEl.style.display = "none";
       if (projSepEl) projSepEl.style.display = "none";
    }

    const task  = conv.task_type || "general";
    const model = TASK_MODELS[task] || "haiku";
    updateHeaderChips(task, model);
    updateInputMeta(task, model);

    // ── Huddle participant bar ────────────────────────────────────────────
    let participantBar = document.getElementById("huddle-bar");
    if (!participantBar) {
      participantBar = document.createElement("div");
      participantBar.id = "huddle-bar";
      participantBar.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 16px;background:rgba(255,255,255,0.04);border-bottom:1px solid var(--bdr);font-size:0.78rem;flex-wrap:wrap;";
      messagesEl.parentElement.insertBefore(participantBar, messagesEl);
    }
    const participants = conv.participant_ids || [conv.user_id];
    const names = conv.participant_names || {};
    const isHuddle = participants.length > 1;
    participantBar.style.display = participants.length > 0 ? "flex" : "none";
    participantBar.innerHTML = `
      <span style="color:var(--muted);margin-right:4px;">${isHuddle ? " Huddle:" : ""}</span>
      ${participants.map(uid => `<span style="background:var(--surface2);border-radius:12px;padding:2px 10px;color:var(--txt);">${names[uid] || uid}</span>`).join("")}
      <button onclick="openHuddleInvite('${convId}')" title="Invite to Huddle" style="margin-left:auto;background:var(--accent);color:#000;border:none;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:0.75rem;font-weight:600;">+ Invite</button>
    `;

    // Render existing messages
    messagesEl.innerHTML = "";
    (conv.messages || []).forEach(m => {
      appendMessage(m.role, m.content, {
        model_tier: m.model_tier,
        cost_usd:   m.cost_usd,
        model_used: m.model_used,
        sender_id:  m.sender_id,
        sender_name: m.sender_name
      });
    });
    scrollToBottom();

    // Mark active in sidebar
    document.querySelectorAll(".conv-item").forEach(el => {
      el.classList.toggle("active", el.dataset.id === convId);
    });

    // ── Connect live SSE if this is a huddle ─────────────────────────────
    if (isHuddle) {
      _connectHuddleSSE(convId);
    }
  } catch (e) {
    showToast("Could not load conversation", "error");
  }
}

function _connectHuddleSSE(convId) {
  const sse = new EventSource(`${API}/api/conversations/${convId}/huddle-events`);
  window._huddleSSE = sse;

  sse.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      if (evt.type === "message" && evt.role && evt.content) {
        // Only append if not our own message (avoid duplicates)
        if (evt.role === "assistant") {
          if (evt.trigger_user_id && evt.trigger_user_id === (currentUser?.user_id || "")) return;
          appendMessage(evt.role, evt.content, { sender_name: evt.sender, sender_id: evt.sender_id });
          scrollToBottom();
        } else if (evt.sender_id && evt.sender_id !== (currentUser?.user_id || "")) {
          appendMessage(evt.role, evt.content, { sender_name: evt.sender, sender_id: evt.sender_id });
          scrollToBottom();
        }
      } else if (evt.type === "joined") {
        showToast(`${evt.user_name} joined the huddle `, "success");
        openConversation(convId); // Refresh participant bar
      }
    } catch (_) {}
  };

  sse.onerror = () => {
    // Silently reconnect after 3s
    sse.close();
    setTimeout(() => { if (currentConvId === convId) _connectHuddleSSE(convId); }, 3000);
  };
}

async function openHuddleInvite(convId) {
  try {
    const res = await fetch(`${API}/api/employees`);
    const { employees } = await res.json();

    const opts = employees
      .filter(e => e.id !== currentUser?.user_id)
      .map(e => `<option value="${e.id}" data-name="${e.name}">${e.name}</option>`)
      .join("");

    const modal = document.createElement("div");
    modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;";
    modal.innerHTML = `
      <div style="background:var(--surface);border:1px solid var(--bdr);border-radius:14px;padding:28px;min-width:320px;max-width:400px;">
        <h3 style="margin:0 0 16px;font-size:1rem;"> Invite to Huddle</h3>
        <select id="huddle-invite-sel" style="width:100%;padding:10px;border-radius:8px;background:var(--surface2);border:1px solid var(--bdr);color:var(--txt);outline:none;">
          <option value="">-- Select teammate --</option>
          ${opts}
        </select>
        <div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end;">
          <button onclick="this.closest('div').parentElement.remove()" style="padding:8px 16px;border-radius:8px;border:1px solid var(--bdr);background:transparent;color:var(--txt);cursor:pointer;">Cancel</button>
          <button id="huddle-invite-btn" style="padding:8px 16px;border-radius:8px;border:none;background:var(--accent);color:#000;cursor:pointer;font-weight:600;">Invite</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    document.getElementById("huddle-invite-btn").onclick = async () => {
      const sel = document.getElementById("huddle-invite-sel");
      const uid = sel.value;
      const uname = sel.options[sel.selectedIndex]?.dataset?.name;
      if (!uid) return;
      await fetch(`${API}/api/conversations/${convId}/invite`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: uid, user_name: uname })
      });
      modal.remove();
      showToast(`${uname} invited to huddle! `, "success");
      openConversation(convId);
    };
  } catch (e) {
    showToast("Could not load employees", "error");
  }
}


async function startNewChat(initialMessage = null, projectId = null) {
  if (!currentUser) { showToast("Please select a user first", "error"); return; }

  try {
    const res  = await fetch(`${API}/api/conversations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: currentUser.user_id, user_name: currentUser.user_name, project_id: projectId }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`HTTP ${res.status}: ${errText}`);
    }
    const conv = await res.json();
    currentConvId = conv.id;

    await loadConversations();  // Refresh sidebar
    showChatView();
    convTitleHeader.textContent = "New conversation";
    messagesEl.innerHTML = "";
    updateHeaderChips("general", "haiku");
    updateInputMeta("general", "claude-haiku-4-5");

    let participantBar = document.getElementById("huddle-bar");
    if (!participantBar) {
      participantBar = document.createElement("div");
      participantBar.id = "huddle-bar";
      participantBar.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 16px;background:rgba(255,255,255,0.04);border-bottom:1px solid var(--bdr);font-size:0.78rem;flex-wrap:wrap;";
      messagesEl.parentElement.insertBefore(participantBar, messagesEl);
    }
    participantBar.style.display = "flex";
    participantBar.innerHTML = `
      <span style="color:var(--muted);margin-right:4px;"></span>
      <span style="background:var(--surface2);border-radius:12px;padding:2px 10px;color:var(--txt);">${currentUser.user_name}</span>
      <button onclick="openHuddleInvite('${conv.id}')" title="Invite to Huddle" style="margin-left:auto;background:var(--accent);color:#000;border:none;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:0.75rem;font-weight:600;">+ Invite</button>
    `;

    if (initialMessage) {
      msgInput.value = initialMessage;
      await sendMessage();
    } else {
      msgInput.focus();
    }
  } catch (e) {
    console.error("Chat creation error:", e);
    showToast("Could not create conversation. Please refresh the page.", "error");
  }
}

async function deleteConversation(convId) {
  try {
    await fetch(`${API}/api/conversations/${convId}`, { method: "DELETE" });
    if (currentConvId === convId) {
      currentConvId = null;
      showWelcomeScreen();
    }
    loadConversations();
    showToast("Conversation deleted", "success");
  } catch (_) {
    showToast("Could not delete conversation", "error");
  }
}

newChatBtn.addEventListener("click", () => {
  currentProjectId = null;
  const projView = document.getElementById("project-view");
  if (projView) projView.classList.add("hidden");
  if (typeof loadProjects === "function") loadProjects();
  startNewChat();
});

// ── Sending Messages ──────────────────────────────────────────────────────────
// The full streaming sendMessage is defined below (window.sendMessage).
// These event bindings use arrow-function wrappers so they resolve
// window.sendMessage at call-time rather than at script-parse-time.
sendBtn.addEventListener("click", () => sendMessage());
msgInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// ── Welcome Screen Actions ────────────────────────────────────────────────────
async function sendWelcomeMessage() {
  const text = welcomeInput.value.trim();
  if (!text || !currentUser) return;
  welcomeInput.value = "";
  toggleWelcomeSend();
  await startNewChat(text);
}

welcomeSend.addEventListener("click", sendWelcomeMessage);
welcomeInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendWelcomeMessage(); }
});
welcomeInput.addEventListener("input", () => {
  autoResize(welcomeInput);
  toggleWelcomeSend();
});

function toggleWelcomeSend() {
  welcomeSend.disabled = !welcomeInput.value.trim();
}

// Removed Quick-start cards

// ── UI Helpers ────────────────────────────────────────────────────────────────
function showChatView() {
  // Always hide project view when entering chat
  const projView = document.getElementById("project-view");
  if (projView) projView.classList.add("hidden");
  welcomeScreen.classList.add("hidden");
  chatView.classList.remove("hidden");
  
  // Move file-chips to chat view
  const fileChips = document.getElementById("file-chips");
  const inputWrap = document.getElementById("input-wrap");
  if (fileChips && inputWrap) {
    const topRow = inputWrap.querySelector(".input-top-row");
    if (topRow) inputWrap.insertBefore(fileChips, topRow);
  }
}
function showWelcomeScreen() {
  // Always hide project view when going to welcome
  const projView = document.getElementById("project-view");
  if (projView) projView.classList.add("hidden");
  chatView.classList.add("hidden");
  welcomeScreen.classList.remove("hidden");
  
  const greetingEl = document.getElementById("welcome-greeting-text");
  if (greetingEl && currentUser) {
    const hour = new Date().getHours();
    let greeting = "Good evening";
    if (hour < 12) greeting = "Good morning";
    else if (hour < 17) greeting = "Good afternoon";
    const name = currentUser.user_name || currentUser.name || "User";
    greetingEl.textContent = `${greeting}, ${name}`;
  }

  // Move file-chips to welcome screen
  const fileChips = document.getElementById("file-chips");
  const welcomeInputWrap = document.getElementById("welcome-input-wrap");
  if (fileChips && welcomeInputWrap) {
    const topRow = welcomeInputWrap.querySelector(".input-top-row");
    if (topRow) welcomeInputWrap.insertBefore(fileChips, topRow);
  }
}

// ToggleProjects panel in sidebar
window.toggleProjectsPanel = function() {
  const list = document.getElementById("projects-list");
  const chevron = document.getElementById("projects-chevron");
  if (!list) return;
  const isOpen = list.style.display !== "none";
  list.style.display = isOpen ? "none" : "block";
  if (chevron) chevron.style.transform = isOpen ? "rotate(-90deg)" : "rotate(0deg)";
};

function updateHeaderChips(task, modelTier) {
  if (taskChip) taskChip.textContent  = task.replace(/_/g, " ");
  if (modelChip) {
    modelChip.textContent = modelTier;
    modelChip.className   = `model-chip ${modelTier}`;
  }
}
function updateInputMeta(taskName, modelName) {
  if (metaTask) metaTask.textContent = taskName.replace(/_/g, " ");
  if (metaModel) metaModel.textContent = modelName || "claude-haiku-4-5";
}

// ── Message Rendering ─────────────────────────────────────────────────────────
function appendMessage(role, content, meta = {}) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  // Assign index based on current number of messages in DOM
  const msgIndex = Array.from(messagesEl.querySelectorAll('.msg')).length;
  el.dataset.index = msgIndex;

  const senderName = meta.sender_name || (role === "user" ? (currentUser ? currentUser.user_name : "You") : "System");
  
  const avatar = role === "assistant" ? `<div class="msg-avatar" style="font-size: 0.8rem; font-weight: bold; background: var(--surface2); border: 1px solid var(--border);">OPs</div>` : ``;
  const name = senderName;

  const metaHtml = (role === "assistant" && (meta.model_tier || meta.cost_usd))
    ? `<div class="msg-meta">
        ${meta.model_used ? `<span class="msg-chip ${meta.model_tier}">${meta.model_used}</span>` : ""}
        ${meta.cost_usd   ? `<span class="msg-chip cost">$${meta.cost_usd.toFixed(5)} <span style="opacity:0.7">(₹${(meta.cost_usd * 83.5).toFixed(4)})</span></span>` : ""}
       </div>`
    : "";

  const actionsBtn = role === "assistant"
    ? `<div class="msg-actions">
         <button class="msg-action-btn copy-btn" onclick="copyMessage(this)" data-text="${escHtml(content).replace(/"/g,'&quot;')}" title="Copy"> Copy</button>
         <button class="msg-action-btn dislike-btn" onclick="dislikeMessage(this)" title="Bad Response"> Dislike</button>
       </div>`
    : `<div class="msg-actions">
         <button class="msg-action-btn edit-btn" onclick="editMessage(this)" title="Edit Message">✏ Edit</button>
       </div>`;

  const nameHtml = role === "user" 
    ? `<div class="msg-name" style="text-align: right; font-size: 0.8rem; color: var(--text-2); margin-bottom: 4px; margin-right: 4px;">${name}</div>`
    : ``;
  el.innerHTML = `
    ${avatar}
    <div class="msg-body">
      ${nameHtml}
      <div class="msg-text-container">
        <div class="msg-text" data-raw="${escHtml(content).replace(/"/g,'&quot;')}">${formatText(content)}</div>
      </div>
      ${metaHtml}
      ${actionsBtn}
    </div>`;

  messagesEl.appendChild(el);
  return el;
}

function appendErrorMessage(text) {
  const el = document.createElement("div");
  el.className = "msg error";
  el.innerHTML = `
    <div class="msg-avatar">⚠</div>
    <div class="msg-body">
      <div class="msg-name">Error</div>
      <div class="msg-text">${escHtml(text)}</div>
    </div>`;
  messagesEl.appendChild(el);
}

function appendTyping() {
  const id = "t" + Date.now();
  const el = document.createElement("div");
  el.className = "msg assistant typing-indicator";
  el.id = id;
  el.innerHTML = `
    <div class="msg-avatar" style="font-size: 0.8rem; font-weight: bold; background: var(--surface2); border: 1px solid var(--border);">OPs</div>
    <div class="msg-body">
      <div class="msg-name">System</div>
      <div class="msg-text">
        <div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>
      </div>
    </div>`;
  messagesEl.appendChild(el);
  return id;
}
function removeTyping(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

// ──Usage ────────────────────────────────────────────────────────────────────
async function fetchBudget() {
  try {
    const res  = await fetch(`${API}/api/budget`);
    const data = await res.json();
    updateBudgetUI(data);
  } catch (_) {}
}

function updateBudgetUI(b) {
  if (!b) return;
  const spent = b.total_spent_ever ?? b.spent ?? b.monthly_spend ?? 0;
  budgetMiniVal.textContent = `$${spent.toFixed(2)} (₹${(spent*83.5).toFixed(2)})`;
  
  const limit = b.budget_limit || 150;
  const pct = Math.min(100, Math.max(0, (spent / limit) * 100));
  
  budgetMiniFill.style.display = "block";
  budgetMiniFill.style.width = `${pct}%`;
  budgetMiniFill.style.background = pct >= 90 ? "var(--red, #ef4444)" : pct >= 75 ? "var(--accent, #f97316)" : "var(--accent, #d97757)";
}

// ── Connection Check ──────────────────────────────────────────────────────────
async function checkConnection() {
  try {
    const res = await fetch(`${API}/api/health`, { signal: AbortSignal.timeout(4000) });
    if (res.ok) {
      connStatus.innerHTML = `<span class="conn-dot ok"></span><span>Connected</span>`;
      connStatus.style.color = "var(--haiku-color)";
    } else throw new Error();
  } catch (_) {
    connStatus.innerHTML = `<span class="conn-dot error"></span><span>Offline</span>`;
    connStatus.style.color = "#ef4444";
  }
}

// ── Sidebar Toggle ────────────────────────────────────────────────────────────
function setupSidebar() {
  const toggles = [
    document.getElementById("sidebar-toggle"),
    document.getElementById("sidebar-toggle-btn"),
    document.getElementById("welcome-sidebar-toggle")
  ];
  
  toggles.forEach(btn => {
    if (btn) {
      btn.addEventListener("click", () => {
        const isMobile = window.innerWidth <= 768;
        sidebar.classList.toggle(isMobile ? "open" : "closed");
      });
    }
  });
}

// ── Input Setup ───────────────────────────────────────────────────────────────
function setupInputs() {
  msgInput.addEventListener("input", () => {
    autoResize(msgInput);
    const len = msgInput.value.length;
    const warnAt = Math.floor(MSG_MAX_CHARS * 0.75);
    charCount.textContent = len > warnAt ? `${len} / ${MSG_MAX_CHARS} characters` : "";
    charCount.style.color = len > Math.floor(MSG_MAX_CHARS * 0.875) ? "#ef4444" : "";
    sendBtn.disabled = !msgInput.value.trim() || isLoading;

    const popup = document.getElementById("slash-popup");
    if (popup) {
      if (msgInput.value.trim() === "/") {
        popup.style.display = "block";
      } else {
        popup.style.display = "none";
      }
    }
  });
}

window.applySlashCommand = function(cmd) {
  msgInput.value = cmd + " ";
  const popup = document.getElementById("slash-popup");
  if (popup) popup.style.display = "none";
  msgInput.focus();
};

function autoResize(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function setLoading(v) {
  isLoading = v;
  sendBtn.disabled = v || !msgInput.value.trim();
  msgInput.disabled = v;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function showToast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  toastContainer.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ── localStorage ──────────────────────────────────────────────────────────────
function saveUserToStorage(user) {
  try { localStorage.setItem("agency_portal_user", JSON.stringify(user)); } catch (_) {}
}
function loadUserFromStorage() {
  try {
    const raw = localStorage.getItem("agency_portal_user");
    if (!raw) return null;
    const u = JSON.parse(raw);
    // Normalize field names: login.html saves 'name'+'id', app.js expects 'user_name'+'user_id'
    if (u.name && !u.user_name) u.user_name = u.name;
    if (u.id   && !u.user_id)  u.user_id   = u.id;
    // Only return if we have a usable identity
    return (u.user_name && u.user_id) ? u : null;
  } catch (_) { return null; }
}

// ══════════════════════════════════════════════════════════════════════════════
// FILE UPLOAD
// ══════════════════════════════════════════════════════════════════════════════
let pendingAttachments = [];  // [{type, filename, content|data, media_type, size}]

const fileInput   = document.getElementById("file-input");
// Fall back to the new menu-upload item if old upload-btn element no longer exists
const uploadBtn   = document.getElementById("upload-btn") || document.getElementById("menu-upload");
const fileChips   = document.getElementById("file-chips");
const dragOverlay = document.getElementById("drag-overlay");

// Trigger hidden file input
uploadBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  [...fileInput.files].forEach(uploadFile);
  fileInput.value = "";
});

// Drag and drop on the whole page
document.addEventListener("dragover", e => { e.preventDefault(); dragOverlay.classList.remove("hidden"); });
document.addEventListener("dragleave", e => {
  if (!e.relatedTarget) dragOverlay.classList.add("hidden");
});
document.addEventListener("drop", e => {
  e.preventDefault();
  dragOverlay.classList.add("hidden");
  [...e.dataTransfer.files].forEach(uploadFile);
});

// Paste handling for images/files
document.addEventListener("paste", e => {
  const items = (e.clipboardData || window.clipboardData).items;
  if (!items) return;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.kind === 'file') {
      const blob = item.getAsFile();
      if (blob) {
        let name = "pasted_image_" + Math.random().toString(36).slice(2, 8);
        if (blob.type === "image/png") name += ".png";
        else if (blob.type === "image/jpeg") name += ".jpg";
        else if (blob.type === "image/webp") name += ".webp";
        else if (blob.type === "image/gif") name += ".gif";
        else name = "pasted_file_" + Math.random().toString(36).slice(2, 8);
        const file = new File([blob], name, { type: blob.type });
        uploadFile(file);
      }
    }
  }
});

async function compressImage(file, maxSizeMB = 4.5) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        
        let width = img.width;
        let height = img.height;
        const maxDim = 2048; // Safe dimension for System API
        
        if (width > maxDim || height > maxDim) {
          if (width > height) {
            height = Math.round((height * maxDim) / width);
            width = maxDim;
          } else {
            width = Math.round((width * maxDim) / height);
            height = maxDim;
          }
        }
        
        canvas.width = width;
        canvas.height = height;
        ctx.drawImage(img, 0, 0, width, height);
        
        canvas.toBlob((blob) => {
          if (blob) {
            resolve(new File([blob], file.name.replace(/\.[^/.]+$/, ".jpg"), {
              type: "image/jpeg",
              lastModified: Date.now(),
            }));
          } else {
            resolve(file);
          }
        }, "image/jpeg", 0.85);
      };
      img.onerror = () => resolve(file);
      img.src = e.target.result;
    };
    reader.onerror = () => resolve(file);
    reader.readAsDataURL(file);
  });
}

async function uploadFile(originalFile) {
  let file = originalFile;
  
  if (file.type.startsWith("image/") && file.size > 4.5 * 1024 * 1024) {
    try {
      file = await compressImage(file);
    } catch (e) {
      console.warn("Image compression failed", e);
    }
  }

  const chipId = "chip_" + Date.now() + Math.random().toString(36).slice(2);
  addFileChip(chipId, file.name, formatBytes(file.size), "uploading", file);

  const form = new FormData();
  form.append("file", file);

  try {
    const res  = await fetch(`${API}/api/upload`, { method: "POST", body: form });
    const data = await res.json();

    if (!res.ok || !data.success) {
      updateChipError(chipId, data.error || "Upload failed");
      showToast(` ${file.name}: ${data.error || "Upload failed"}`, "error");
      return;
    }

    // Store processed attachment
    const att = {
      type:       data.type,
      filename:   data.filename,
      size_bytes: data.size_bytes || file.size,
    };
    if (data.type === "image") {
      att.media_type = data.media_type;
      att.data       = data.data;
    } else {
      att.content = data.content;
    }
    pendingAttachments.push(att);

    const icon = data.type === "image" ? "" : getFileIcon(file.name);
    updateChipReady(chipId, att, icon);
  } catch (err) {
    updateChipError(chipId, "Server unreachable");
    showToast(" Upload failed: server offline", "error");
  }
}

function addFileChip(id, name, size, state, fileObj = null) {
  const el = document.createElement("div");
  el.className = `file-chip ${state}`;
  el.id = id;
  
  if (fileObj && fileObj.type && fileObj.type.startsWith("image/")) {
    const url = URL.createObjectURL(fileObj);
    el.classList.add("is-image-chip");
    el.innerHTML = `
      <div class="file-chip-img-wrap" style="position: relative; width: 64px; height: 64px;">
        <img src="${url}" style="width: 100%; height: 100%; object-fit: cover; border-radius: var(--radius-md); border: 1px solid var(--border);" />
        <button class="file-chip-remove" data-id="${id}" title="Remove" style="position: absolute; top: -6px; left: -6px; background: var(--surface2); color: var(--text-2); border: 1px solid var(--border); border-radius: 50%; width: 20px; height: 20px; font-size: 10px; display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0;">✕</button>
      </div>`;
    el.style.padding = "0";
    el.style.border = "none";
    el.style.background = "none";
  } else {
    el.innerHTML = `
      <span class="file-chip-icon">⏳</span>
      <span class="file-chip-name">${escHtml(name)}</span>
      <span class="file-chip-size">${size}</span>`;
  }
  fileChips.appendChild(el);
  const rmBtn = el.querySelector(".file-chip-remove");
  if (rmBtn) rmBtn.addEventListener("click", () => el.remove());
}

function updateChipReady(id, att, icon) {
  const el = document.getElementById(id);
  if (!el) return;
  
  if (el.classList.contains("is-image-chip")) {
    el.className = `file-chip is-image-chip ${att.type}`;
    const rmBtn = el.querySelector(".file-chip-remove");
    if (rmBtn) {
      const newBtn = rmBtn.cloneNode(true);
      rmBtn.replaceWith(newBtn);
      newBtn.addEventListener("click", () => removeChip(id, att));
    }
  } else {
    el.className = `file-chip ${att.type}`;
    el.innerHTML = `
      <span class="file-chip-icon">${icon}</span>
      <span class="file-chip-name">${escHtml(att.filename)}</span>
      <span class="file-chip-size">${formatBytes(att.size_bytes || 0)}</span>
      <button class="file-chip-remove" data-id="${id}" title="Remove">✕</button>`;
    el.querySelector(".file-chip-remove").addEventListener("click", () => removeChip(id, att));
  }
}

function updateChipError(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = "file-chip error";
  el.innerHTML = `<span class="file-chip-icon">⚠</span><span class="file-chip-name">${msg}</span>
    <button class="file-chip-remove" data-id="${id}">✕</button>`;
  el.querySelector(".file-chip-remove").addEventListener("click", () => el.remove());
}

function removeChip(id, att) {
  const el = document.getElementById(id);
  if (el) el.remove();
  pendingAttachments = pendingAttachments.filter(a => a.filename !== att.filename);
}

function clearAttachments() {
  pendingAttachments = [];
  fileChips.innerHTML = "";
}

function getFileIcon(name) {
  const ext = name.split(".").pop().toLowerCase();
  const MAP = { pdf: "", docx: "", doc: "", xlsx: "", xls: "",
                py: "", js: "", ts: "", html: "", css: "",
                json: "", csv: "", md: "", txt: "" };
  return MAP[ext] || "";
}

function formatBytes(b) {
  if (b < 1024)     return b + " B";
  if (b < 1048576)  return (b / 1024).toFixed(1) + " KB";
  return (b / 1048576).toFixed(1) + " MB";
}



// ══════════════════════════════════════════════════════════════════════════════
// MEMORY MANAGEMENT
// ══════════════════════════════════════════════════════════════════════════════
const memoryToggle  = document.getElementById("memory-toggle");
const memoryBody    = document.getElementById("memory-body");
const memoryChevron = document.getElementById("memory-chevron");
const memoryCount   = document.getElementById("memory-count");
const memoryList    = document.getElementById("memory-list");
const memoryInput   = document.getElementById("memory-input");
const memorySaveBtn = document.getElementById("memory-save-btn");
const memoryAddBtn  = document.getElementById("memory-add-btn");

memoryToggle.addEventListener("click", () => {
  const isOpen = !memoryBody.classList.contains("hidden");
  memoryBody.classList.toggle("hidden", isOpen);
  memoryChevron.classList.toggle("open", !isOpen);
  if (!isOpen && currentUser) loadMemories();
});

memoryAddBtn.addEventListener("click", e => {
  e.stopPropagation();
  memoryBody.classList.remove("hidden");
  memoryChevron.classList.add("open");
  memoryInput.focus();
  if (currentUser) loadMemories();
});

memorySaveBtn.addEventListener("click", saveMemory);
memoryInput.addEventListener("keydown", e => { if (e.key === "Enter") saveMemory(); });

async function loadMemories() {
  if (!currentUser) return;
  try {
    const res  = await fetch(`${API}/api/memory/${encodeURIComponent(currentUser.user_id)}`);
    const data = await res.json();
    renderMemories(data.memories || []);
    memoryCount.textContent = (data.memories || []).length;
  } catch (_) {}
}

function formatMemoryContent(content) {
  // Detect "key: {json}" or "key: [json]" patterns from System's auto-profile
  const match = content.match(/^([^:]+):\s*([\s\S]*)$/);
  if (match) {
    const label = match[1].trim()
      .replace(/_/g, ' ')
      .replace(/\b\w/g, c => c.toUpperCase()); // Title Case
    
    let rawValue = match[2].trim();
    
    try {
      // Try parsing as JSON first
      const obj = JSON.parse(rawValue);
      if (typeof obj === 'object' && obj !== null && !Array.isArray(obj)) {
        const lines = Object.entries(obj)
          .filter(([, v]) => v !== null && v !== undefined && v !== '')
          .map(([k, v]) => `<span style="color:var(--muted);font-size:10px">${k.replace(/_/g,' ')}:</span> ${escHtml(String(v))}`)
          .join('<br>');
        return `<strong style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--accent);opacity:0.7">${escHtml(label)}</strong><br>${lines}`;
      }
      if (Array.isArray(obj)) {
        const lines = obj.map(v => `• ${escHtml(String(v))}`).join('<br>');
        return `<strong style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--accent);opacity:0.7">${escHtml(label)}</strong><br>${lines}`;
      }
    } catch(_) {}
    
    // Fallback if JSON.parse fails (e.g. because it's a Python dict string with single quotes)
    // Strip leading/trailing braces or brackets if present
    if ((rawValue.startsWith('{') && rawValue.endsWith('}')) || (rawValue.startsWith('[') && rawValue.endsWith(']'))) {
      rawValue = rawValue.slice(1, -1).trim();
    }
    
    return `<strong style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--accent);opacity:0.7">${escHtml(label)}</strong><br><span style="font-size:12px;opacity:0.9">${escHtml(rawValue)}</span>`;
  }
  // Plain manual note - show as-is
  return escHtml(content);
}

function renderMemories(mems) {
  if (!mems.length) {
    memoryList.innerHTML = "<div class='memory-empty'>No memories yet. Add facts System should always know about you.</div>";
    return;
  }
  memoryList.innerHTML = mems.map(m => `
    <div class="memory-item" data-id="${m.id}" style="align-items:flex-start">
      <span class="memory-item-text" style="line-height:1.5">${formatMemoryContent(m.content)}</span>
      <button class="memory-del" data-id="${m.id}" title="Delete" style="flex-shrink:0;margin-top:2px">✕</button>
    </div>`).join("");

  memoryList.querySelectorAll(".memory-del").forEach(btn => {
    btn.addEventListener("click", () => deleteMemory(btn.dataset.id));
  });
}

async function saveMemory() {
  const content = memoryInput.value.trim();
  if (!content || !currentUser) return;
  memoryInput.value = "";
  try {
    const res = await fetch(`${API}/api/memory/${encodeURIComponent(currentUser.user_id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    if (res.ok) {
      showToast("Saved Notes saved", "success");
      loadMemories();
    }
  } catch (_) { showToast("Could not save memory", "error"); }
}

async function deleteMemory(memId) {
  if (!currentUser) return;
  try {
    await fetch(`${API}/api/memory/${encodeURIComponent(currentUser.user_id)}/${memId}`, { method: "DELETE" });
    loadMemories();
  } catch (_) {}
}

// Load memories when user is set
const _origApplyUser = applyUser;
window.applyUser = function(user) {
  _origApplyUser(user);
  loadMemories();
};

// ══════════════════════════════════════════════════════════════════════════════
// STREAMING — replaces the override from the file-upload section
// Uses fetch + ReadableStream to consume SSE, exactly like System.ai
// ══════════════════════════════════════════════════════════════════════════════
function showThinkingIndicator() {
  const el = document.getElementById("thinking-indicator");
  if (el) el.style.display = "flex";
}

function hideThinkingIndicator() {
  const el = document.getElementById("thinking-indicator");
  if (el) el.style.display = "none";
}

/** User asked for PowerPoint / .pptx (export) — checked before Word so “ppt deck” wins. */
function userAskedForPptExport(question) {
  if (!question || !question.trim()) return false;
  const q = question;
  return (
    /\b(?:pptx?|\.pptx\b|power\s*point|powerpoint)\b/i.test(q) ||
    (/\b(?:slide\s*)?deck\b/i.test(q) &&
      /\b(ppt|pptx|powerpoint|slides?|\.ppt)\b/i.test(q)) ||
    /\b(?:save|create|give|generate|make|export|download)\s+(?:me\s+)?(?:a\s+)?(?:the\s+)?(?:ppt|pptx|powerpoint(?:\s+file|presentation)?|slide\s*(?:presentation|deck))\b/i.test(q)
  );
}

/** User asked forPDF export — checked before Word */
function userAskedForPdfExport(question) {
  if (!question || !question.trim()) return false;
  const q = question;
  if (userAskedForPptExport(q)) return false;
  return (
    /\bpdf\b/i.test(q) ||
    /\.pdf\b/i.test(q) ||
    /\b(?:save|create|give|generate|make|export|download)\s+(?:me\s+)?(?:a\s+)?(?:the\s+)?(?:pdf|\.pdf)\b/i.test(q) ||
    /\b(?:as|to)\s+(?:a\s+)?pdf\b/i.test(q) ||
    /\bpdf\s+(?:of|for|from)\b/i.test(q)
  );
}

/** User asked for an actual Word /DOCX file (export), not just markdown in chat */
function userAskedForWordExport(question) {
  if (!question || !question.trim()) return false;
  const q = question;
  if (userAskedForPptExport(q)) return false;
  if (userAskedForPdfExport(q)) return false;
  return (
    /\b(docx|\.doc\b)/i.test(q) ||
    /\bmicrosoft\s+word\b/i.test(q) ||
    /\bword\s+document\b/i.test(q) ||
    /\bmake\s+(?:me\s+)?(?:a\s+)?word\b/i.test(q) ||
    /\b(?:save|create|give|generate|export)\s+(?:me\s+)?(?:a\s+)?(?:word|docx)\b/i.test(q) ||
    /\ba\s+word\s+(?:file|doc|document)\b/i.test(q)
  );
}

function collectOutputContract() {
  const fmt = document.getElementById("oc-format")?.value?.trim();
  const len = document.getElementById("oc-length")?.value?.trim();
  const tone = document.getElementById("oc-tone")?.value?.trim();
  const aud = document.getElementById("oc-audience")?.value?.trim();
  const o = {};
  if (fmt) o.format = fmt;
  if (len) o.length = len;
  if (tone) o.tone = tone;
  if (aud) o.audience = aud;
  return o;
}

window.regenerateWithNote = function(assistantEl, note) {
  const n = (note || "").trim();
  if (!n) {
    showToast("Add a short instruction first", "info");
    return;
  }
  const allMsgs = [...messagesEl.querySelectorAll(".msg")];
  const idx = allMsgs.indexOf(assistantEl);
  if (idx < 1) {
    showToast("Cannot regenerate this reply", "error");
    return;
  }
  const prevUser = allMsgs[idx - 1];
  if (!prevUser.classList.contains("user")) {
    showToast("Could not find your previous message", "error");
    return;
  }
  const userText = prevUser.querySelector(".msg-text")?.innerText?.split("\n")[0].trim() || "";
  if (!userText) return;
  const combined = userText + "\n\n---\nRegenerate your last reply with this instruction: " + n;
  window.sendMessage(combined, idx, { amend_last_user: true });
};

window.sendMessage = async function(overrideText = null, truncateFromIndex = null, opts = {}) {
  const text = overrideText || msgInput.value.trim();
  if (!text || isLoading || !currentConvId) return;
  
  const popup = document.getElementById("slash-popup");
  if (popup) popup.style.display = "none";

  const atts = typeof pendingAttachments !== "undefined" ? [...pendingAttachments] : [];

  if (truncateFromIndex !== null) {
    const allMsgs = Array.from(messagesEl.querySelectorAll('.msg'));
    for (let i = truncateFromIndex; i < allMsgs.length; i++) {
      allMsgs[i].remove();
    }
  }

  if (!overrideText) {
    msgInput.value = "";
    msgInput.style.height = "auto";
    charCount.textContent = "";
    if (typeof clearAttachments === "function") clearAttachments();
  }
  setLoading(true);

  // Show user message (with file indicator if files attached)
  const displayText = atts.length
    ? text + `\n\n _${atts.length} file(s): ${atts.map(a => a.filename).join(", ")}_`
    : text;
  if (!opts.amend_last_user) {
    appendMessage("user", displayText);
  } else {
    const lastUserEl = [...messagesEl.querySelectorAll(".msg.user")].pop();
    const mt = lastUserEl?.querySelector(".msg-text");
    if (mt) {
      mt.innerHTML = formatText(displayText);
      mt.setAttribute("data-raw", escHtml(displayText).replace(/"/g, "&quot;"));
    }
  }

  // Create the streaming assistant message element
  const streamEl = createStreamingMessage();
  scrollToBottom();

  // --- URL Fetching Indicator ---
  let urlNoticeEl = null;
  const urlPattern = /https?:\/\/[^\s]+/g;
  if (urlPattern.test(text)) {
    urlNoticeEl = document.createElement("div");
    urlNoticeEl.className = "url-reading-notice";
    urlNoticeEl.textContent = "Reading page content...";
    // Append it to the stream element (so it shows under the avatar)
    streamEl.querySelector(".msg-text").appendChild(urlNoticeEl);
  }

  let fullText = "";
  let abortController = new AbortController();

  // Transform send button to Stop button
  sendBtn.disabled = false;
  sendBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none" width="14" height="14"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>`;
  sendBtn.style.background = "#ef4444";
  sendBtn.title = "Stop generating";
  const stopHandler = () => { abortController.abort(); };
  sendBtn.addEventListener("click", stopHandler, { once: true });

  const resetSendBtn = () => {
    sendBtn.removeEventListener("click", stopHandler);
    sendBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
    sendBtn.style.background = "";
    sendBtn.title = "Send (Enter)";
  };

  try {
    const modelOverrideEl = document.getElementById("model-override");
    const override = modelOverrideEl ? modelOverrideEl.value : "auto";
    const bodyPayload = { 
      message: text, 
      attachments: atts,
      sender_id: currentUser ? currentUser.user_id : "",
      sender_name: currentUser ? currentUser.user_name : ""
    };
    if (override !== "auto") bodyPayload.model_override = override;
    if (truncateFromIndex !== null) bodyPayload.truncate_from_index = truncateFromIndex;
    const webEl = document.getElementById("web-search-toggle");
    if (webEl && webEl.checked) bodyPayload.web_search = true; // legacy web search
    
    // Inject skills state
    if (window.activeSkill) bodyPayload.skill_id = window.activeSkill;
    if (window.activeStyle) bodyPayload.style = window.activeStyle;
    if (window.webSearchEnabled) bodyPayload.webSearchEnabled = window.webSearchEnabled;
    
    const oc = collectOutputContract();
    if (Object.keys(oc).length) bodyPayload.output_contract = oc;
    if (opts.amend_last_user) bodyPayload.amend_last_user = true;

    const response = await fetch(`${API}/api/conversations/${currentConvId}/stream`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(bodyPayload),
      signal:  abortController.signal,
    });

    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      streamEl.remove();
      appendErrorMessage(err.error || `HTTP ${response.status}`);
      showToast(" " + (err.error || "Error"), "error");
      return;
    }

    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();  // keep incomplete line

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const json = line.slice(6).trim();
        if (!json) continue;

        let event;
        try { event = JSON.parse(json); } catch { continue; }

        if (urlNoticeEl) {
          urlNoticeEl.remove();
          urlNoticeEl = null;
        }

        if (event.type === "thinking_start") {
          showThinkingIndicator();

        } else if (event.type === "thinking_end") {
          hideThinkingIndicator();

        } else if (event.type === "text") {
          fullText += event.text;
          updateStreamingMessage(streamEl, fullText);
          scrollToBottom();

        } else if (event.type === "done") {
          hideThinkingIndicator();
          finalizeStreamingMessage(streamEl, fullText, {
            model_tier: event.model_tier,
            model_used: event.model_used,
            cost_usd:   event.cost_usd,
            kb_sources: event.kb_sources || [],
          });
          if (typeof exportDocument === "function") {
            if (userAskedForPptExport(text)) {
              setTimeout(() => {
                try {
                  exportDocument("pptx");
                  showToast(" Downloading PowerPoint (.pptx)…", "success");
                } catch (_) {}
              }, 80);
            } else if (userAskedForPdfExport(text)) {
              setTimeout(() => {
                try {
                  exportDocument("pdf");
                  showToast(" DownloadingPDF…", "success");
                } catch (_) {}
              }, 80);
            } else if (userAskedForWordExport(text)) {
              setTimeout(() => {
                try {
                  exportDocument("docx");
                  showToast(" Downloading Word document…", "success");
                } catch (_) {}
              }, 80);
            }
          }
          updateHeaderChips(event.task_type || "general", event.model_tier || "haiku");
          updateInputMeta(event.task_type || "general", event.model_used || "");
          convTitleHeader.textContent = event.title || convTitleHeader.textContent;
          const activeItem = convList.querySelector(`.conv-item[data-id="${currentConvId}"] .conv-item-title`);
          if (activeItem && event.title) activeItem.textContent = event.title;
          updateBudgetUI(event.budget);
          scrollToBottom();

        } else if (event.type === "error") {
          hideThinkingIndicator();
          streamEl.remove();
          appendErrorMessage(event.error);
          showToast(" " + event.error, "error");
        }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      // User clicked Stop — finalize whatever was received
      hideThinkingIndicator();
      if (fullText) {
        finalizeStreamingMessage(streamEl, fullText + "\n\n*[Generation stopped]*", {});
      } else {
        streamEl.remove();
      }
      showToast("⏹ Generation stopped", "info");
    } else {
      console.error("Stream Fetch Error:", err);
      hideThinkingIndicator();
      streamEl.remove();
      appendErrorMessage("Network error: " + err.message);
      showToast("⚠ " + err.message, "error");
    }
  } finally {
    hideThinkingIndicator();
    resetSendBtn();
    setLoading(false);
    msgInput.focus();
    scrollToBottom();
  }
};

// Re-bind buttons to streaming sendMessage
sendBtn.removeEventListener("click", sendMessage);
sendBtn.addEventListener("click", () => window.sendMessage());
msgInput.removeEventListener("keydown", () => {});
msgInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); window.sendMessage(); }
});

/**
 * Adds visual “presentation wrapping” around assistant replies (card + optional deliverable ribbon).
 * System web bundles layout polish in-product; here we frame the same story more clearly.
 */
function decorateAssistantReply(el, rawText) {
  if (!el || !el.classList.contains("assistant") || el.classList.contains("typing-indicator")) return;
  // el.classList.add("assistant-reply-framed");
  const body = el.querySelector(".msg-body");
  if (!body) return;
  // body.classList.add("assistant-reply-frame");

  const text = rawText || "";
  const looksDeck = /\b(?:slide\s+\d+|##\s*slide\s+\d+)/i.test(text);
  const looksFormalDoc =
    /^#\s+\S/m.test(text) && text.length > 160 &&
    /\b(?:introduction|summary|conclusion|abstract)\b/i.test(text);

  if (!looksDeck && !looksFormalDoc) return;
  if (body.querySelector(".reply-deliverable-hint")) return;

  const nameEl = body.querySelector(".msg-name");
  if (!nameEl) return;

  const hint = document.createElement("div");
  hint.className = "reply-deliverable-hint";
  hint.setAttribute("role", "note");
  if (looksDeck) {
    hint.innerHTML =
      '<span class="hint-label">Deliverable</span>' +
      '<span class="hint-text">Use <strong>PPT</strong> for slides, <strong>DOCX</strong> for Word, or <strong>PDF</strong> for a print-ready file.</span>';
  } else {
    hint.innerHTML =
      '<span class="hint-label">Deliverable</span>' +
      '<span class="hint-text">Use <strong>DOCX</strong> or <strong>PDF</strong> from the footer above the input.</span>';
  }
  nameEl.after(hint);
}

// ── Streaming DOM helpers ─────────────────────────────────────────────────────
function createStreamingMessage() {
  const el  = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `
    <div class="msg-body">
      <div class="msg-text stream-text"><span class="cursor-blink"></span></div>
    </div>`;
  messagesEl.appendChild(el);
  return el;
}

function updateStreamingMessage(el, text) {
  const textEl = el.querySelector(".msg-text");
  // Render partial markdown but keep cursor at end
  textEl.innerHTML = formatText(text) + '<span class="cursor-blink"></span>';
}

function finalizeStreamingMessage(el, text, meta = {}) {
  const body   = el.querySelector(".msg-body");
  const textEl = el.querySelector(".msg-text");
  textEl.innerHTML = formatText(text);

  // Auto-open artifact panel if response contains an HTML or SVG code block
  (function autoArtifact() {
    const m = text.match(/```(html|svg)\r?\n([\s\S]*?)```/i);
    if (m) showArtifactSidePreview(m[1].toLowerCase(), m[2]);
  })();

  // Keep artifact/export pane in sync (streaming bypassed appendMessage wrapper).
  if (window.setLastAIResponse) {
    const convTitle = document.getElementById("conv-title-header")?.textContent?.trim()
                      || "System Export";
    window.setLastAIResponse(text, convTitle);
  }

  // Cost + model chips
  if (meta.model_tier || meta.cost_usd) {
    const metaEl = document.createElement("div");
    metaEl.className = "msg-meta";
    metaEl.innerHTML = `
      ${meta.model_used ? `<span class="msg-chip ${meta.model_tier}">${meta.model_used}</span>` : ""}
      ${meta.cost_usd  ? `<span class="msg-chip cost">$${meta.cost_usd.toFixed(5)} <span style="opacity:0.7">(₹${(meta.cost_usd * 83.5).toFixed(4)})</span></span>` : ""}`;
    body.appendChild(metaEl);
  }

  if (meta.kb_sources && meta.kb_sources.length) {
    const kbEl = document.createElement("div");
    kbEl.className = "msg-kb-sources";
    const names = [...new Set(meta.kb_sources.map((k) => k.filename).filter(Boolean))];
    kbEl.innerHTML = "<strong>KB used:</strong> " + names.map(escHtml).join(", ");
    body.appendChild(kbEl);
  }

  // Action buttons
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  actions.innerHTML = `
    <button class="msg-action-btn copy-btn" title="Copy response"> Copy</button>
    <button class="msg-action-btn export-pdf-btn" type="button" title="Download asPDF">PDF</button>
    <button class="msg-action-btn export-docx-btn" type="button" title="Download as Word">DOCX</button>
    <button class="msg-action-btn export-pptx-btn" type="button" title="Download as PowerPoint">PPT</button>
    <button class="msg-action-btn regen-btn" title="Remove this reply and re-send your last question">↺ Retry</button>`;
  body.appendChild(actions);

  const pdfBtn = actions.querySelector(".export-pdf-btn");
  const docxBtn = actions.querySelector(".export-docx-btn");
  const pptxBtn = actions.querySelector(".export-pptx-btn");
  if (pdfBtn && typeof exportDocument === "function") {
    pdfBtn.addEventListener("click", () => exportDocument("pdf", pdfBtn));
  }
  if (docxBtn && typeof exportDocument === "function") {
    docxBtn.addEventListener("click", () => exportDocument("docx", docxBtn));
  }
  if (pptxBtn && typeof exportDocument === "function") {
    pptxBtn.addEventListener("click", () => exportDocument("pptx", pptxBtn));
  }

  actions.querySelector(".copy-btn").addEventListener("click", function() {
    navigator.clipboard.writeText(text).then(() => {
      this.textContent = "✓ Copied";
      this.classList.add("copied");
      setTimeout(() => { this.textContent = " Copy"; this.classList.remove("copied"); }, 2000);
    });
  });
  actions.querySelector(".regen-btn").addEventListener("click", () => {
    // Retry: remove last assistant message, re-send last user message
    el.remove();
    const lastUser = [...messagesEl.querySelectorAll(".msg.user")].pop();
    if (lastUser) {
      const content = lastUser.querySelector(".msg-text")?.textContent?.split("\n")[0].trim();
      if (content) { msgInput.value = content; window.sendMessage(); }
    }
  });


  maybeAddSidePreviewButton(actions, text);

  decorateAssistantReply(el, text);
}

// Also add action buttons to appendMessage (for loaded conversation history)
const _origAppendMessage = appendMessage;
window.appendMessage = function(role, content, meta = {}) {
  const el = _origAppendMessage(role, content, meta);
  if (role === "assistant" && el) {
    const body    = el.querySelector(".msg-body");
    if (meta.kb_sources && meta.kb_sources.length) {
      const kbEl = document.createElement("div");
      kbEl.className = "msg-kb-sources";
      const names = [...new Set(meta.kb_sources.map((k) => k.filename).filter(Boolean))];
      kbEl.innerHTML = "<strong>KB used:</strong> " + names.map(escHtml).join(", ");
      body.appendChild(kbEl);
    }
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    actions.innerHTML = `
      <button class="msg-action-btn copy-btn" title="Copy"> Copy</button>
      <button class="msg-action-btn export-pdf-btn" type="button" title="Download asPDF">PDF</button>
      <button class="msg-action-btn export-docx-btn" type="button" title="Download as Word">DOCX</button>
      <button class="msg-action-btn export-pptx-btn" type="button" title="Download as PowerPoint">PPT</button>
      <button class="msg-action-btn regen-btn" type="button" title="Remove this reply and retry">↺ Retry</button>`;
    body.appendChild(actions);
    const pdfBtn = actions.querySelector(".export-pdf-btn");
    const docxBtn = actions.querySelector(".export-docx-btn");
    const pptxBtn = actions.querySelector(".export-pptx-btn");
    if (pdfBtn && typeof exportDocument === "function") {
      pdfBtn.addEventListener("click", () => exportDocument("pdf", pdfBtn));
    }
    if (docxBtn && typeof exportDocument === "function") {
      docxBtn.addEventListener("click", () => exportDocument("docx", docxBtn));
    }
    if (pptxBtn && typeof exportDocument === "function") {
      pptxBtn.addEventListener("click", () => exportDocument("pptx", pptxBtn));
    }

    actions.querySelector(".copy-btn").addEventListener("click", function() {
      navigator.clipboard.writeText(content).then(() => {
        this.textContent = "✓ Copied";
        this.classList.add("copied");
        setTimeout(() => { this.textContent = " Copy"; this.classList.remove("copied"); }, 2000);
      });
    });
    actions.querySelector(".regen-btn").addEventListener("click", () => {
      el.remove();
      const lastUser = [...messagesEl.querySelectorAll(".msg.user")].pop();
      if (lastUser) {
        const c = lastUser.querySelector(".msg-text")?.textContent?.split("\n")[0].trim();
        if (c) { msgInput.value = c; window.sendMessage(); }
      }
    });



    // ── Track last AI response for server-side export ──────────────────────
    if (window.setLastAIResponse) {
      const convTitle = document.getElementById("conv-title-header")?.textContent?.trim()
                        || "System Export";
      window.setLastAIResponse(content, convTitle);
    }

    maybeAddSidePreviewButton(actions, content);
    decorateAssistantReply(el, content);
  }
  return el;
};


// ── System.ai Clone Features ────────────────────────────────────────────────
window.filterChats = function(query) {
  const term = query.toLowerCase();
  document.querySelectorAll(".conv-item").forEach(item => {
    const title = item.querySelector(".conv-item-title").textContent.toLowerCase();
    item.style.display = title.includes(term) ? "flex" : "none";
  });
};

// Global Keyboard Shortcuts
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const pane = document.getElementById("artifacts-pane");
    if (pane && pane.classList.contains("artifacts-pane--open")) {
      e.preventDefault();
      window.closeArtifact();
    }
  }
  // Cmd+K or Ctrl+K for New Chat
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    document.getElementById("new-chat-btn")?.click();
  }
  // Cmd+Enter or Ctrl+Enter to Send Message
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    if (document.activeElement === msgInput) {
      e.preventDefault();
      document.getElementById("send-btn")?.click();
    }
  }
});

window.downloadCode = function(btn, ext) {
  const code = decodeURIComponent(btn.getAttribute("data-code"));
  const blob = new Blob([code], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `code_snippet.${ext}`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  
  const originalText = btn.innerHTML;
  btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22d3a0" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg> <span style="color:#22d3a0">Saved</span>`;
  setTimeout(() => { btn.innerHTML = originalText; }, 2000);
};

// ── Copy Message ──────────────────────────────────────────────────────────────
window.copyMessage = function(btn) {
  const text = btn.getAttribute("data-text") || btn.closest(".msg-body")?.querySelector(".msg-text")?.innerText || "";
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.innerHTML;
    btn.innerHTML = "✓ Copied";
    btn.classList.add("copied");
    setTimeout(() => { btn.innerHTML = orig; btn.classList.remove("copied"); }, 2000);
  }).catch(() => showToast("Copy failed", "error"));
};

// ── Dark / Light Mode Toggle ──────────────────────────────────────────────────
(function initTheme() {
  const saved = localStorage.getItem("theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);
})();

window.toggleTheme = function() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  const btn = document.getElementById("theme-toggle-btn");
  if (btn) btn.textContent = next === "dark" ? "☀" : "";
};

// ── Export all calls to CSV ───────────────────────────────────────────────────
window.exportAllCalls = function() {
  window.open(`${API}/api/usage/export`, "_blank");
};

