import re

with open("frontend/dashboard.html", "r", encoding="utf-8") as f:
    html = f.read()

# 1. Update the title and header
html = html.replace('<title>Cost Dashboard | Agency Portal Assistant</title>', '<title>Agency Dashboard | Agency Portal Assistant</title>')
html = html.replace('<h1>Cost <span>Dashboard</span></h1>', '<h1>Agency <span>Dashboard</span></h1>')
html = html.replace('Real-time System API spend monitoring', 'Overview of agency activities and attendance')
html = html.replace('Agency Portal Assistant · Cost Dashboard', 'Agency Portal Assistant · Agency Dashboard')

# 2. Add API Costing button to Quick Actions
# Find Quick Actions panel
qa_target = '<span>⌨</span> Open Chat Assistant\n        </a>'
qa_replacement = qa_target + '\n        <a href="api-costing.html" style="display:flex;align-items:center;gap:8px;padding:12px;border-radius:8px;background:var(--surface2);border:1px solid var(--border);color:var(--text);text-decoration:none;font-size:.875rem;transition:all .2s" onmouseover="this.style.borderColor=\'var(--accent)\'" onmouseout="this.style.borderColor=\'var(--border)\'">\n          <span>💰</span> API Costing\n        </a>'
html = html.replace(qa_target, qa_replacement)

# 3. Remove Cost KPI Cards, Usage Bar, Model Usage, Task breakdown, Top Users, Recent Calls
# Everything from <!-- KPI Cards --> up to <!-- Admin Attendance Panel -->
start_idx = html.find('<!-- KPI Cards -->')
end_idx = html.find('<!-- Admin Attendance Panel -->')

if start_idx != -1 and end_idx != -1:
    html = html[:start_idx] + html[end_idx:]

# 4. Remove loadDashboard function logic since the dashboard no longer needs /api/usage
# Wait, actually loadDashboard still runs and fetches `/api/usage`, but there's no `kpi-spent` etc to update.
# It does call `loadAttendance()`. We should probably keep `loadAttendance()` but remove `renderDashboard(data)` from `dashboard.html`!
# Let's just remove the `renderDashboard` function entirely and change `loadDashboard` to only `loadAttendance`.
render_start = html.find('function renderDashboard(d) {')
if render_start != -1:
    render_end = html.find('async function loadAttendance() {')
    html = html[:render_start] + html[render_end:]

# Replace loadDashboard with a simpler one
html = re.sub(r'async function loadDashboard\(\) \{.*?\n\}\n\nasync function loadAttendance\(\)', 'async function loadAttendance()', html, flags=re.DOTALL)

# Since we removed loadDashboard, we need to change `setInterval(loadDashboard, 60000);` to `setInterval(loadAttendance, 60000);`
html = html.replace('loadDashboard();\nsetInterval(loadDashboard, 60000);', 'loadAttendance();\nsetInterval(loadAttendance, 60000);')
html = html.replace('onclick="loadDashboard()"', 'onclick="loadAttendance()"')

# 5. We also need to remove changeMonth and toggleCurrency functions since they're for Cost
html = re.sub(r'let currencyMode = \'USD\';\nlet currentMonthFilter = null;\n\nfunction changeMonth.*?function formatCost.*?\n\}\n\nfunction formatUser.*?\n\}\n', '', html, flags=re.DOTALL)
html = re.sub(r'const TASK_ICONS = \{.*?\};\n', '', html, flags=re.DOTALL)
html = html.replace('let currentDashboardData = null;\n', '')

# Remove month-select and currency-select from header
html = re.sub(r'<select class="currency-toggle".*?</select>', '', html, flags=re.DOTALL)

with open("frontend/dashboard.html", "w", encoding="utf-8") as f:
    f.write(html)
