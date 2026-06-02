import os
import re

# Regex to match emojis (this covers most common emojis)
EMOJI_REGEX = re.compile(r'[\U00010000-\U0010ffff]', flags=re.UNICODE)

REPLACEMENTS = {
    "Agency Portal": "Agency Portal",
    "agency_portal_": "agency_portal_",
    "System": "System",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
    "": "",
}

def process_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        return # Skip binary files

    new_content = content
    # Remove all emojis that fall in the supplementary planes (this catches most modern emojis)
    new_content = EMOJI_REGEX.sub('', new_content)
    
    # Specific replacements
    for k, v in REPLACEMENTS.items():
        new_content = new_content.replace(k, v)

    # Some emojis are in the BMP, so we also remove them manually if they exist
    # Let's just use a broader regex for any remaining common emojis
    # (The manual list above covers the ones we saw)

    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Updated {filepath}")

def main():
    for root, dirs, files in os.walk('.'):
        if '.git' in root or '__pycache__' in root or '.env' in root:
            continue
        for file in files:
            if file.endswith(('.html', '.js', '.css', '.py', '.md')):
                process_file(os.path.join(root, file))

if __name__ == '__main__':
    main()
