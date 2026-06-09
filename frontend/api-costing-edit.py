import re

with open("frontend/api-costing.html", "r", encoding="utf-8") as f:
    html = f.read()

# Split out the bottom panels
parts = html.split('<!-- Admin Attendance Panel -->')
if len(parts) > 1:
    footer_part = parts[1].split('<footer class="dash-footer">')
    if len(footer_part) > 1:
        html = parts[0] + '<footer class="dash-footer">' + footer_part[1]

# Remove the unused scripts
html = re.sub(r'async function loadAttendance\(\) \{.*?\n\}\n', '', html, flags=re.DOTALL)
html = re.sub(r'async function generateDailyReport\(\) \{.*?\n\}\n', '', html, flags=re.DOTALL)
html = html.replace('loadAttendance();', '')

# Remove copyClientLink and admin check
html = re.sub(r'// ── Show "Onboard Client", Backup DB & Copy Client Link only for admins ──.*?\n\}\n', '', html, flags=re.DOTALL)
html = re.sub(r'function copyClientLink\(\) \{.*?\n\}\n', '', html, flags=re.DOTALL)

# Update title and headers
html = html.replace('<title>Cost Dashboard | Agency Portal Assistant</title>', '<title>API Costing | Agency Portal Assistant</title>')
html = html.replace('<h1>Cost <span>Dashboard</span></h1>', '<h1>API <span>Costing</span></h1>')
html = html.replace('Real-time System API spend monitoring', 'Detailed API usage and billing metrics')

with open("frontend/api-costing.html", "w", encoding="utf-8") as f:
    f.write(html)
