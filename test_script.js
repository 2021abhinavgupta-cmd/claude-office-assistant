
const API = location.hostname==="localhost"||location.hostname==="127.0.0.1" ? "http://localhost:5000" : location.origin;
const user = JSON.parse(localStorage.getItem("claude_office_user")||"{}");
const UID = user.user_id || user.id;
if (!UID) { window.location.href = "login.html"; }

const USER_NAME = user.user_name || user.name || "";

document.getElementById("greeting").textContent = `Showing tasks for ${USER_NAME || "you"}`;

function toast(m,t="ok"){const e=document.getElementById("toast");e.textContent=m;e.className=t;e.style.display="block";setTimeout(()=>e.style.display="none",3500)}

// Normalise Notion status strings
function normStatus(s){
  if(!s) return "not_started";
  const map={"not started":"not_started","in progress":"in_progress","in_review":"submitted","pending review":"submitted","done":"approved"};
  const lower=s.toLowerCase().replace(/[-\s]+/g,"_");
  return map[s.toLowerCase()]||lower;
}

function spLabel(s){return{not_started:"Not Started",in_progress:"In Progress 🔄",submitted:"⏳ In Review",approved:"✅ Approved"}[s]||s}
function progressPct(s){return{not_started:0,in_progress:40,submitted:75,approved:100}[s]??0}

let useNotion = false;
let currentViewMode = 'status';
window.allTasks = [];

// Calendar state
let calMonth = new Date().getMonth();
let calYear = new Date().getFullYear();

function setViewMode(mode) {
  currentViewMode = mode;
  document.querySelectorAll('.kgroup-btn').forEach(b => b.classList.remove('on'));
  document.getElementById('btn-view-' + mode).classList.add('on');
  renderTasks();
}

function changeCalMonth(dir) {
  calMonth += dir;
  if (calMonth > 11) { calMonth = 0; calYear++; }
  if (calMonth < 0) { calMonth = 11; calYear--; }
  renderTasks();
}

async function load(){
  const wrap = document.getElementById("tasks-wrap");

  // Detect Notion
  try {
    const ns = await fetch(`${API}/api/notion/status`, {signal:AbortSignal.timeout(5000)});
    const nd = await ns.json();
    useNotion = nd.configured;
  } catch(e){}

  if(!useNotion){
    wrap.innerHTML='<div class="empty">⚙️ Notion is not configured. Add NOTION_TOKEN and DB IDs to Railway env vars.</div>';
    return;
  }

  // Fetch tasks assigned to this user
  const assignedName = USER_NAME || "";
  if(!assignedName){
    wrap.innerHTML='<div class="empty">⚠️ Could not determine your name. Please log in again.</div>';
    return;
  }

  try {
    const r = await fetch(`${API}/api/notion/tasks?assigned_to=${encodeURIComponent(assignedName)}`, {signal:AbortSignal.timeout(15000)});
    const d = await r.json();
    window.allTasks = (d.tasks||[]).map(t=>({...t, status: normStatus(t.status)}));
  } catch(e){
    wrap.innerHTML=`<div class="empty">⚠️ Could not load tasks: ${e.message}</div>`;
    return;
  }

  renderTasks();
}

function renderTasks() {
  const wrap = document.getElementById("tasks-wrap");
  const today = new Date().toISOString().slice(0,10);

  const query = document.getElementById("task-search-input")?.value.toLowerCase() || "";
  let filteredTasks = window.allTasks;
  if(query) {
    filteredTasks = window.allTasks.filter(t => 
      (t.title||"").toLowerCase().includes(query) || 
      (t.client||"").toLowerCase().includes(query)
    );
  }

  renderDashboardWidgets(filteredTasks);

  if(!filteredTasks.length){
    wrap.innerHTML='<div class="empty">🎉 No tasks found matching your search.</div>';
    return;
  }

  let html="";
  
  if (currentViewMode === 'status') {
    const active  = filteredTasks.filter(t=>t.status==="in_progress");
    const pending = filteredTasks.filter(t=>t.status==="not_started");
    const review  = filteredTasks.filter(t=>t.status==="submitted");
    const done    = filteredTasks.filter(t=>t.status==="approved");

    if(active.length)  html+=renderSection('sec-active', `🔥 Active — ${active.length} task${active.length>1?"s":""}`, active, today);
    if(pending.length) html+=renderSection('sec-pending', `📋 Up Next — ${pending.length}`, pending, today);
    if(review.length)  html+=renderSection('sec-review', `⏳ In Review — ${review.length}`, review, today);
    if(done.length)    html+=renderSection('sec-done', `✅ Done — ${done.length}`, done, today);
    
  } else if (currentViewMode === 'client') {
    const byClient = {};
    filteredTasks.forEach(t => {
       const c = t.client_name || t.client || "Internal / Quick Tasks";
       if(!byClient[c]) byClient[c] = [];
       byClient[c].push(t);
    });
    Object.keys(byClient).sort().forEach((c, i) => {
       html += renderSection(`sec-client-${i}`, `📁 ${c} — ${byClient[c].length} task${byClient[c].length>1?"s":""}`, byClient[c], today);
    });
    
  } else if (currentViewMode === 'date') {
    const overdue = [], todayTasks = [], upcoming = [], nodate = [];
    filteredTasks.forEach(t => {
       if(!t.due_date) nodate.push(t);
       else if(t.due_date < today) overdue.push(t);
       else if(t.due_date === today) todayTasks.push(t);
       else upcoming.push(t);
    });
    // Sort upcoming chronologically
    upcoming.sort((a,b) => a.due_date.localeCompare(b.due_date));
    
    if(overdue.length)    html+=renderSection('sec-overdue', `<span style="color:var(--red)">🔴 Overdue — ${overdue.length}</span>`, overdue, today);
    if(todayTasks.length) html+=renderSection('sec-today', `<span style="color:var(--acc)">⚡ Due Today — ${todayTasks.length}</span>`, todayTasks, today);
    if(upcoming.length)   html+=renderSection('sec-upcoming', `📅 Upcoming — ${upcoming.length}`, upcoming, today);
    if(nodate.length)     html+=renderSection('sec-nodate', `⚪ No Due Date — ${nodate.length}`, nodate, today);
    
  } else if (currentViewMode === 'calendar') {
    html = renderCalendarHtml(today, filteredTasks);
  }

  wrap.innerHTML=html;
  attachSubmitHandlers();
}

function renderCalendarHtml(todayString, filteredTasks = window.allTasks) {
  const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  
  let html = `
    <div class="cal-header">
      <button class="cal-nav-btn" onclick="changeCalMonth(-1)">◀ Prev</button>
      <div class="cal-title">${monthNames[calMonth]} ${calYear}</div>
      <button class="cal-nav-btn" onclick="changeCalMonth(1)">Next ▶</button>
    </div>
    <div class="cal-grid">
      <div class="cal-day-hdr">Mon</div><div class="cal-day-hdr">Tue</div><div class="cal-day-hdr">Wed</div>
      <div class="cal-day-hdr">Thu</div><div class="cal-day-hdr">Fri</div><div class="cal-day-hdr">Sat</div><div class="cal-day-hdr">Sun</div>
  `;

  const tasksByDate = {};
  filteredTasks.forEach(t => {
    if(t.due_date) {
      if(!tasksByDate[t.due_date]) tasksByDate[t.due_date] = [];
      tasksByDate[t.due_date].push(t);
    }
  });

  const firstDay = new Date(calYear, calMonth, 1);
  let startDay = (firstDay.getDay() + 6) % 7; 
  const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();

  for(let i=0; i<startDay; i++) {
    html += `<div class="cal-cell empty-cell"></div>`;
  }

  for(let d=1; d<=daysInMonth; d++) {
    const dStr = `${calYear}-${String(calMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const isToday = dStr === todayString;
    const dayTasks = tasksByDate[dStr] || [];
    
    let taskHtml = "";
    let popoverHtml = "";
    
    if(dayTasks.length) {
      popoverHtml += `<div class="cal-popover">`;
      dayTasks.forEach(t => {
        let stClass = "st-pending";
        if(t.status==="in_progress") stClass="st-active";
        if(t.status==="submitted") stClass="st-review";
        if(t.status==="approved") stClass="st-done";
        
        taskHtml += `<div class="cal-pill ${stClass}">${t.title||'Untitled'}</div>`;
        
        popoverHtml += `
          <div class="cal-pop-item">
            <div class="cal-pop-title">${t.title||'Untitled'}</div>
            <div class="cal-pop-meta">${t.client_name || t.client || 'Quick Task'} • ${spLabel(t.status)}</div>
            ${t.status === "not_started" ? `<button class="cal-pop-btn" onclick="startTask('${t.notion_id}')">▶ Start Task</button>` : ''}
          </div>
        `;
      });
      popoverHtml += `</div>`;
    }

    const cellIndex = startDay + d - 1;
    const popUpClass = cellIndex >= 28 ? 'pop-up' : '';

    html += `
      <div class="cal-cell ${isToday ? 'today' : ''} ${popUpClass}">
        <span class="cal-date-num">${d}</span>
        <div class="cal-tasks">${taskHtml}</div>
        ${popoverHtml}
      </div>
    `;
  }

  html += `</div>`;
  return html;
}

function attachSubmitHandlers() {
  const wrap = document.getElementById("tasks-wrap");
  wrap.querySelectorAll("form.sf").forEach(form=>{
    form.addEventListener("submit", async e=>{
      e.preventDefault();
      const nid   = form.dataset.nid;
      const note  = form.querySelector("textarea").value.trim();
      const fileEl= form.querySelector(".file-input");
      const file  = fileEl ? fileEl.value.trim() : "";
      if(!note){toast("Add a note first","err");return;}
      const btn=form.querySelector("button[type=submit]");
      btn.disabled=true; btn.textContent="Submitting...";
      try{
        const r = await fetch(`${API}/api/notion/tasks/${nid}`,{
          method:"PATCH",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({status:"submitted",progress:75,submission_note:note+(file?`\nProof: ${file}`:"")}),
          signal:AbortSignal.timeout(10000),
        });
        const d = await r.json();
        if(d.success){
          confetti({ particleCount: 150, spread: 80, origin: { y: 0.6 } });
          toast("✅ Submitted for review — 75% progress");
          load();
        }
        else{toast(d.error||"Error","err");btn.disabled=false;btn.textContent="Submit Work →";}
      }catch(err){toast("Error: "+err.message,"err");btn.disabled=false;btn.textContent="Submit Work →";}
    });
  });
}

function renderTask(t, today){
  const pct     = t.progress || progressPct(t.status);
  
  // Professional Urgency Calculation
  let isOverdue = false;
  let isDueToday = false;
  let isDueTomorrow = false;
  
  if (t.due_date && t.status !== "approved") {
    const todayStr = today;
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    const tomorrowStr = tomorrow.toISOString().slice(0,10);
    
    if (t.due_date < todayStr) isOverdue = true;
    else if (t.due_date === todayStr) isDueToday = true;
    else if (t.due_date === tomorrowStr) isDueTomorrow = true;
  }

  let body="";

  // Load subtasks
  let subtasks = [];
  try { subtasks = JSON.parse(localStorage.getItem(`my_subtasks_${t.notion_id}`) || "[]"); } catch(e){}
  
  const safeTitleForAttr = (t.title||'').replace(/'/g,"&apos;").replace(/`/g,'&#96;');
  const safeClientForAttr = (t.client_name||t.client||'').replace(/'/g,"&apos;");

  let subtasksHtml = "";
  if(t.status !== "approved") {
    subtasksHtml = `<div class="subtasks-container" id="st-wrap-${t.notion_id}">`;
    subtasks.forEach((st, idx) => {
      subtasksHtml += `
        <div class="subtask-item ${st.done ? 'done' : ''}">
          <input type="checkbox" class="subtask-cb" ${st.done ? 'checked' : ''} onchange="toggleMySubtask('${t.notion_id}', ${idx}, this.checked)">
          <span>${st.text}</span>
        </div>
      `;
    });
    subtasksHtml += `
      <div style="display:flex;gap:6px;margin-top:6px;">
        <button class="add-subtask-btn" style="flex:1" onclick="addMySubtask('${t.notion_id}')">+ Add Sub-task</button>
        <button class="ai-breakdown-btn" style="width:auto;padding:7px 14px;" id="ai-btn-${t.notion_id}" onclick="aiBreakdownTask('${t.notion_id}','${safeTitleForAttr}','${safeClientForAttr}')">🧠 AI Breakdown</button>
      </div>
    </div>`;
    subtasksHtml = subtasksHtml; // close container added in loop body
  }

  // Approved state
  if(t.status==="approved") body+=`<div class="done-box">Approved — great work!</div>`;
  // In review
  if(t.status==="submitted") body+=`<div class="done-box" style="color:var(--acc);border-color:rgba(245,166,35,.3);background:rgba(245,166,35,.07)">Submitted — waiting for review.</div>`;
  // Submission note (if any)
  if(t.submission_note && t.status==="submitted") body+=`<div class="rej-box" style="border-color:rgba(245,166,35,.3)"><div class="rej-title" style="color:var(--acc)">Your Note</div><p>${t.submission_note}</p></div>`;

  // Action buttons
  if(t.status !== "approved"){
    body+=`<div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:12px;">`;
    
    if(t.status==="not_started"){
      body+=`<button class="btn btn-quick" onclick="startTask('${t.notion_id}')">▶ Start Task</button>`;
    }
    
    body+=`<button class="btn btn-quick" onclick="pushToStandup('${t.notion_id}', '${t.title?.replace(/'/g, "\\'") || "Untitled"}')">➕ Add to Standup</button>`;
    body+=`<button class="btn btn-quick" onclick="deferTask('${t.notion_id}', '${t.due_date||today}')">⏰ Defer</button>`;
    
    body+=`</div>`;
  }
  
  body += subtasksHtml;

  // Submit form for in_progress
  if(t.status==="in_progress"){
    body+=`<form class="submit-form sf" data-nid="${t.notion_id}" data-title="${t.title||''}" data-client="${t.client_name||t.client||''}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <label style="margin-bottom:0">Progress Update</label>
        <button type="button" class="ai-breakdown-btn" style="width:auto;padding:5px 12px;font-size:0.75rem;" onclick="aiDraftProofOfWork(this.closest('form'),'${safeTitleForAttr}','${safeClientForAttr}')">🧠 AI Draft</button>
      </div>
      <textarea id="ta-${t.notion_id}" placeholder="What did you do on this task?" required></textarea>
      <label>Proof of Work — link or file URL <span style="color:var(--muted)">(optional)</span></label>
      <input type="url" class="file-input" placeholder="https://drive.google.com/... or figma.com/..." />
      <div class="form-row">
        <button type="submit" class="btn btn-acc">Submit Work</button>
        <button type="button" class="btn btn-quick" onclick="quickDone('${t.notion_id}')">⚡ Quick Mark Done</button>
      </div>
    </form>`;
  }

  let tagHtml = "";
  if(t.client) tagHtml += `<span class="tag">Client: ${t.client}</span>`;
  
  if (t.due_date) {
    if (isOverdue) tagHtml += `<span class="tag overdue-tag" style="border-color:var(--red);color:var(--red);font-weight:700">OVERDUE: ${t.due_date}</span>`;
    else if (isDueToday) tagHtml += `<span class="tag" style="border-color:var(--red);color:var(--red);font-weight:700">DUE TODAY: ${t.due_date}</span>`;
    else if (isDueTomorrow) tagHtml += `<span class="tag" style="border-color:var(--acc);color:var(--acc);font-weight:700">DUE TOMORROW: ${t.due_date}</span>`;
    else tagHtml += `<span class="tag">Due: ${t.due_date}</span>`;
  }

  // Drag and drop attributes
  const dragAttrs = currentViewMode === 'status' ? `draggable="true" ondragstart="dragStart(event)" ondragover="dragOver(event)" ondrop="dropTask(event)"` : "";

  return`<div class="tc ${(isOverdue||isDueToday)?"overdue":""}" id="tc-${t.notion_id}" data-nid="${t.notion_id}" ${dragAttrs}>
    <div class="tc-top">
      <div>
        <div class="tc-title" style="display:flex; align-items:center; gap:8px">
          ${t.title||"Untitled"}
          <button onclick="editTask('${t.notion_id}', '${t.title?.replace(/'/g, "\\'") || ""}', '${t.due_date || ""}')" style="background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:0.8rem;" title="Edit Task">✏️</button>
        </div>
        <div class="tc-meta">${tagHtml}</div>
      </div>
      <span class="sp sp-${t.status}">${spLabel(t.status)}</span>
    </div>
    <div class="prog"><div class="prog-fill" style="width:${pct}%"></div></div>
    <div class="prog-label">${pct}% complete</div>
    ${body}
  </div>`;
}

async function startTask(nid){
  try{
    const r = await fetch(`${API}/api/notion/tasks/${nid}`,{
      method:"PATCH",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({status:"in_progress",progress:10}),
      signal:AbortSignal.timeout(10000),
    });
    const d = await r.json();
    if(d.success){toast("Task started! 🚀");load();}
    else toast(d.error||"Error","err");
  }catch(err){toast("Error: "+err.message,"err");}
}

async function quickDone(nid){
  try{
    const r = await fetch(`${API}/api/notion/tasks/${nid}`,{
      method:"PATCH",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({status:"submitted",progress:75}),
      signal:AbortSignal.timeout(10000),
    });
    const d = await r.json();
    if(d.success){
      confetti({ particleCount: 150, spread: 80, origin: { y: 0.6 } });
      toast("Marked for review!");
      load();
    }
    else toast(d.error||"Error","err");
  }catch(err){toast("Error: "+err.message,"err");}
}

async function editTask(nid, currentTitle, currentDate) {
  const newTitle = prompt("Edit Task Title:", currentTitle);
  if (newTitle === null) return; // user cancelled
  
  const newDate = prompt("Edit Due Date (YYYY-MM-DD) or leave empty:", currentDate || "");
  if (newDate === null) return; // user cancelled

  try {
    const payload = {};
    if (newTitle.trim() !== currentTitle) payload.new_title = newTitle.trim();
    if (newDate.trim() !== (currentDate || "")) payload.due_date = newDate.trim() || null;
    
    if (Object.keys(payload).length === 0) return; // nothing changed

    const r = await fetch(`${API}/api/notion/tasks/${nid}`, {
      method: "PATCH",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (d.success) {
      toast("✅ Task updated successfully!");
      load();
    } else {
      toast(d.error || "Failed to update task", "err");
    }
  } catch(err) {
    toast("Error: " + err.message, "err");
  }
}

async function pushToStandup(notion_id, title) {
  try {
    const r = await fetch(`${API}/api/standup/my-tasks`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ user_id: UID, title: title, notion_id: notion_id })
    });
    const d = await r.json();
    if (d.success) {
      toast("✅ Added to today's Standup list!");
    } else {
      toast(d.error || "Error adding to standup", "err");
    }
  } catch(err) {
    toast("Error: " + err.message, "err");
  }
}

// --- Collapsible Sections Logic ---
function toggleSection(id) {
  const content = document.getElementById(id);
  const isCollapsed = content.classList.contains('collapsed');
  const icon = document.getElementById(`icon-${id}`);
  
  if (isCollapsed) {
    content.classList.remove('collapsed');
    icon.classList.remove('collapsed-icon');
    localStorage.setItem(id, 'open');
  } else {
    content.classList.add('collapsed');
    icon.classList.add('collapsed-icon');
    localStorage.setItem(id, 'collapsed');
  }
}

function renderSection(id, titleHtml, tasksArray, today) {
  const isCollapsed = localStorage.getItem(id) === 'collapsed';
  const iconClass = isCollapsed ? 'collapse-icon collapsed-icon' : 'collapse-icon';
  const contentClass = isCollapsed ? 'sec-content collapsed' : 'sec-content';
  
  let html = `<div class="sec-lbl" onclick="toggleSection('${id}')">
                <div>${titleHtml}</div>
                <span id="icon-${id}" class="${iconClass}">▼</span>
              </div>`;
  html += `<div id="${id}" class="${contentClass}">` + tasksArray.map(t => renderTask(t, today)).join("") + `</div>`;
  return html;
}

// --- Dashboard Widgets Logic ---
function renderDashboardWidgets(tasks) {
  const dashContainer = document.getElementById("dashboard-widgets");
  if (!dashContainer || currentViewMode !== 'status') {
    if(dashContainer) dashContainer.innerHTML = "";
    return;
  }

  const todayStr = new Date().toISOString().slice(0,10);
  
  // Progress Ring Calculation
  let activeWeight = 0;
  let totalWeight = 0;
  
  let overdueCount = 0;
  const overdueIds = [];

  const clientNotStarted = {};

  tasks.forEach(t => {
    // Determine if task counts towards today's workload
    if (t.status !== "not_started" || t.due_date === todayStr || (t.due_date && t.due_date < todayStr)) {
      totalWeight += 100;
      activeWeight += progressPct(t.status);
    }
    
    // Check overdue
    if (t.status !== "approved" && t.status !== "submitted" && t.due_date && t.due_date < todayStr) {
      overdueCount++;
      overdueIds.push(t.notion_id);
    }
    
    // Check sprint suggestions
    if (t.status === "not_started") {
      const c = t.client_name || t.client || "Internal";
      if(!clientNotStarted[c]) clientNotStarted[c] = [];
      clientNotStarted[c].push(t.notion_id);
    }
  });

  const pct = totalWeight > 0 ? Math.round((activeWeight / totalWeight) * 100) : 0;
  const circumference = 2 * Math.PI * 28; // r=28
  const offset = circumference - (pct / 100) * circumference;

  let html = `
    <div class="dash-card">
      <div class="ring-container">
        <svg class="ring-svg" viewBox="0 0 64 64">
          <circle class="ring-bg" cx="32" cy="32" r="28"></circle>
          <circle class="ring-fill" cx="32" cy="32" r="28" stroke-dasharray="${circumference}" stroke-dashoffset="${offset}"></circle>
        </svg>
        <div class="ring-text">${pct}%</div>
      </div>
      <div class="dash-info">
        <h3>Daily Progress</h3>
        <p>You are ${pct}% done with your active workload today.</p>
      </div>
    </div>
  `;

  // Smart Suggestions
  if (overdueCount > 2) {
    html += `
      <div class="dash-card smart-suggestion">
        <div class="dash-info" style="flex:1">
          <h3>⚠️ Bulk Defer Overdue Tasks</h3>
          <p>You have ${overdueCount} overdue tasks causing clutter. Start with a clean slate tomorrow?</p>
        </div>
        <button class="btn btn-quick" onclick="bulkDefer('${overdueIds.join(',')}')">Defer All</button>
      </div>
    `;
  } else {
    // Check for sprint grouping
    let sprintClient = null;
    let sprintIds = [];
    for (let c in clientNotStarted) {
      if (clientNotStarted[c].length >= 3) {
        sprintClient = c;
        sprintIds = clientNotStarted[c];
        break;
      }
    }
    if (sprintClient) {
      html += `
        <div class="dash-card smart-suggestion">
          <div class="dash-info" style="flex:1">
            <h3>💡 Smart Sprint: ${sprintClient}</h3>
            <p>You have ${sprintIds.length} pending tasks for this client. Group them into a sprint and start now?</p>
          </div>
          <button class="btn btn-acc" onclick="bulkStart('${sprintIds.join(',')}')">Start Sprint</button>
        </div>
      `;
    }
  }

  dashContainer.innerHTML = html;
}

async function bulkDefer(idsStr) {
  const ids = idsStr.split(',');
  if(!ids.length) return;
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  const nextDay = tomorrow.toISOString().slice(0,10);
  
  toast("Deferring tasks...");
  try {
    await Promise.all(ids.map(nid => 
      fetch(`${API}/api/notion/tasks/${nid}`, {
        method: "PATCH",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({due_date: nextDay})
      })
    ));
    toast("All overdue tasks deferred!");
    load();
  } catch(e) {
    toast("Error deferring tasks", "err");
  }
}

async function bulkStart(idsStr) {
  const ids = idsStr.split(',');
  if(!ids.length) return;
  toast("Starting sprint...");
  try {
    await Promise.all(ids.map(nid => 
      fetch(`${API}/api/notion/tasks/${nid}`, {
        method: "PATCH",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({status:"in_progress", progress:10})
      })
    ));
    toast("Sprint started! Let's go!");
    load();
  } catch(e) {
    toast("Error starting sprint", "err");
  }
}

// --- Sub-Tasks Logic ---
async function addMySubtask(nid) {
  const text = prompt("Enter sub-task:");
  if (!text) return;
  
  let subtasks = [];
  try { subtasks = JSON.parse(localStorage.getItem(`my_subtasks_${nid}`) || "[]"); } catch(e){}
  
  subtasks.push({text, done: false});
  localStorage.setItem(`my_subtasks_${nid}`, JSON.stringify(subtasks));
  
  recalcSubtasks(nid, subtasks);
  renderTasks();
}

async function toggleMySubtask(nid, idx, isChecked) {
  let subtasks = [];
  try { subtasks = JSON.parse(localStorage.getItem(`my_subtasks_${nid}`) || "[]"); } catch(e){}
  if (subtasks[idx]) {
    subtasks[idx].done = isChecked;
    localStorage.setItem(`my_subtasks_${nid}`, JSON.stringify(subtasks));
    await recalcSubtasks(nid, subtasks);
    renderTasks();
  }
}

async function recalcSubtasks(nid, subtasks) {
  if (!subtasks.length) return;
  const doneCount = subtasks.filter(st => st.done).length;
  // If task status is not_started, progress is 0. If there are subtasks, progress is mapped between 10% and 90%.
  // For simplicity, we just set progress directly.
  const newProgress = Math.round((doneCount / subtasks.length) * 100);
  
  try {
    const payload = { progress: newProgress };
    if (newProgress > 0) payload.status = "in_progress"; // Auto-start if progressing
    await fetch(`${API}/api/notion/tasks/${nid}`, {
      method: "PATCH",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    // Update local cache to reflect immediately
    const t = window.allTasks.find(x => x.notion_id === nid);
    if(t) {
      t.progress = newProgress;
      if(newProgress > 0) t.status = "in_progress";
    }
  } catch(e) {
    console.error("Failed to sync subtask progress to Notion", e);
  }
}

// --- Premium Features Logic ---

async function quickCreateTask() {
  const inp = document.getElementById("quick-task-input");
  const rawText = inp.value.trim();
  if(!rawText) return;
  inp.disabled = true;
  
  const placeholder = inp.placeholder;
  inp.placeholder = "🧠 Claude is parsing...";
  
  try {
    // Feature 3: NL Task Parsing — send to Claude first
    let title = rawText, client_name = "", due_date = new Date().toISOString().slice(0,10);
    
    try {
      const parseR = await fetch(`${API}/api/ai/parse-task`, {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({text: rawText, assigned_name: USER_NAME || ""})
      });
      const parseD = await parseR.json();
      if (parseD.title) {
        title = parseD.title;
        client_name = parseD.client_name || "";
        due_date = parseD.due_date || due_date;
      }
    } catch(e) { /* fallback to raw text */ }
    
    const r = await fetch(`${API}/api/notion/tasks`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({title, assigned_to: USER_NAME || "", due_date})
    });
    const d = await r.json();
    if(d.success) {
      toast(`Task created: "${title}"${due_date ? ` | Due ${due_date}` : ""}`);
      inp.value = "";
      load();
    } else {
      toast(d.error || "Failed to create", "err");
    }
  } catch(e) {
    toast("Error: " + e.message, "err");
  } finally {
    inp.disabled = false;
    inp.placeholder = placeholder;
  }
}

async function deferTask(nid, currentDateStr) {
  let date = new Date(currentDateStr);
  if (isNaN(date.getTime())) date = new Date(); // fallback to today
  date.setDate(date.getDate() + 1); // bump by 1 day
  const nextDay = date.toISOString().slice(0,10);
  
  try {
    const r = await fetch(`${API}/api/notion/tasks/${nid}`, {
      method: "PATCH",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({due_date: nextDay})
    });
    const d = await r.json();
    if (d.success) {
      toast("Task deferred to " + nextDay);
      load();
    } else {
      toast("Failed to defer", "err");
    }
  } catch(err) {
    toast("Error: " + err.message, "err");
  }
}

// --- Drag and Drop Logic ---
let draggedElement = null;

function dragStart(e) {
  draggedElement = e.currentTarget;
  e.dataTransfer.effectAllowed = "move";
  // Add slight transparency to indicate dragging
  setTimeout(() => draggedElement.style.opacity = '0.5', 0);
}

function dragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
}

function dropTask(e) {
  e.preventDefault();
  e.stopPropagation();
  if (!draggedElement) return;
  draggedElement.style.opacity = '1';
  
  const target = e.currentTarget;
  if (target !== draggedElement && target.classList.contains('tc')) {
    const parent = target.parentNode;
    // Determine if we drop before or after the target
    const rect = target.getBoundingClientRect();
    const next = (e.clientY - rect.top) / (rect.bottom - rect.top) > 0.5;
    parent.insertBefore(draggedElement, next ? target.nextSibling : target);
    
    saveTaskOrder();
  }
}

function saveTaskOrder() {
  // Store the visual order in localStorage
  const orderedIds = [];
  document.querySelectorAll('.tc').forEach(el => {
    if(el.dataset.nid) orderedIds.push(el.dataset.nid);
  });
  localStorage.setItem(`task_order_${UID}`, JSON.stringify(orderedIds));
  toast("Priority order saved.");
}

function applyTaskOrder() {
  if (currentViewMode !== 'status') return;
  const savedOrder = JSON.parse(localStorage.getItem(`task_order_${UID}`) || "[]");
  if (!savedOrder.length) return;
  
  // Sort window.allTasks globally based on the saved visual order
  // Elements not in the saved list get pushed to the bottom
  window.allTasks.sort((a, b) => {
    let idxA = savedOrder.indexOf(a.notion_id);
    let idxB = savedOrder.indexOf(b.notion_id);
    if(idxA === -1) idxA = 999999;
    if(idxB === -1) idxB = 999999;
    return idxA - idxB;
  });
}

// Hook applyTaskOrder right after fetching tasks
const originalLoad = load;
window.load = async function() {
  await originalLoad();
  if(window.allTasks && window.allTasks.length > 0) {
    applyTaskOrder();
    renderTasks(); // Re-render with sorted order
  }
};

load();

// ═══════════════════════════════════════════════
// CLAUDE AI FEATURES
// ═══════════════════════════════════════════════

// Feature 1: AI Auto-Breakdown for Sub-Tasks
async function aiBreakdownTask(nid, taskTitle, clientName) {
  const btn = document.getElementById(`ai-btn-${nid}`);
  if(btn) { btn.disabled = true; btn.textContent = '🧠 Thinking...'; }
  
  try {
    const r = await fetch(`${API}/api/ai/breakdown`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({task_title: taskTitle, client_name: clientName})
    });
    const d = await r.json();
    if(d.subtasks && d.subtasks.length) {
      let existing = [];
      try { existing = JSON.parse(localStorage.getItem(`my_subtasks_${nid}`) || "[]"); } catch(e){}
      
      const newSubtasks = d.subtasks.map(text => ({text, done: false}));
      const merged = [...existing, ...newSubtasks];
      localStorage.setItem(`my_subtasks_${nid}`, JSON.stringify(merged));
      renderTasks();
      toast(`🧠 Added ${d.subtasks.length} AI-generated sub-tasks!`);
    } else {
      toast(d.error || 'No sub-tasks generated', 'err');
    }
  } catch(e) {
    toast('AI breakdown failed: ' + e.message, 'err');
  } finally {
    if(btn) { btn.disabled = false; btn.textContent = '🧠 AI Breakdown'; }
  }
}

// Feature 2: AI Auto-Draft Proof of Work
async function aiDraftProofOfWork(form, taskTitle, clientName) {
  const nid = form.dataset.nid;
  const ta = form.querySelector('textarea');
  if(!ta) return;
  
  const origPlaceholder = ta.placeholder;
  ta.placeholder = '🧠 Claude is drafting your submission note...';
  ta.disabled = true;

  let subtasks = [];
  try { subtasks = JSON.parse(localStorage.getItem(`my_subtasks_${nid}`) || '[]'); } catch(e){}

  try {
    const r = await fetch(`${API}/api/ai/proof-of-work`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({task_title: taskTitle, client_name: clientName, subtasks})
    });
    const d = await r.json();
    if(d.draft) {
      ta.value = d.draft;
      toast('🧠 AI drafted your submission note. Review and edit as needed!');
    } else {
      toast(d.error || 'Could not draft note', 'err');
    }
  } catch(e) {
    toast('AI draft failed: ' + e.message, 'err');
  } finally {
    ta.disabled = false;
    ta.placeholder = origPlaceholder;
  }
}

// Feature 4: AI Coach Widget
function toggleCoach() {
  const panel = document.getElementById('ai-coach-panel');
  panel.classList.toggle('open');
  if(panel.classList.contains('open')) {
    document.getElementById('coach-input').focus();
  }
}

async function sendCoachMessage() {
  const input = document.getElementById('coach-input');
  const sendBtn = document.getElementById('coach-send');
  const messages = document.getElementById('coach-messages');
  const question = input.value.trim();
  if(!question) return;

  // Add user message
  messages.innerHTML += `<div class="coach-msg user">${question}</div>`;
  input.value = '';
  input.disabled = true;
  sendBtn.disabled = true;

  // Thinking indicator
  const thinkId = 'think-' + Date.now();
  messages.innerHTML += `<div class="coach-msg ai thinking" id="${thinkId}">Claude is thinking...</div>`;
  messages.scrollTop = messages.scrollHeight;

  try {
    const r = await fetch(`${API}/api/ai/coach`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({question, assigned_name: USER_NAME || '', user_id: UID})
    });
    const d = await r.json();
    const thinking = document.getElementById(thinkId);
    if(thinking) thinking.remove();

    if(d.reply) {
      messages.innerHTML += `<div class="coach-msg ai">${d.reply}</div>`;
    } else {
      messages.innerHTML += `<div class="coach-msg ai" style="color:var(--red)">Sorry, I couldn't fetch a response. Try again.</div>`;
    }
  } catch(e) {
    const thinking = document.getElementById(thinkId);
    if(thinking) thinking.remove();
    messages.innerHTML += `<div class="coach-msg ai" style="color:var(--red)">Connection error. Check that the backend is running.</div>`;
  } finally {
    input.disabled = false;
    sendBtn.disabled = false;
    input.focus();
    messages.scrollTop = messages.scrollHeight;
  }
}
