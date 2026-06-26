// ── Skills & Popup Logic ───────────────────────────────────────────────────
window.activeSkill = null;
window.activeStyle = null;
window.webSearchEnabled = false;
let availableSkills = { builtin: [], custom: [] };

document.addEventListener("DOMContentLoaded", () => {
  const plusBtn = document.getElementById("plus-btn");
  const plusMenu = document.getElementById("plus-menu");
  
  if (plusBtn && plusMenu) {
    plusBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isHidden = plusMenu.classList.contains("hidden");
      plusMenu.classList.toggle("hidden");
      if (isHidden) {
        fetchSkills();
      }
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
  }

  // Same logic for welcome plus menu
  const welcomePlusBtn = document.getElementById("welcome-plus-btn");
  const welcomePlusMenu = document.getElementById("welcome-plus-menu");
  
  if (welcomePlusBtn && welcomePlusMenu) {
    welcomePlusBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isHidden = welcomePlusMenu.classList.contains("hidden");
      welcomePlusMenu.classList.toggle("hidden");
      if (isHidden) {
        fetchSkills();
      }
    });

    document.addEventListener("click", (e) => {
      if (!welcomePlusMenu.classList.contains("hidden")) {
        const container = welcomePlusBtn.closest(".plus-menu-container");
        if (!container || !container.contains(e.target)) {
          welcomePlusMenu.classList.add("hidden");
        }
      }
    });
  }

  // File upload via menu
  const menuUpload = document.getElementById("menu-upload");
  if (menuUpload) {
    menuUpload.addEventListener("click", (e) => {
      e.stopPropagation();
      if(plusMenu) plusMenu.classList.add("hidden");
      document.getElementById("file-input")?.click();
    });
  }
  
  const welcomeMenuUpload = document.getElementById("welcome-menu-upload");
  if (welcomeMenuUpload) {
    welcomeMenuUpload.addEventListener("click", (e) => {
      e.stopPropagation();
      if(welcomePlusMenu) welcomePlusMenu.classList.add("hidden");
      document.getElementById("file-input")?.click();
    });
  }

  // Web search toggle
  const menuWebSearch = document.getElementById("menu-web-search");
  const webSearchCheck = document.getElementById("web-search-check");
  if (menuWebSearch) {
    menuWebSearch.addEventListener("click", (e) => {
      e.stopPropagation();
      window.webSearchEnabled = !window.webSearchEnabled;
      if (window.webSearchEnabled) {
        webSearchCheck.classList.remove("hidden");
        addBadge("web-search", "Web search", () => {
          window.webSearchEnabled = false;
          webSearchCheck.classList.add("hidden");
        });
      } else {
        webSearchCheck.classList.add("hidden");
        removeBadge("web-search");
      }
      plusMenu.classList.add("hidden");
      updateInputPlaceholder();
    });
  }

  // Styles
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
      plusMenu.classList.add("hidden");
      updateInputPlaceholder();
    });
  });

  // Skills Manager Modal
  const manageSkillsBtn = document.getElementById("menu-manage-skills");
  const addSkillBtn = document.getElementById("menu-add-skill");
  const skillsModal = document.getElementById("skills-modal");
  const skillsModalClose = document.getElementById("skills-modal-close");
  const saveSkillBtn = document.getElementById("save-skill-btn");

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
        alert("Name and instructions are required.");
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
          alert("Error saving skill.");
        }
      } catch (e) {
        alert("Error saving skill.");
      } finally {
        saveSkillBtn.disabled = false;
        saveSkillBtn.textContent = "Save Skill";
      }
    });
  }
});

async function fetchSkills() {
  const uid = currentUser ? currentUser.user_id : "anonymous";
  try {
    const res = await fetch(`${API}/api/skills?user_id=${encodeURIComponent(uid)}`);
    const data = await res.json();
    availableSkills = data;
    renderSkillsMenu();
  } catch (e) {
    console.error("Failed to load skills", e);
  }
}

function renderSkillsMenu() {
  const container = document.getElementById("skills-list-container");
  if (!container) return;
  container.innerHTML = "";
  
  const allSkills = [...availableSkills.custom, ...availableSkills.builtin];
  
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
      document.getElementById("plus-menu").classList.add("hidden");
      updateInputPlaceholder();
    });
    container.appendChild(el);
  });
}

function addBadge(type, text, onRemove) {
  const container = document.getElementById("active-badges");
  if (!container) return;
  
  // Remove existing of same type
  const existing = container.querySelector(`[data-type="${type}"]`);
  if (existing) existing.remove();

  const badge = document.createElement("div");
  badge.className = "skill-badge";
  badge.dataset.type = type;
  badge.innerHTML = `<span>${escHtml(text)}</span> <span class="remove-badge" title="Remove">&times;</span>`;
  
  badge.querySelector(".remove-badge").addEventListener("click", (e) => {
    e.stopPropagation();
    badge.remove();
    onRemove();
    updateInputPlaceholder();
  });
  
  container.appendChild(badge);
}

function removeBadge(type) {
  const container = document.getElementById("active-badges");
  if (!container) return;
  const existing = container.querySelector(`[data-type="${type}"]`);
  if (existing) existing.remove();
}

function updateInputPlaceholder() {
  const msgInput = document.getElementById("msg-input");
  if (!msgInput) return;
  
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
  
  msgInput.placeholder = ph;
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
      alert("Error deleting skill");
    }
  } catch (e) {
    alert("Error deleting skill");
  }
};