
const API = location.hostname==="localhost"||location.hostname==="127.0.0.1" ? "http://localhost:5000" : location.origin;
const user = JSON.parse(localStorage.getItem("agency_portal_user")||"{}");
const UID = user.user_id;
if (!UID) window.location.href = "login.html";
const IS_ADMIN = user.is_admin === true;

function esc(s){if(!s)return"";return s.replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"})[m]);}

(async()=>{
  await loadEmployees();
  const hour = new Date().getHours();
  const greet = hour<12?"Good morning":hour<17?"Good afternoon":"Good evening";
  document.getElementById("greeting-line").textContent = `${greet}, ${window.EMP_DICT[UID]||UID}! Your task list is ready.`;

  // Fire critical page loads immediately — don't wait for Notion
  loadMyTasks();
  loadTeamStandups();
  loadAlerts();
  loadVelocity();
  setInterval(loadTeamStandups, 30000);

  // Load Notion tasks in background (non-blocking, with 5s timeout)
  (async () => {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 5000);
      const nr = await fetch(`${API}/api/notion/tasks?assigned_to=${window.EMP_DICT[UID]||UID}`, {signal: ctrl.signal});
      clearTimeout(timer);
      const nd = await nr.json();
      if(nd.tasks) {
        window.notionTasks = nd.tasks.filter(t => t.status === 'not_started' || t.status === 'in_progress');
        const dl = document.getElementById("notion-tasks-list");
        if(dl) dl.innerHTML = window.notionTasks.map(t => `<option value="${t.title}">`).join("");
      }
    } catch(e){}
  })();
})();

// ── Personal Task Tracker ─────────────────────────────────────────────────────

let allEmployees = [];

async function loadMyTasks(){
  if(!allEmployees.length) {
    try {
      const re = await fetch(`${API}/api/employees`);
      const de = await re.json();
      allEmployees = de.employees || [];
    } catch(e){}
  }
  const r = await fetch(`${API}/api/standup/my-tasks?user_id=${UID}`);
  const d = await r.json();
  allTasks = d.tasks || [];
  renderTasks();
}

function renderTasks(){
  const list = document.getElementById("task-list");
  const stats = document.getElementById("task-stats");
  const done  = allTasks.filter(t=>t.status==="done").length;
  const pend  = allTasks.filter(t=>t.status==="pending").length;

  if(allTasks.length === 0){
    list.innerHTML = '<div class="empty-tasks">No tasks yet — add your first task above!</div>';
    stats.style.display = "none";
    return;
  }
  stats.style.display = "flex";
  document.getElementById("stat-done").textContent = `${done} done`;
  document.getElementById("stat-pend").textContent = `${pend} remaining`;

  list.innerHTML = allTasks.map(t=>{
    let subtasksHtml = "";
    if (t.notion_id) {
       const stList = t.subtasks || [];
       subtasksHtml = `
         <div style="margin-top:8px; display:flex; flex-direction:column; gap:4px; padding-left:24px; border-left:2px solid var(--bdr);">
           ${stList.map((st, i) => `
             <div style="display:flex; align-items:center; gap:6px; font-size:0.75rem;">
               <input type="checkbox" ${st.done?'checked':''} onchange="toggleSubtask(${t.id}, ${i}, this.checked)" style="accent-color:var(--acc)" />
               <span style="${st.done?'text-decoration:line-through;color:var(--muted)':''}">${esc(st.title)}</span>
               <button onclick="deleteSubtask(${t.id}, ${i})" style="background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:0.7rem;margin-left:auto">✕</button>
             </div>
           `).join("")}
           <div style="display:flex; gap:6px; margin-top:4px;">
             <input type="text" id="subtask-input-${t.id}" placeholder="Add subtask..." onkeydown="if(event.key==='Enter')addSubtask(${t.id})" style="background:transparent;border:1px dashed var(--bdr);border-radius:4px;color:var(--txt);padding:3px 6px;font-size:0.7rem;flex:1;outline:none;" />
           </div>
         </div>
       `;
    }

    return `
    <div class="task-item ${t.status==="done"?"done":""} ${t.carried_from?"carried":""}" id="task-${t.id}" style="align-items:flex-start; flex-direction:column;">
      <div style="display:flex; width:100%; align-items:flex-start; gap:10px;">
        <button class="task-check ${t.status==="done"?"checked":""}" onclick="toggleTask(${t.id},'${t.status==="done"?"pending":"done"}')" style="margin-top:2px">
          ${t.status==="done"?"✓":""}
        </button>
        <div style="flex:1;display:flex;flex-direction:column;gap:6px">
          <div style="display:flex;align-items:center;gap:8px">
            <input type="text" class="task-title-input" value="${esc(t.title)}" 
                   onchange="updateTaskTitle(${t.id}, this.value)"
                   style="background:transparent; border:none; outline:none; color:inherit; font-size:0.88rem; font-family:inherit; flex:1; text-decoration:inherit; padding: 0;"
                   ${t.status==="done" || t.status==="delegated" ? "disabled" : ""} />
            ${t.carried_from?`<span class="carried-badge">↩ from ${t.carried_from}</span>`:""}
            ${t.delegated_from?`<span class="carried-badge" style="background:#8b5cf6;color:#fff;border:none">Delegated from ${t.delegated_from}</span>`:""}
            ${t.status==="delegated"?`<span class="carried-badge" style="background:#8b5cf6;color:#fff;border:none">Delegated to ${t.delegated_to}</span>`:""}
            ${t.status==="pending" ? `
              <select class="delegate-select" onchange="delegateTask(${t.id}, this.value, this.options[this.selectedIndex].text)" style="background:transparent; border:1px dashed var(--bdr); border-radius:4px; color:var(--muted); font-size:0.7rem; outline:none; max-width: 100px;">
                <option value="">Assign...</option>
                ${allEmployees.filter(e=>e.id!==UID).map(e=>`<option value="${e.id}">${e.name}</option>`).join("")}
              </select>
            ` : ""}
          </div>
          ${t.status==="pending" ? `
            <input type="text" placeholder="Add a blocker (optional)..." 
                   value="${esc(t.blocker)}" onchange="updateBlocker(${t.id}, this.value)"
                   style="background:rgba(255,92,92,0.1); border:1px solid rgba(255,92,92,0.3); border-radius:4px; color:var(--txt); padding:4px 8px; font-size:0.75rem; width:100%; outline:none; font-family:'DM Sans',sans-serif;" />
          ` : (t.blocker ? `<div style="font-size:0.75rem;color:var(--red);"> Blocker: ${esc(t.blocker)}</div>` : "")}
        </div>
        <div style="display:flex; align-items:center; gap:4px">
          ${!t.notion_id ? `<button onclick="pushToBoard(${t.id}, this)" title="Push to Notion Board" style="background:none;border:none;color:var(--acc);cursor:pointer;font-size:1.1rem;padding:2px;transition:opacity 0.2s;">↗</button>` : ''}
          <button class="task-del" onclick="deleteTask(${t.id})" title="Remove">✕</button>
        </div>
      </div>
      ${subtasksHtml}
    </div>`
  }).join("");
}

async function addSubtask(taskId) {
  const inp = document.getElementById(`subtask-input-${taskId}`);
  const title = inp.value.trim();
  if(!title) return;
  const t = allTasks.find(x=>x.id===taskId);
  if(!t) return;
  
  if(!t.subtasks) t.subtasks = [];
  t.subtasks.push({ title: title, done: false });
  
  await fetch(`${API}/api/standup/my-tasks/${taskId}`,{
    method:"PATCH",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({subtasks: t.subtasks})
  });
  renderTasks();
}

async function toggleSubtask(taskId, index, done) {
  const t = allTasks.find(x=>x.id===taskId);
  if(!t || !t.subtasks || !t.subtasks[index]) return;
  
  t.subtasks[index].done = done;
  
  await fetch(`${API}/api/standup/my-tasks/${taskId}`,{
    method:"PATCH",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({subtasks: t.subtasks})
  });
  renderTasks();
}

async function deleteSubtask(taskId, index) {
  const t = allTasks.find(x=>x.id===taskId);
  if(!t || !t.subtasks) return;
  
  t.subtasks.splice(index, 1);
  
  await fetch(`${API}/api/standup/my-tasks/${taskId}`,{
    method:"PATCH",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({subtasks: t.subtasks})
  });
  renderTasks();
}

async function delegateTask(taskId, targetUserId, targetUserName) {
  if(!targetUserId) return;
  try {
    await fetch(`${API}/api/standup/tasks/${taskId}/delegate`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({target_user_id: targetUserId, target_user_name: targetUserName})
    });
    loadMyTasks();
  } catch(e) {
    console.error("Delegate failed", e);
  }
}

async function addTask(btnEl){
  const inp = document.getElementById("new-task-input");
  const title = inp.value.trim();
  if(!title) return;
  inp.value = "";
  
  const origBtnText = btnEl ? btnEl.textContent : "+ Add";
  if(btnEl) btnEl.textContent = "⏳...";
  
  try {
    const r = await fetch(`${API}/api/standup/smart-add`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({user_id:UID, title:title, assigned_to:window.EMP_DICT[UID]||UID})
    });
    const d = await r.json();
    if(d.success){
      allTasks.push({id:d.task_id, title:d.title, status:"pending", carried_from:null, notion_id:d.notion_id});
      renderTasks();
      if(d.is_project) {
          // Toast or subtle indicator could go here. 
          console.log("AI Auto-Router created this as a project task in Notion.");
      }
    }
  } catch(e) {}
  
  if(btnEl) btnEl.textContent = origBtnText;
}

async function pushToBoard(id, btn){
  btn.style.opacity = "0.5";
  btn.textContent = "⏳";
  try {
    const r = await fetch(`${API}/api/standup/push-to-notion/${id}`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({assigned_to:window.EMP_DICT[UID]||UID})
    });
    const d = await r.json();
    if(d.success) {
      const t = allTasks.find(x=>x.id===id);
      if(t) t.notion_id = d.notion_id;
      renderTasks();
    } else {
      alert("Error pushing to Notion.");
      btn.textContent = "↗";
      btn.style.opacity = "1";
    }
  } catch(e){
      alert("Network error.");
      btn.textContent = "↗";
      btn.style.opacity = "1";
  }
}

async function autoFillStandup(pull_upcoming = false) {
  const btn = document.getElementById("autofill-btn");
  btn.disabled = true;
  btn.textContent = "⏳ Fetching...";
  try {
    const r = await fetch(`${API}/api/standup/auto-fill`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ 
        user_id: UID, 
        assigned_name: window.EMP_DICT[UID] || UID,
        pull_upcoming: pull_upcoming
      })
    });
    const d = await r.json();
    if (d.success) {
      if (d.added > 0) {
        alert(` Added ${d.added} task(s) from Notion to your standup!`);
        loadMyTasks();
      } else {
        if (!pull_upcoming) {
            if(confirm("No active or overdue tasks found.\n\nWould you like to pull in your upcoming (Not Started) tasks to work on today?")) {
                autoFillStandup(true);
                return;
            }
        } else {
            alert("No upcoming tasks found in Notion.");
        }
      }
    } else {
      alert("Error: " + (d.error || "Could not auto-fill"));
    }
  } catch (err) {
    alert("Network error.");
  } finally {
    btn.disabled = false;
    btn.textContent = " Auto-Fill";
  }
}


async function toggleTask(id, newStatus){
  const t = allTasks.find(x=>x.id===id);
  let payload = { status: newStatus };
  
  if(newStatus === "done" && t && t.notion_id) {
    let pct = prompt(`This task is linked to Notion.\nHow much of the total task is complete now? (0-100)`, "100");
    if (pct === null) return; // User cancelled
    let parsed = parseInt(pct, 10);
    if (!isNaN(parsed) && parsed >= 0 && parsed <= 100) {
      payload.progress = parsed;
    }
  }

  const item = document.getElementById(`task-${id}`);
  if(item) item.style.opacity="0.5";
  
  await fetch(`${API}/api/standup/my-tasks/${id}`,{
    method:"PATCH",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)
  });
  
  if(t) t.status = newStatus;
  renderTasks();
}

async function deleteTask(id){
  await fetch(`${API}/api/standup/my-tasks/${id}`,{method:"DELETE"});
  allTasks = allTasks.filter(x=>x.id!==id);
  renderTasks();
}

async function wrapUpDay(){
  const pend = allTasks.filter(t=>t.status==="pending");
  const btn  = document.getElementById("wrap-btn");
  const conf = document.getElementById("wrap-confirm");
  if(pend.length===0){
    conf.style.display="block";
    conf.textContent = " All tasks done! Great work today.";
    return;
  }
  btn.disabled = true;
  btn.querySelector("span").textContent = "Carrying over…";
  const r = await fetch(`${API}/api/standup/carry-over`,{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({user_id:UID})
  });
  const d = await r.json();
  btn.disabled = false;
  btn.querySelector("span").textContent = " Wrap Up Day — carry unfinished to tomorrow";
  if(d.success){
    conf.style.display = "block";
    conf.textContent = d.carried>0
      ? ` Done! ${d.carried} task${d.carried>1?"s":""} carried over to tomorrow (${d.date}).`
      : " All tasks were already done — nothing to carry over!";
  }
}

async function updateBlocker(id, blockerText) {
  await fetch(`${API}/api/standup/my-tasks/${id}`,{
    method:"PATCH",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({blocker: blockerText})
  });
  const t = allTasks.find(x=>x.id===id);
  if(t) t.blocker = blockerText;
  loadTeamStandups();
}

async function updateTaskTitle(id, newTitle) {
  const title = newTitle.trim();
  if(!title) return;
  await fetch(`${API}/api/standup/my-tasks/${id}`,{
    method:"PATCH",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({title: title})
  });
  const t = allTasks.find(x=>x.id===id);
  if(t) t.title = title;
  loadTeamStandups();
}

// ── Team standups panel ───────────────────────────────────────────────────────

async function loadTeamStandups(dateStr = ""){
  if (!window.EMPLOYEES || window.EMPLOYEES.length === 0) {
    await loadEmployees();
  }
  if (!window.EMPLOYEES || window.EMPLOYEES.length === 0) {
    document.getElementById("team-standups").innerHTML = '<div class="empty">Could not load employee list. Please refresh.</div>';
    return;
  }
  const refreshBtn = document.getElementById("refresh-team-btn");
  if (refreshBtn) { refreshBtn.textContent = " ⏳"; refreshBtn.disabled = true; }
  const query = dateStr ? `?date=${dateStr}` : "";
  const r = await fetch(`${API}/api/standup/today${query}`);
  const data = await r.json();
  const today = data.date||new Date().toISOString().slice(0,10);
  
  if (!dateStr && document.getElementById("history-date")) {
      document.getElementById("history-date").value = today;
  }
  
  const submitted=new Map(data.standups.map(s=>[s.user_id,s]));
  const tasksByUser = data.tasks_by_user || {};
  const wrap=document.getElementById("team-standups");
  const t=iso=>iso?new Date(iso).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"}):"";
  
  wrap.innerHTML=window.EMPLOYEES.map(e=>{
    const s=submitted.get(e.id);
    const tasks = tasksByUser[e.id] || [];
    const name=window.EMP_DICT[e.id]||e.id;
    
    if(!s && tasks.length === 0){
        return`<div class="not-sub"><span style="font-size:1rem">⏳</span><span><strong>${name}</strong> hasn't started today's standup yet</span></div>`;
    }
    
    let taskHtml = "";
    if(tasks.length > 0){
        const newTasks = tasks.filter(t => !t.carried_from);
        const carriedTasks = tasks.filter(t => t.carried_from);

        taskHtml = `<div class="sc-field" style="margin-top:4px">`;
        
        if (newTasks.length > 0) {
            taskHtml += `<div class="fl" style="margin-top:6px"> New Tasks</div>`;
            taskHtml += `<ul style="list-style:none;padding-left:4px;font-size:.82rem;color:var(--txt)">`;
            newTasks.forEach(task => {
                const isDone = task.status === "done";
                taskHtml += `<li style="${isDone ? 'text-decoration:line-through;color:var(--muted);' : 'margin-bottom:6px'}">
                    ${isDone ? '✓' : '•'} ${esc(task.title)}
                    ${task.blocker ? `<div style="color:var(--red);font-size:0.75rem;margin-left:14px;margin-top:2px;text-decoration:none;"> Blocker: ${esc(task.blocker)}</div>` : ""}
                </li>`;
            });
            taskHtml += `</ul>`;
        }

        if (carriedTasks.length > 0) {
            taskHtml += `<div class="fl" style="margin-top:10px; color:var(--acc);"> Carried Over</div>`;
            taskHtml += `<ul style="list-style:none;padding-left:4px;font-size:.82rem;color:var(--txt)">`;
            carriedTasks.forEach(task => {
                const isDone = task.status === "done";
                let daysOverdue = 0;
                if (task.carried_from) {
                    const d1 = new Date(task.carried_from);
                    const d2 = new Date(today);
                    daysOverdue = Math.max(1, Math.floor((d2 - d1) / (1000 * 60 * 60 * 24)));
                }
                const overdueBadge = !isDone && daysOverdue > 0 
                    ? `<span style="background:rgba(255,92,92,0.15); color:#ff5c5c; padding:2px 6px; border-radius:4px; font-size:11px; margin-left:6px">${daysOverdue} day${daysOverdue !== 1 ? 's' : ''} overdue</span>` 
                    : '';

                taskHtml += `<li style="${isDone ? 'text-decoration:line-through;color:var(--muted);' : 'margin-bottom:6px; color:var(--acc);'}">
                    ${isDone ? '✓' : '⚠'} ${esc(task.title)} ${overdueBadge}
                    ${task.blocker ? `<div style="color:var(--red);font-size:0.75rem;margin-left:22px;margin-top:2px;text-decoration:none;"> Blocker: ${esc(task.blocker)}</div>` : ""}
                </li>`;
            });
            taskHtml += `</ul>`;
        }

        taskHtml += `</div>`;
    }

    const timeStr = s ? `Submitted ${t(s.submitted_at)}` : `Started updating task list`;

    return`<div class="standup-card">
      <div class="sc-header">
        <div class="sc-avatar">${name[0].toUpperCase()}</div>
        <div><div class="sc-name">${name}</div><div class="sc-time">${timeStr}</div></div>
      </div>
      <div class="sc-body">
        ${s && s.yesterday?`<div class="sc-field"><div class="fl"> Done Yesterday</div><div class="fv">${esc(s.yesterday)}</div></div>`:""}
        ${s && s.today?`<div class="sc-field"><div class="fl"> Today's Plan</div><div class="fv">${esc(s.today)}</div></div>`:""}
        ${taskHtml}
        ${s && s.blockers?`<div class="sc-field blocker"><div class="fl"> Blockers</div><div class="fv">${esc(s.blockers)}</div></div>`:""}
      </div></div>`;
  }).join("");
  const refreshBtn = document.getElementById("refresh-team-btn");
  if (refreshBtn) { refreshBtn.textContent = " ↻ Refresh"; refreshBtn.disabled = false; }
}

// ── Alerts (admin) ────────────────────────────────────────────────────────────

async function loadAlerts(){
  if(!IS_ADMIN)return;
  document.getElementById("alerts-panel").style.display="block";
  const r=await fetch(`${API}/api/alerts?user_id=${UID}`);
  const data=await r.json();
  const alerts=data.alerts||[];
  document.getElementById("alert-count").textContent=alerts.length?`(${alerts.length} active)`:"(none)";
  const wrap=document.getElementById("alerts-list");
  if(!alerts.length){wrap.innerHTML='<div class="empty" style="text-align:left;padding:10px 0"> No at-risk or critical tasks.</div>';return;}
  wrap.innerHTML=alerts.map(a=>`<div class="alert-card alert-${a.risk_level}">
    <div><div class="alert-title">${a.assignee_name} — ${a.title}</div><div class="alert-sub"> ${a.client_name} · Due: ${a.due_date||"N/A"}</div></div>
    <span class="risk-pill rp-${a.risk_level}">${a.risk_level==="critical"?" CRITICAL":" AT RISK"}</span>
  </div>`).join("");
}

async function runCheck(){
  const r=await fetch(`${API}/api/alerts/run-check`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:UID})});
  const d=await r.json();
  alert(`Check complete. ${d.alerts_fired||0} new alert(s) fired.`);
  loadAlerts();
}

// ── Velocity Tracking ─────────────────────────────────────────────────────────
let _velocityChart = null;

async function loadVelocity() {
  const scope = document.getElementById("velocity-scope")?.value || "me";
  const days  = document.getElementById("velocity-days")?.value  || 14;
  const uid   = scope === "me" ? `&user_id=${UID}` : "";

  try {
    const r = await fetch(`${API}/api/standup/velocity?days=${days}${uid}`);
    const d = await r.json();
    const data = d.velocity || [];

    const canvas  = document.getElementById("velocity-chart");
    const emptyEl = document.getElementById("velocity-empty");

    if (!data.length) {
      canvas.style.display = "none";
      emptyEl.style.display = "block";
      return;
    }
    canvas.style.display = "block";
    emptyEl.style.display = "none";

    const labels    = data.map(d => d.date.slice(5));   // MM-DD
    const completed = data.map(d => d.completed);
    const carried   = data.map(d => d.carried);

    if (_velocityChart) _velocityChart.destroy();

    _velocityChart = new Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Completed ",
            data: completed,
            backgroundColor: "rgba(34,211,160,0.75)",
            borderRadius: 4,
            borderSkipped: false,
          },
          {
            label: "Carried Over ↩",
            data: carried,
            backgroundColor: "rgba(245,166,35,0.65)",
            borderRadius: 4,
            borderSkipped: false,
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "#e8e8f0", font: { size: 11 } } },
          tooltip: { mode: "index", intersect: false }
        },
        scales: {
          x: {
            stacked: false,
            ticks: { color: "#6b6b8a", font: { size: 10 } },
            grid:  { color: "rgba(255,255,255,0.04)" }
          },
          y: {
            stacked: false,
            beginAtZero: true,
            ticks: { color: "#6b6b8a", stepSize: 1 },
            grid:  { color: "rgba(255,255,255,0.06)" }
          }
        }
      }
    });
  } catch(e) {
    console.error("Velocity load failed", e);
  }
}
