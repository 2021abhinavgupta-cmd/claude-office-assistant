import re

with open('frontend/projects.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Use regex to find and replace the broken onclick on the kavatar div inside buildCard
# The broken line contains: openAssigneePicker(event,'${taskId}','${notionId}',...
fixed = re.sub(
    r"onclick=\"event\.stopPropagation\(\);openAssigneePicker\(event,[^\"]*\)\"",
    'onclick="event.stopPropagation();openAssigneePicker(this)"',
    content
)

if fixed != content:
    with open('frontend/projects.html', 'w', encoding='utf-8') as f:
        f.write(fixed)
    print("SUCCESS: onclick fixed")
else:
    print("NOT FOUND: no change made")
