with open('backend/notion_store.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(462, 472):
    # Add 4 spaces of indentation
    lines[i] = "    " + lines[i]

with open('backend/notion_store.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
