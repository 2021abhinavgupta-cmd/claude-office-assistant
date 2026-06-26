document.addEventListener("DOMContentLoaded", async () => {
  const grid = document.getElementById("projects-grid");
  const newBtn = document.getElementById("main-new-proj-btn");
  const searchInput = document.getElementById("projects-search-input");
  
  const modal = document.getElementById("project-modal");
  const cancelBtn = document.getElementById("project-cancel-btn");
  const createBtn = document.getElementById("project-create-btn");
  const nameInput = document.getElementById("project-name-input");
  const descInput = document.getElementById("project-desc-input");

  let allProjects = [];

  const API = (() => {
    const h = window.location.hostname;
    if (!h || h === 'localhost' || h === '127.0.0.1') return 'http://localhost:5000';
    return window.location.origin;
  })();

  const authDataStr = localStorage.getItem("authData");
  if (!authDataStr) {
    window.location.href = "login.html";
    return;
  }
  const authData = JSON.parse(authDataStr);
  const userId = authData.user_id;

  async function loadProjects() {
    try {
      const res = await fetch(`${API}/api/projects?user_id=${encodeURIComponent(userId)}`);
      if (!res.ok) throw new Error("Failed to load projects");
      const data = await res.json();
      allProjects = data.projects || [];
      renderProjects(allProjects);
    } catch (e) {
      console.error(e);
      grid.innerHTML = `<div style="color:var(--red);">Error loading projects.</div>`;
    }
  }

  function renderProjects(list) {
    if (list.length === 0) {
      grid.innerHTML = `<div style="color:var(--muted); font-size:0.95rem;">No projects yet. Create one to get started.</div>`;
      return;
    }

    grid.innerHTML = list.map(p => {
      const date = new Date(p.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
      return `
        <a href="project.html?id=${p.id}" style="display:flex; flex-direction:column; justify-content:space-between; background:var(--surface2); border:1px solid rgba(255,255,255,0.05); border-radius:12px; padding:24px; text-decoration:none; color:var(--text); min-height:160px; transition:border-color 0.2s, background 0.2s;" onmouseover="this.style.background='var(--surface)'; this.style.borderColor='var(--border)'" onmouseout="this.style.background='var(--surface2)'; this.style.borderColor='rgba(255,255,255,0.05)'">
          <div>
            <h3 style="font-size:1.1rem; font-weight:600; margin-bottom:8px;">${p.name}</h3>
            ${p.instructions ? `<p style="font-size:0.85rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical;">${p.instructions}</p>` : ''}
          </div>
          <div style="font-size:0.8rem; color:var(--text-2); margin-top:16px;">
            Updated ${date}
          </div>
        </a>
      `;
    }).join("");
  }

  searchInput.addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase();
    if (!q) {
      renderProjects(allProjects);
      return;
    }
    const filtered = allProjects.filter(p => p.name.toLowerCase().includes(q) || (p.instructions || "").toLowerCase().includes(q));
    renderProjects(filtered);
  });

  // Modal logic
  newBtn.addEventListener("click", () => {
    nameInput.value = "";
    descInput.value = "";
    modal.style.display = "flex";
    modal.classList.remove("hidden");
    setTimeout(() => nameInput.focus(), 50);
  });

  cancelBtn.addEventListener("click", () => {
    modal.style.display = "none";
    modal.classList.add("hidden");
  });

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
          user_id: userId,
          name: name,
          custom_instructions: descInput.value.trim()
        })
      });
      if (!res.ok) throw new Error("Failed to create project");
      const p = await res.json();
      window.location.href = `project.html?id=${p.id}`;
    } catch (e) {
      console.error(e);
      alert("Error creating project");
      createBtn.disabled = false;
      createBtn.textContent = "Create project";
    }
  });

  await loadProjects();
});
