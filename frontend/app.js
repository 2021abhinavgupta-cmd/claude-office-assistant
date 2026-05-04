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
      openConversation(item.dataset.id);
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

async function startNewChat(initialMessage = null) {
  if (!currentUser) { showToast("Please select a user first", "error"); return; }

  try {
    const res  = await fetch(`${API}/api/conversations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: currentUser.user_id, user_name: currentUser.user_name }),
    });
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
    showToast("Could not create conversation", "error");
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

newChatBtn.addEventListener("click", () => startNewChat());

// ── Sending Messages ──────────────────────────────────────────────────────────
async function sendMessage() {
  const text = msgInput.value.trim();
  if (!text || isLoading || !currentConvId) return;

  msgInput.value = "";
  msgInput.style.height = "auto";
  charCount.textContent = "";
  setLoading(true);

  appendMessage("user", text);
  const typingId = appendTyping();
  scrollToBottom();

  try {
    const res = await fetch(`${API}/api/conversations/${currentConvId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
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
  welcomeScreen.classList.add("hidden");
  chatView.classList.remove("hidden");
}
function showWelcomeScreen() {
  chatView.classList.add("hidden");
  welcomeScreen.classList.remove("hidden");
}

function updateHeaderChips(task, modelTier) {
  taskChip.textContent  = task.replace(/_/g, " ");
  modelChip.textContent = modelTier;
  modelChip.className   = `model-chip ${modelTier}`;
}
function updateInputMeta(task, modelName) {
  metaTask.textContent  = task.replace(/_/g, " ");
  metaModel.textContent = modelName || "claude-haiku-4-5";
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
        ${meta.cost_usd   ? `<span class="msg-chip cost">$${meta.cost_usd.toFixed(5)}</span>` : ""}
       </div>`
    : "";

  el.innerHTML = `
    ${avatar}
    <div class="msg-body">
      <div class="msg-name">${escHtml(name)}</div>
      <div class="msg-text">${formatText(content)}</div>
      ${metaHtml}
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

// ── Text Formatting (markdown-lite) ──────────────────────────────────────────
function formatText(text) {
  const blocks = [];
  // Extract code blocks first
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const ph = `\x00BLK${blocks.length}\x00`;
    blocks.push(`<pre><code class="language-${lang}">${escHtml(code.trim())}</code></pre>`);
    return ph;
  });

  text = text.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  text = text.replace(/\*\*(.+?)\*\*/gs, "<strong>$1</strong>");
  text = text.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
  text = text.replace(/^### (.+)$/gm, "<h4>$1</h4>");
  text = text.replace(/^## (.+)$/gm,  "<h3>$1</h3>");

  // Bullet + numbered lists
  text = text.replace(/(?:^[•\-\*] .+$\n?)+/gm, m => {
    const items = m.trim().split("\n").map(l => `<li>${l.replace(/^[•\-\*] /, "")}</li>`).join("");
    return `<ul>${items}</ul>`;
  });
  text = text.replace(/(?:^\d+\. .+$\n?)+/gm, m => {
    const items = m.trim().split("\n").map(l => `<li>${l.replace(/^\d+\.\s*/, "")}</li>`).join("");
    return `<ol>${items}</ol>`;
  });

  text = text.replace(/\n\n+/g, "</p><p>");
  text = text.replace(/\n/g, "<br>");
  text = `<p>${text}</p>`;

  // Restore code blocks
  blocks.forEach((b, i) => { text = text.replace(`\x00BLK${i}\x00`, b); });
  return text;
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
  budgetMiniVal.textContent = `$${spent.toFixed(2)} / $${limit}`;
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
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
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

async function uploadFile(file) {
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

// Override sendMessage to include attachments
const _origSendMessage = sendMessage;
window.sendMessage = async function() {
  // Grab attachments snapshot before clearing
  const atts = [...pendingAttachments];

  // Inline the send logic with attachments
  const text = msgInput.value.trim();
  if (!text || isLoading || !currentConvId) return;

  msgInput.value = "";
  msgInput.style.height = "auto";
  charCount.textContent = "";
  clearAttachments();
  setLoading(true);

  // Show user message with file indicators
  const displayText = atts.length
    ? text + `\n\n📎 _${atts.length} file(s) attached: ${atts.map(a => a.filename).join(", ")}_`
    : text;
  appendMessage("user", displayText);
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
        model_tier: data.model_tier, model_used: data.model_used, cost_usd: data.cost_usd,
      });
      const task  = data.task_type || "general";
      const model = data.model_tier || "haiku";
      updateHeaderChips(task, model);
      updateInputMeta(task, data.model_used || "");
      convTitleHeader.textContent = data.title || convTitleHeader.textContent;
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
};

// Re-bind send button and enter key to the new sendMessage
sendBtn.addEventListener("click", window.sendMessage);
msgInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); window.sendMessage(); }
});

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

  try {
    const response = await fetch(`${API}/api/conversations/${currentConvId}/stream`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ message: text, attachments: atts }),
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
    streamEl.remove();
    appendErrorMessage("Could not connect to server. Is Flask running?");
    showToast("⚠️ Server offline", "error");
  } finally {
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
      ${meta.cost_usd  ? `<span class="msg-chip cost">$${meta.cost_usd.toFixed(5)}</span>` : ""}`;
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
