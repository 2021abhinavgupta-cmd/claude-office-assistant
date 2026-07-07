import re

with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_str = 'if cr_date == today_str and s == "not_started":'
new_str = 'if cr_date == today_str and s in ("not_started", "need_to_start", ""):'

content = content.replace(old_str, new_str)

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.write(content)
