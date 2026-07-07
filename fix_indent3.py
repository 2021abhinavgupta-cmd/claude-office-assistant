with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if line.strip() == "is_creation_today = False":
        start_idx = i
        break

for i in range(start_idx, len(lines)):
    if "# If the user has" in lines[i]:
        end_idx = i
        break

lines_to_replace = lines[start_idx:end_idx]

replacement = """        is_creation_today = False
        has_creation_date = False
        is_future_creation = False
        cr_date_val = t.get("creation_date")
        
        if cr_date_val:
            # It's an actual Notion Date Property
            has_creation_date = True
            try:
                cr_date = cr_date_val.split("T")[0]
                if cr_date == today_str and s in ("not_started", "need_to_start", ""):
                    is_creation_today = True
                elif cr_date > today_str:
                    is_future_creation = True
            except: pass
        else:
            desc = t.get("description", "") or t.get("content", "") or t.get("brief", "") or ""
            if desc:
                import re as _re
                cr_match = _re.search(r'creation date\s*:\s*([\d-]+)', desc, _re.IGNORECASE)
                if cr_match:
                    has_creation_date = True
                    try:
                        cr_date = cr_match.group(1).strip()
                        if _re.match(r"^\d{2}-\d{2}-\d{4}$", cr_date):
                            cr_date = f"{cr_date[6:10]}-{cr_date[3:5]}-{cr_date[0:2]}"
                        if cr_date == today_str and s in ("not_started", "need_to_start", ""):
                            is_creation_today = True
                        elif cr_date > today_str:
                            is_future_creation = True
                    except: pass
"""

lines = lines[:start_idx] + [replacement] + lines[end_idx:]

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
