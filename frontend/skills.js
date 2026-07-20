// ── Skills & Popup Logic ───────────────────────────────────────────────────
window.activeSkill = null;
window.activeStyle = null;
window.webSearchEnabled = false;
let availableSkills = { builtin: [], custom: [] };

// Wires a plus-menu (main chat or welcome screen — they're separate DOM
// trees with parallel IDs, not shared elements) to the shared skills/
// web-search/style behaviors below. Both menus are wired through this one
// function so a new menu item only needs adding once, not once per screen.
function wirePlusMenu(cfg) {
  const plusBtn = document.getElementById(cfg.plusBtn);
  const plusMenu = document.getElementById(cfg.plusMenu);
  if (!plusBtn || !plusMenu) return;

  plusBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isHidden = plusMenu.classList.contains("hidden");
    plusMenu.classList.toggle("hidden");
    if (isHidden) fetchSkills();
  });

  // Only close the menu when clicking outside — don't block any other actions
  document.addEventListener("click", (e) => {
    if (!plusMenu.classList.contains("hidden")) {
      const container = plusBtn.closest(".plus-menu-container");
      if (!container || !container.contains(e.target)) {
        plusMenu.classList.add("hidden");
      }
    }
  });

  const menuUpload = document.getElementById(cfg.upload);
  if (menuUpload) {
    menuUpload.addEventListener("click", (e) => {
      e.stopPropagation();
      plusMenu.classList.add("hidden");
      document.getElementById("file-input")?.click();
    });
  }

  const menuWebSearch = document.getElementById(cfg.webSearch);
  const webSearchCheck = document.getElementById(cfg.webSearchCheck);
  if (menuWebSearch) {
    menuWebSearch.addEventListener("click", (e) => {
      e.stopPropagation();
      window.webSearchEnabled = !window.webSearchEnabled;
      syncWebSearchCheck();
      if (window.webSearchEnabled) {
        addBadge("web-search", "Web search", () => {
          window.webSearchEnabled = false;
          syncWebSearchCheck();
        });
      } else {
        removeBadge("web-search");
      }
      plusMenu.classList.add("hidden");
      updateInputPlaceholder();
    });
  }

  const manageSkillsBtn = document.getElementById(cfg.manageSkills);
  const addSkillBtn = document.getElementById(cfg.addSkill);
  const skillsModal = document.getElementById("skills-modal");

  if (manageSkillsBtn && skillsModal) {
    manageSkillsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      plusMenu.classList.add("hidden");
      openSkillsModal();
    });
  }

  if (addSkillBtn && skillsModal) {
    addSkillBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      plusMenu.classList.add("hidden");
      openSkillsModal();
      document.getElementById("new-skill-name").focus();
    });
  }
}

// Keeps both menus' web-search checkmarks in sync with the single global
// window.webSearchEnabled flag, regardless of which menu toggled it.
function syncWebSearchCheck() {
  ["web-search-check", "welcome-web-search-check"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", !window.webSearchEnabled);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  wirePlusMenu({
    plusBtn: "plus-btn", plusMenu: "plus-menu", upload: "menu-upload",
    webSearch: "menu-web-search", webSearchCheck: "web-search-check",
    manageSkills: "menu-manage-skills", addSkill: "menu-add-skill",
  });
  wirePlusMenu({
    plusBtn: "welcome-plus-btn", plusMenu: "welcome-plus-menu", upload: "welcome-menu-upload",
    webSearch: "welcome-menu-web-search", webSearchCheck: "welcome-web-search-check",
    manageSkills: "welcome-menu-manage-skills", addSkill: "welcome-menu-add-skill",
  });

  // Styles — a single global selector covers both menus' .style-option
  // elements since they share the class; close whichever menu the clicked
  // option actually belongs to.
  document.querySelectorAll(".style-option").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const styleVal = el.dataset.style;
      if (styleVal === "normal") {
        window.activeStyle = null;
        removeBadge("style");
      } else {
        window.activeStyle = styleVal;
        addBadge("style", "Style: " + styleVal, () => {
          window.activeStyle = null;
        });
      }
      el.closest(".plus-menu")?.classList.add("hidden");
      updateInputPlaceholder();
    });
  });

  // Skills Manager Modal
  const skillsModal = document.getElementById("skills-modal");
  const skillsModalClose = document.getElementById("skills-modal-close");
  const saveSkillBtn = document.getElementById("save-skill-btn");

  if (skillsModalClose) {
    skillsModalClose.addEventListener("click", () => {
      skillsModal.classList.add("hidden");
    });
  }

  if (saveSkillBtn) {
    saveSkillBtn.addEventListener("click", async () => {
      const name = document.getElementById("new-skill-name").value.trim();
      const model = document.getElementById("new-skill-model").value;
      const prompt = document.getElementById("new-skill-prompt").value.trim();
      const isShared = document.getElementById("new-skill-shared").checked;

      if (!name || !prompt) {
        if (typeof showToast === "function") showToast("Name and instructions are required.", "error");
        return;
      }
      saveSkillBtn.disabled = true;
      saveSkillBtn.textContent = "Saving...";

      try {
        const res = await fetch(`${API}/api/skills/custom`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: currentUser ? currentUser.user_id : "anonymous",
            name, model, prompt, is_shared: isShared
          })
        });
        if (res.ok) {
          document.getElementById("new-skill-name").value = "";
          document.getElementById("new-skill-prompt").value = "";
          document.getElementById("new-skill-shared").checked = false;
          await fetchSkills();
          renderSkillsManager();
        } else {
          if (typeof showToast === "function") showToast("Error saving skill.", "error");
        }
      } catch (e) {
        if (typeof showToast === "function") showToast("Error saving skill.", "error");
      } finally {
        saveSkillBtn.disabled = false;
        saveSkillBtn.textContent = "Save Skill";
      }
    });
  }
});

let _fetchSkillsInFlight = null;

async function fetchSkills() {
  // Guard against overlapping requests (e.g. rapidly toggling the menu):
  // if one's already in flight, reuse its promise instead of firing another
  // that could resolve out of order and overwrite a newer response.
  if (_fetchSkillsInFlight) return _fetchSkillsInFlight;

  const uid = currentUser ? currentUser.user_id : "anonymous";
  _fetchSkillsInFlight = (async () => {
    try {
      const res = await fetch(`${API}/api/skills?user_id=${encodeURIComponent(uid)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      availableSkills = { builtin: data.builtin || [], custom: data.custom || [] };
      renderSkillsMenu();
    } catch (e) {
      console.error("Failed to load skills", e);
      if (typeof showToast === "function") showToast("Could not load skills", "error");
    } finally {
      _fetchSkillsInFlight = null;
    }
  })();
  return _fetchSkillsInFlight;
}

function renderSkillsMenu() {
  const allSkills = [...availableSkills.custom, ...availableSkills.builtin];

  ["skills-list-container", "welcome-skills-list-container"].forEach(containerId => {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = "";

    allSkills.forEach(sk => {
      const el = document.createElement("div");
      el.className = "menu-item";
      el.textContent = sk.name;
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        window.activeSkill = sk.id;
        addBadge("skill", sk.name, () => {
          window.activeSkill = null;
        });
        el.closest(".plus-menu")?.classList.add("hidden");
        updateInputPlaceholder();
      });
      container.appendChild(el);
    });
  });
}

// Badges live in two separate containers (main chat vs welcome screen —
// parallel DOM trees, not a shared/moved element), so every add/remove
// applies to both to keep whichever screen is visible in sync.
function addBadge(type, text, onRemove) {
  ["active-badges", "welcome-active-badges"].forEach(containerId => {
    const container = document.getElementById(containerId);
    if (!container) return;

    const existing = container.querySelector(`[data-type="${type}"]`);
    if (existing) existing.remove();

    const badge = document.createElement("div");
    badge.className = "skill-badge";
    badge.dataset.type = type;
    badge.innerHTML = `<span>${escHtml(text)}</span> <span class="remove-badge" title="Remove">&times;</span>`;

    badge.querySelector(".remove-badge").addEventListener("click", (e) => {
      e.stopPropagation();
      removeBadge(type);
      onRemove();
      updateInputPlaceholder();
    });

    container.appendChild(badge);
  });
}

function removeBadge(type) {
  ["active-badges", "welcome-active-badges"].forEach(containerId => {
    const container = document.getElementById(containerId);
    if (!container) return;
    const existing = container.querySelector(`[data-type="${type}"]`);
    if (existing) existing.remove();
  });
}

function updateInputPlaceholder() {
  let ph = "Type your message…";

  if (window.activeSkill) {
    const all = [...availableSkills.builtin, ...availableSkills.custom];
    const sk = all.find(s => s.id === window.activeSkill);
    if (sk) {
      ph = `Ask ${sk.name}…`;
    }
  } else if (window.webSearchEnabled) {
    ph = "Ask anything to search the web…";
  }

  ["msg-input", "welcome-input"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.placeholder = ph;
  });
}

function openSkillsModal() {
  const modal = document.getElementById("skills-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  fetchSkills().then(renderSkillsManager);
}

function renderSkillsManager() {
  const list = document.getElementById("skills-manager-list");
  if (!list) return;
  list.innerHTML = "";
  
  if (!availableSkills.custom || availableSkills.custom.length === 0) {
    list.innerHTML = `<div style="color:var(--muted); font-size:0.9rem;">No custom skills created yet.</div>`;
    return;
  }
  
  availableSkills.custom.forEach(sk => {
    const row = document.createElement("div");
    row.className = "skill-row";
    row.innerHTML = `
      <div class="skill-info">
        <div class="skill-name">${escHtml(sk.name)}</div>
        <div class="skill-meta">${sk.is_shared ? "Shared" : "Private"} · ${sk.model === "haiku" ? "Fast" : "Powerful"}</div>
      </div>
      <div class="skill-actions">
        <button onclick="deleteSkill('${sk.id}')">Delete</button>
      </div>
    `;
    list.appendChild(row);
  });
}

window.deleteSkill = async function(skillId) {
  if (!confirm("Are you sure you want to delete this skill?")) return;
  try {
    const uid = currentUser ? currentUser.user_id : "anonymous";
    const res = await fetch(`${API}/api/skills/custom/${skillId}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: uid })
    });
    if (res.ok) {
      await fetchSkills();
      renderSkillsManager();
      if (window.activeSkill === skillId) {
        window.activeSkill = null;
        removeBadge("skill");
        updateInputPlaceholder();
      }
    } else {
      if (typeof showToast === "function") showToast("Error deleting skill", "error");
    }
  } catch (e) {
    if (typeof showToast === "function") showToast("Error deleting skill", "error");
  }
};