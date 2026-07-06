with open('frontend/projects.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    if 'const API =' in line:
        new_lines.append(line)
        new_lines.append("function esc(s){if(!s)return'';return String(s).replace(/[&<>\"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#039;'})[m]);}\n")
        continue
    
    # HTML template in sheets
    if '${title}' in line and '<td' in line:
        line = line.replace('${title}', '${esc(title)}')
    if '${content}' in line and '<td' in line:
        line = line.replace('${content}', '${esc(content)}')
    if '${idea}' in line and '<td' in line:
        line = line.replace('${idea}', '${esc(idea)}')
    if '${scripts}' in line and '<td' in line:
        line = line.replace('${scripts}', '${esc(scripts)}')
    if '${caption}' in line and '<td' in line:
        line = line.replace('${caption}', '${esc(caption)}')
    if '${link}' in line and '<td' in line:
        line = line.replace('${link}', '${esc(link)}')
    if '${myNotes}' in line and '<td' in line:
        line = line.replace('${myNotes}', '${esc(myNotes)}')
        
    # Kanban template
    if '${c.title}' in line and 'class="cp-title"' in line:
        line = line.replace('${c.title}', '${esc(c.title)}')
        
    # Calendar popup
    if '${extContent?' in line:
        line = line.replace('${extContent?', '${esc(extContent)?')
        line = line.replace('${extContent.replace', '${esc(extContent).replace')
    if '${extIdea?' in line:
        line = line.replace('${extIdea?', '${esc(extIdea)?')
        line = line.replace('${extIdea.replace', '${esc(extIdea).replace')
    if '${extScripts?' in line:
        line = line.replace('${extScripts?', '${esc(extScripts)?')
        line = line.replace('${extScripts.replace', '${esc(extScripts).replace')
        
    # For extContent inside the b tag in popup: `<b>Content:</b> ${extContent.replace...`
    # The regex .replace is operating on the string, but wait, the regex .replace is for URL linking:
    # `${extContent.replace(/(https?:\/\/[^\s<>"']+)/g, '<a href="$1" ...>$1</a>')}`
    # If we escape first, then replace URLs, that works. But wait, we shouldn't `esc(extContent).replace()` directly if we're not careful because URL replace injects `<a href>`.
    # Wait, the replacement in the file is already doing `extContent.replace(...)`. If we change `extContent.replace` to `esc(extContent).replace`, it escapes the text first, which means `&`, `<`, `>` are replaced. Then the URL regex matches URLs and injects `<a>` tags. That is perfectly safe!

    new_lines.append(line)

with open('frontend/projects.html', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
