import re

with open('frontend/projects.html', 'r', encoding='utf-8') as f:
    content = f.read()

old_pattern = r'if \(taskObj\.description === notes &&[\s\S]*?body: JSON\.stringify\(\{[\s\S]*?status: status\n\s*\}\)\n\s*\}\);'

new_code = """const changedPayload = {};
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

content = re.sub(old_pattern, new_code, content)

with open('frontend/projects.html', 'w', encoding='utf-8') as f:
    f.write(content)
