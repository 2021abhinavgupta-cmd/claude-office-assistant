/**
 * Claude Office Assistant — Multi-user, multi-conversation frontend
 * Each employee gets their own set of conversations.
 * Full Claude-like experience with task auto-detection and persistent history.
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
  coding: "⌨️", html_design: "🎨", presentations: "📊",
  captions: "✍️", scripts: "🎬", general: "💬",
};
const TASK_MODELS = {
  coding: "sonnet", html_design: "sonnet", presentations: "sonnet",
  captions: "haiku", scripts: "haiku", general: "haiku",
};

// ── DOM refs ─────────────────────────────────────────────────────────────────
const employeeModal  = document.getElementById("employee-modal");
const empGrid        = document.getElementById("emp-grid");
const customNameInput = document.getElementById("custom-name");
const customNameBtn  = document.getElementById("custom-name-btn");
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
document.addEventListener("DOMContentLoaded", () => {
  checkConnection();
  fetchBudget();

  const saved = loadUserFromStorage();
  if (saved) {
    currentUser = saved;
    applyUser(saved);
    loadConversations();
    employeeModal.classList.add("hidden");
  } else {
    loadEmployeeList();
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

// ── Employee Selection ────────────────────────────────────────────────────────
async function loadEmployeeList() {
  try {
    const res  = await fetch(`${API}/api/employees`);
    const data = await res.json();
    const emps = data.employees || [];

    if (emps.length === 0) {
      empGrid.innerHTML = "<div class='emp-loading'>No employees found.</div>";
      return;
    }

    empGrid.innerHTML = emps.map(e => `
      <button class="emp-btn" data-id="${e.whatsapp}" data-name="${e.name}" data-role="${e.role || ''}">
        <div class="emp-avatar">${e.name.charAt(0)}</div>
        <strong>${e.name}</strong>
        <small>${e.role || e.department || ''}</small>
      </button>
    `).join("");

    empGrid.querySelectorAll(".emp-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        selectUser(btn.dataset.id, btn.dataset.name);
      });
    });
  } catch (_) {
    empGrid.innerHTML = "<div class='emp-loading'>Could not load employees.</div>";
  }
}

function selectUser(userId, userName) {
  currentUser = { user_id: userId, user_name: userName };
  saveUserToStorage(currentUser);
  applyUser(currentUser);
  employeeModal.classList.add("hidden");
  loadConversations();
  if (typeof loadProjects === "function") loadProjects();
}

function applyUser(user) {
  userAvatar.textContent   = user.user_name.charAt(0).toUpperCase();
  userNameText.textContent = user.user_name;
}

customNameBtn.addEventListener("click", () => {
  const name = customNameInput.value.trim();
  if (!name) { customNameInput.focus(); return; }
  // Use name as both ID (slugified) and display name
  const uid = name.toLowerCase().replace(/\s+/g, "_").replace(/[^a-z0-9_]/g, "");
  selectUser(uid || "user_" + Date.now(), name);
});
customNameInput.addEventListener("keydown", e => {
  if (e.key === "Enter") customNameBtn.click();
});

userPill.addEventListener("click", () => {
  employeeModal.classList.remove("hidden");
  loadEmployeeList();
});

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
      const icon    = TASK_ICONS[c.task_type] || "💬";
      const isActive = c.id === currentConvId ? " active" : "";
      const msgCount = c.message_count || "";
      html += `
        <div class="conv-item${isActive}" data-id="${c.id}">
          <span class="conv-item-icon">${icon}</span>
          <div class="conv-item-body">
            <div class="conv-item-title">${escHtml(c.title)}</div>
            <div class="conv-item-sub">${c.task_type || "general"}${msgCount ? ` · ${msgCount} msgs` : ""}</div>
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

  try {
    const res  = await fetch(`${API}/api/conversations/${convId}`);
    const conv = await res.json();

    convTitleHeader.textContent = conv.title;
    const task  = conv.task_type || "general";
    const model = TASK_MODELS[task] || "haiku";
    updateHeaderChips(task, model);
    updateInputMeta(task, model);

    // Render existing messages
    messagesEl.innerHTML = "";
    (conv.messages || []).forEach(m => {
      appendMessage(m.role, m.content, {
        model_tier: m.model_tier,
        cost_usd:   m.cost_usd,
        model_used: m.model_used,
      });
    });
    scrollToBottom();

    // Mark active in sidebar
    document.querySelectorAll(".conv-item").forEach(el => {
      el.classList.toggle("active", el.dataset.id === convId);
    });
  } catch (e) {
    showToast("Could not load conversation", "error");
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
async function sendMessage() {
  const atts = typeof pendingAttachments !== "undefined" ? [...pendingAttachments] : [];
  const text = msgInput.value.trim();
  if (!text && !atts.length) return;
  if (isLoading || !currentConvId) return;

  msgInput.value = "";
  msgInput.style.height = "auto";
  charCount.textContent = "";
  if (typeof clearAttachments === "function") clearAttachments();
  setLoading(true);

  const displayText = atts.length
    ? text + `\n\n📎 _${atts.length} file(s) attached: ${atts.map(a => a.filename).join(", ")}_`
    : text;
  appendMessage("user", displayText || "Sent an attachment");
  const typingId = appendTyping();
  scrollToBottom();

  try {
    const res = await fetch(`${API}/api/conversations/${currentConvId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, attachments: atts }),
    });
    const data = await res.json();
    removeTyping(typingId);

    if (res.ok && data.success) {
      appendMessage("assistant", data.response, {
        model_tier: data.model_tier,
        model_used: data.model_used,
        cost_usd:   data.cost_usd,
      });
      // Update header with detected task type
      const task  = data.task_type || "general";
      const model = data.model_tier || "haiku";
      updateHeaderChips(task, model);
      updateInputMeta(task, data.model_used || "");
      convTitleHeader.textContent = data.title || convTitleHeader.textContent;

      // Update sidebar item title
      const activeItem = convList.querySelector(`.conv-item[data-id="${currentConvId}"] .conv-item-title`);
      if (activeItem && data.title) activeItem.textContent = data.title;

      updateBudgetUI(data.budget);
      scrollToBottom();
    } else {
      appendErrorMessage(data.error || "Something went wrong");
      showToast("❌ " + (data.error || "Error"), "error");
    }
  } catch (err) {
    removeTyping(typingId);
    appendErrorMessage("Could not reach the server. Is Flask running?");
    showToast("⚠️ Server offline", "error");
  } finally {
    setLoading(false);
    msgInput.focus();
  }
}

sendBtn.addEventListener("click", sendMessage);
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

// Quick-start cards
document.querySelectorAll(".qs-card").forEach(card => {
  card.addEventListener("click", () => {
    welcomeInput.value = card.dataset.prompt;
    toggleWelcomeSend();
    welcomeInput.focus();
  });
});

// ── UI Helpers ────────────────────────────────────────────────────────────────
function showChatView() {
  // Always hide project view when entering chat
  const projView = document.getElementById("project-view");
  if (projView) projView.classList.add("hidden");
  welcomeScreen.classList.add("hidden");
  chatView.classList.remove("hidden");
}
function showWelcomeScreen() {
  // Always hide project view when going to welcome
  const projView = document.getElementById("project-view");
  if (projView) projView.classList.add("hidden");
  chatView.classList.add("hidden");
  welcomeScreen.classList.remove("hidden");
}

// Toggle Projects panel in sidebar
window.toggleProjectsPanel = function() {
  const list = document.getElementById("projects-list");
  const chevron = document.getElementById("projects-chevron");
  if (!list) return;
  const isOpen = list.style.display !== "none";
  list.style.display = isOpen ? "none" : "block";
  if (chevron) chevron.style.transform = isOpen ? "rotate(-90deg)" : "rotate(0deg)";
};

function updateHeaderChips(task, modelTier) {
  taskChip.textContent  = task.replace(/_/g, " ");
  modelChip.textContent = modelTier;
  modelChip.className   = `model-chip ${modelTier}`;
}
function updateInputMeta(taskName, modelName) {
  if (metaTask) metaTask.textContent = taskName.replace(/_/g, " ");
  if (metaModel) metaModel.textContent = modelName || "claude-haiku-4-5";
}

// ── Message Rendering ─────────────────────────────────────────────────────────
function appendMessage(role, content, meta = {}) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;

  const avatar = role === "user"
    ? `<div class="msg-avatar">${currentUser ? currentUser.user_name.charAt(0).toUpperCase() : "U"}</div>`
    : `<div class="msg-avatar">✦</div>`;
  const name = role === "user"
    ? (currentUser ? currentUser.user_name : "You")
    : "Claude";

  const metaHtml = (role === "assistant" && (meta.model_tier || meta.cost_usd))
    ? `<div class="msg-meta">
        ${meta.model_used ? `<span class="msg-chip ${meta.model_tier}">${meta.model_used}</span>` : ""}
        ${meta.cost_usd   ? `<span class="msg-chip cost">$${meta.cost_usd.toFixed(5)} <span style="opacity:0.7">(₹${(meta.cost_usd * 83.5).toFixed(4)})</span></span>` : ""}
       </div>`
    : "";

  const copyBtn = role === "assistant"
    ? `<div class="msg-actions"><button class="msg-action-btn copy-btn" onclick="copyMessage(this)" data-text="${escHtml(content).replace(/"/g,'&quot;')}" title="Copy">📋 Copy</button></div>`
    : "";

  el.innerHTML = `
    ${avatar}
    <div class="msg-body">
      <div class="msg-name">${escHtml(name)}</div>
      <div class="msg-text">${formatText(content)}</div>
      ${metaHtml}
      ${copyBtn}
    </div>`;

  messagesEl.appendChild(el);
  return el;
}

function appendErrorMessage(text) {
  const el = document.createElement("div");
  el.className = "msg error";
  el.innerHTML = `
    <div class="msg-avatar">⚠️</div>
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
    <div class="msg-avatar">✦</div>
    <div class="msg-body">
      <div class="msg-name">Claude</div>
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

// ── Text Formatting (Markdown & Highlight.js) ──────────────────────────────
if (typeof marked !== "undefined") {
  const renderer = new marked.Renderer();
  renderer.code = function(code, language, isEscaped) {
    const ext = language || 'txt';
    const cleanCode = code.trim();
    const encodedCode = cleanCode.length < 500000 ? encodeURIComponent(cleanCode) : encodeURIComponent(cleanCode.slice(0, 500000));
    
    let highlighted = cleanCode;
    if (language && hljs.getLanguage(language)) {
      highlighted = hljs.highlight(cleanCode, { language }).value;
    } else {
      highlighted = escHtml(cleanCode);
    }

    let previewBtn = '';
    if (ext === 'html' || ext === 'svg') {
      previewBtn = `<button onclick="previewArtifact(this, '${ext}')" data-code="${encodedCode}" style="background:none; border:none; color:var(--accent); cursor:pointer; font-size:0.75rem; display:flex; align-items:center; gap:4px; opacity:0.9;"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg> Preview</button>`;
    }

    const headerHtml = `
      <div class="code-header" style="display:flex; justify-content:space-between; background:var(--surface2); padding:6px 12px; border-radius:8px 8px 0 0; font-size:0.75rem; color:var(--muted); border:1px solid var(--border); border-bottom:none;">
        <span style="font-family:JetBrains Mono, monospace; text-transform:uppercase">${ext}</span>
        <div style="display:flex; gap:12px;">
          ${previewBtn}
          <button onclick="downloadCode(this, '${ext}')" data-code="${encodedCode}" style="background:none; border:none; color:var(--text); cursor:pointer; font-size:0.75rem; display:flex; align-items:center; gap:4px; opacity:0.8; transition:opacity 0.2s;" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.8'">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg> Download
          </button>
        </div>
      </div>`;

    return `<div class="code-block-wrapper" style="margin: 16px 0;">${headerHtml}<pre style="margin-top:0; border-top-left-radius:0; border-top-right-radius:0; border:1px solid var(--border); padding:16px; overflow-x:auto; background:#282c34;"><code class="hljs language-${language}">${highlighted}</code></pre></div>`;
  };
  marked.setOptions({ renderer: renderer, breaks: true, gfm: true });
}

function formatText(text) {
  if (typeof marked !== "undefined") {
    // Fix unclosed code blocks during streaming
    const codeBlocks = (text.match(/```/g) || []).length;
    if (codeBlocks % 2 !== 0) {
      text += "\n```"; 
    }
    return marked.parse(text);
  }
  return `<p>${escHtml(text).replace(/\n/g, "<br>")}</p>`;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Budget ────────────────────────────────────────────────────────────────────
async function fetchBudget() {
  try {
    const res  = await fetch(`${API}/api/budget`);
    const data = await res.json();
    updateBudgetUI(data);
  } catch (_) {}
}

function updateBudgetUI(b) {
  if (!b) return;
  const spent = b.spent ?? b.monthly_spend ?? 0;
  const limit = b.limit ?? b.budget_limit  ?? 150;
  const pct   = Math.min((spent / limit) * 100, 100);
  budgetMiniVal.textContent = `$${spent.toFixed(2)} (₹${(spent*83.5).toFixed(2)}) / $${limit} (₹${(limit*83.5).toFixed(2)})`;
  budgetMiniFill.style.width = pct + "%";
  if (pct >= 90) budgetMiniFill.style.background = "#ef4444";
  else if (pct >= 80) budgetMiniFill.style.background = "linear-gradient(90deg,#f59e0b,#ef4444)";
  else budgetMiniFill.style.background = "linear-gradient(90deg,var(--haiku-color),var(--accent))";
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
  sidebarToggle.addEventListener("click", () => {
    const isMobile = window.innerWidth <= 640;
    sidebar.classList.toggle(isMobile ? "open" : "closed");
  });
}

// ── Input Setup ───────────────────────────────────────────────────────────────
function setupInputs() {
  msgInput.addEventListener("input", () => {
    autoResize(msgInput);
    const len = msgInput.value.length;
    charCount.textContent = len > 6000 ? `${len}/8000` : "";
    charCount.style.color = len > 7000 ? "#ef4444" : "";
    sendBtn.disabled = !msgInput.value.trim() || isLoading;
  });
}

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
  try { localStorage.setItem("claude_office_user", JSON.stringify(user)); } catch (_) {}
}
function loadUserFromStorage() {
  try {
    const raw = localStorage.getItem("claude_office_user");
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}

// ══════════════════════════════════════════════════════════════════════════════
// FILE UPLOAD
// ══════════════════════════════════════════════════════════════════════════════
let pendingAttachments = [];  // [{type, filename, content|data, media_type, size}]

const fileInput   = document.getElementById("file-input");
const uploadBtn   = document.getElementById("upload-btn");
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
        const maxDim = 2048; // Safe dimension for Claude API
        
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
  addFileChip(chipId, file.name, formatBytes(file.size), "uploading");

  const form = new FormData();
  form.append("file", file);

  try {
    const res  = await fetch(`${API}/api/upload`, { method: "POST", body: form });
    const data = await res.json();

    if (!res.ok || !data.success) {
      updateChipError(chipId, data.error || "Upload failed");
      showToast(`❌ ${file.name}: ${data.error || "Upload failed"}`, "error");
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

    const icon = data.type === "image" ? "🖼️" : getFileIcon(file.name);
    updateChipReady(chipId, att, icon);
  } catch (err) {
    updateChipError(chipId, "Server unreachable");
    showToast("❌ Upload failed: server offline", "error");
  }
}

function addFileChip(id, name, size, state) {
  const el = document.createElement("div");
  el.className = `file-chip ${state}`;
  el.id = id;
  el.innerHTML = `
    <span class="file-chip-icon">⏳</span>
    <span class="file-chip-name">${escHtml(name)}</span>
    <span class="file-chip-size">${size}</span>`;
  fileChips.appendChild(el);
}

function updateChipReady(id, att, icon) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `file-chip ${att.type}`;
  el.innerHTML = `
    <span class="file-chip-icon">${icon}</span>
    <span class="file-chip-name">${escHtml(att.filename)}</span>
    <span class="file-chip-size">${formatBytes(att.size_bytes || 0)}</span>
    <button class="file-chip-remove" data-id="${id}" title="Remove">✕</button>`;
  el.querySelector(".file-chip-remove").addEventListener("click", () => removeChip(id, att));
}

function updateChipError(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = "file-chip error";
  el.innerHTML = `<span class="file-chip-icon">⚠️</span><span class="file-chip-name">${msg}</span>
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
  const MAP = { pdf: "📄", docx: "📝", doc: "📝", xlsx: "📊", xls: "📊",
                py: "🐍", js: "🟨", ts: "🔷", html: "🌐", css: "🎨",
                json: "📋", csv: "📊", md: "📄", txt: "📄" };
  return MAP[ext] || "📎";
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

function renderMemories(mems) {
  if (!mems.length) {
    memoryList.innerHTML = "<div class='memory-empty'>No memories yet. Add facts Claude should always know about you.</div>";
    return;
  }
  memoryList.innerHTML = mems.map(m => `
    <div class="memory-item" data-id="${m.id}">
      <span class="memory-item-text">${escHtml(m.content)}</span>
      <button class="memory-del" data-id="${m.id}" title="Delete">✕</button>
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
      showToast("🧠 Memory saved", "success");
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
// Uses fetch + ReadableStream to consume SSE, exactly like Claude.ai
// ══════════════════════════════════════════════════════════════════════════════
window.sendMessage = async function() {
  const text = msgInput.value.trim();
  if (!text || isLoading || !currentConvId) return;

  const atts = [...pendingAttachments];

  msgInput.value = "";
  msgInput.style.height = "auto";
  charCount.textContent = "";
  clearAttachments();
  setLoading(true);

  // Show user message (with file indicator if files attached)
  const displayText = atts.length
    ? text + `\n\n📎 _${atts.length} file(s): ${atts.map(a => a.filename).join(", ")}_`
    : text;
  appendMessage("user", displayText);

  // Create the streaming assistant message element
  const streamEl = createStreamingMessage();
  scrollToBottom();

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
    const bodyPayload = { message: text, attachments: atts };
    if (override !== "auto") bodyPayload.model_override = override;

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
      showToast("❌ " + (err.error || "Error"), "error");
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

        if (event.type === "text") {
          fullText += event.text;
          updateStreamingMessage(streamEl, fullText);
          scrollToBottom();

        } else if (event.type === "done") {
          finalizeStreamingMessage(streamEl, fullText, {
            model_tier: event.model_tier,
            model_used: event.model_used,
            cost_usd:   event.cost_usd,
          });
          updateHeaderChips(event.task_type || "general", event.model_tier || "haiku");
          updateInputMeta(event.task_type || "general", event.model_used || "");
          convTitleHeader.textContent = event.title || convTitleHeader.textContent;
          const activeItem = convList.querySelector(`.conv-item[data-id="${currentConvId}"] .conv-item-title`);
          if (activeItem && event.title) activeItem.textContent = event.title;
          updateBudgetUI(event.budget);
          scrollToBottom();

        } else if (event.type === "error") {
          streamEl.remove();
          appendErrorMessage(event.error);
          showToast("❌ " + event.error, "error");
        }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      // User clicked Stop — finalize whatever was received
      if (fullText) {
        finalizeStreamingMessage(streamEl, fullText + "\n\n*[Generation stopped]*", {});
      } else {
        streamEl.remove();
      }
      showToast("⏹ Generation stopped", "info");
    } else {
      console.error("Stream Fetch Error:", err);
      streamEl.remove();
      appendErrorMessage("Network error: " + err.message);
      showToast("⚠️ " + err.message, "error");
    }
  } finally {
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

// ── Streaming DOM helpers ─────────────────────────────────────────────────────
function createStreamingMessage() {
  const el  = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `
    <div class="msg-avatar">✦</div>
    <div class="msg-body">
      <div class="msg-name">Claude</div>
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

  // Cost + model chips
  if (meta.model_tier || meta.cost_usd) {
    const metaEl = document.createElement("div");
    metaEl.className = "msg-meta";
    metaEl.innerHTML = `
      ${meta.model_used ? `<span class="msg-chip ${meta.model_tier}">${meta.model_used}</span>` : ""}
      ${meta.cost_usd  ? `<span class="msg-chip cost">$${meta.cost_usd.toFixed(5)} <span style="opacity:0.7">(₹${(meta.cost_usd * 83.5).toFixed(4)})</span></span>` : ""}`;
    body.appendChild(metaEl);
  }

  // Action buttons
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  actions.innerHTML = `
    <button class="msg-action-btn copy-btn" title="Copy response">📋 Copy</button>
    <button class="msg-action-btn regen-btn" title="Regenerate response">↺ Retry</button>`;
  body.appendChild(actions);

  actions.querySelector(".copy-btn").addEventListener("click", function() {
    navigator.clipboard.writeText(text).then(() => {
      this.textContent = "✓ Copied";
      this.classList.add("copied");
      setTimeout(() => { this.textContent = "📋 Copy"; this.classList.remove("copied"); }, 2000);
    });
  });
  actions.querySelector(".regen-btn").addEventListener("click", () => {
    // Retry: remove last assistant message, re-send last user message
    el.remove();
    const lastUser = [...messagesEl.querySelectorAll(".msg.user")].pop();
    if (lastUser) {
      const content = lastUser.querySelector(".msg-text")?.textContent?.split("\n📎")[0].trim();
      if (content) { msgInput.value = content; window.sendMessage(); }
    }
  });
}

// Also add action buttons to appendMessage (for loaded conversation history)
const _origAppendMessage = appendMessage;
window.appendMessage = function(role, content, meta = {}) {
  const el = _origAppendMessage(role, content, meta);
  if (role === "assistant" && el) {
    const body    = el.querySelector(".msg-body");
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    actions.innerHTML = `<button class="msg-action-btn copy-btn" title="Copy">📋 Copy</button>`;
    body.appendChild(actions);
    actions.querySelector(".copy-btn").addEventListener("click", function() {
      navigator.clipboard.writeText(content).then(() => {
        this.textContent = "✓ Copied";
        this.classList.add("copied");
        setTimeout(() => { this.textContent = "📋 Copy"; this.classList.remove("copied"); }, 2000);
      });
    });
  }
  return el;
};

// ── Projects Feature ────────────────────────────────────────────────────────
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
        <span class="conv-item-icon">🗂</span>
        <div class="conv-item-body">
          <div class="conv-item-title">${escHtml(p.name)}</div>
        </div>
      </div>`;
  });
  list.innerHTML = html;
}

const newProjBtn = document.getElementById("new-project-btn");
if (newProjBtn) {
  newProjBtn.addEventListener("click", async () => {
    const name = prompt("Enter project name:");
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
              <div class="conv-item-title" style="color:var(--text); font-weight:500;">💬 ${escHtml(c.title)}</div>
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
  pFileInput.addEventListener("change", async (e) => {
    if (!currentProjectId || !e.target.files.length) return;
    const file = e.target.files[0];
    const reader = new FileReader();
    reader.onload = async (ev) => {
      let content = ev.target.result;
      try {
        const res = await fetch(`${API}/api/projects/${currentProjectId}/knowledge`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            user_id: currentUser.user_id,
            filename: file.name,
            content: content.slice(0, 50000)
          })
        });
        if (res.ok) {
          showToast("Document added to Knowledge Base", "success");
          window.openProject(currentProjectId); // reload
        }
      } catch (err) { showToast("Failed to upload document", "error"); }
    };
    reader.readAsText(file);
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
      <div style="color:var(--text)">📄 ${escHtml(d.filename)}</div>
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

// ── Claude.ai Clone Features ────────────────────────────────────────────────
window.filterChats = function(query) {
  const term = query.toLowerCase();
  document.querySelectorAll(".conv-item").forEach(item => {
    const title = item.querySelector(".conv-item-title").textContent.toLowerCase();
    item.style.display = title.includes(term) ? "flex" : "none";
  });
};

window.previewArtifact = function(btn, ext) {
  const code = decodeURIComponent(btn.getAttribute("data-code"));
  const pane = document.getElementById("artifacts-pane");
  const iframe = document.getElementById("artifact-iframe");
  const chatContainer = document.getElementById("chat-container");
  
  if (ext === 'html' || ext === 'svg') {
    pane.style.display = "flex";
    chatContainer.style.width = "55%";
    iframe.srcdoc = code;
  }
};

window.closeArtifact = function() {
  document.getElementById("artifacts-pane").style.display = "none";
  document.getElementById("chat-container").style.width = "100%";
};

// Global Keyboard Shortcuts
document.addEventListener("keydown", (e) => {
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
  if (btn) btn.textContent = next === "dark" ? "☀️" : "🌙";
};

// ── Export all calls to CSV ───────────────────────────────────────────────────
window.exportAllCalls = function() {
  window.open(`${API}/api/usage/export`, "_blank");
};
