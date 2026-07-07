with open('frontend/projects.html', 'r', encoding='utf-8') as f:
    content = f.read()

old_logic = """if (taskObj.description === notes &&
        taskObj.due_date === due_date &&
        taskObj.title === newTitle &&
        taskObj.assigned_to === assigned_to &&
        taskObj.status === status) return;

    taskObj.description = notes;
    taskObj.due_date = due_date;
    taskObj.title = newTitle;
    taskObj.assigned_to = assigned_to;
    taskObj.status = status;

    cellElem.style.opacity = "0.5";

    try {
      const endpoint = isNotionClient 
        ? `${API}/api/notion/tasks/${taskId}` 
        : `${API}/api/sqlite/tasks/${taskId}`;

      const res = await fetch(endpoint, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          submission_note: notes,
          due_date: due_date,
          new_title: newTitle,
          assigned_to: assigned_to,
          status: status
        })
      });"""

new_logic = """const changedPayload = {};
    if (taskObj.description !== notes) changedPayload.submission_note = notes;
    if (taskObj.due_date !== due_date) changedPayload.due_date = due_date;
    if (taskObj.title !== newTitle) changedPayload.new_title = newTitle;
    if (taskObj.assigned_to !== assigned_to) changedPayload.assigned_to = assigned_to;
    if (taskObj.status !== status) changedPayload.status = status;

    if (Object.keys(changedPayload).length === 0) return;

    taskObj.description = notes;
    taskObj.due_date = due_date;
    taskObj.title = newTitle;
    taskObj.assigned_to = assigned_to;
    taskObj.status = status;

    cellElem.style.opacity = "0.5";

    try {
      const endpoint = isNotionClient 
        ? `${API}/api/notion/tasks/${taskId}` 
        : `${API}/api/sqlite/tasks/${taskId}`;

      const res = await fetch(endpoint, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(changedPayload)
      });"""

content = content.replace(old_logic, new_logic)

with open('frontend/projects.html', 'w', encoding='utf-8') as f:
    f.write(content)
