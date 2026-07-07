with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if line.startswith("    cr_date_val = t.get(\"creation_date\")"):
        # Fix indentation for lines starting from here up to the comment # If the user has
        for j in range(i, len(lines)):
            if "# If the user has" in lines[j]:
                break
            if lines[j].strip():
                if lines[j].startswith("    "):
                    lines[j] = "    " + lines[j]

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
