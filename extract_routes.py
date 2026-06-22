import os, re

for root, dirs, files in os.walk('backend'):
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            with open(path, 'r', encoding='utf-8') as file:
                content = file.read()
                # Find @*.route("/api/path") decorators
                routes = re.findall(r'@(?:app|bp|ops_bp|auth_bp|system_bp|attendance_bp)\.route\([\'\"](.*?)[\'\"]', content)
                if routes:
                    print(f'{path}:')
                    for r in routes:
                        print(f'  - {r}')
