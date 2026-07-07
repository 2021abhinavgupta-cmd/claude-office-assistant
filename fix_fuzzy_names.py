with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "target_uids = [emp_name_to_id.get(n) for n in names if emp_name_to_id.get(n)]" in line:
        replacement = """            target_uids = []
            for n in names:
                for ename, eid in emp_name_to_id.items():
                    if ename.lower() in n.lower():
                        if eid not in target_uids:
                            target_uids.append(eid)
"""
        lines[i] = replacement

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
