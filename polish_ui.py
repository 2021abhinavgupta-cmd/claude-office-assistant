"""
polish_ui.py — Remove AI-sounding text and fix UI polish issues
Only touches project source files, not venv packages.
"""
import os
import re

PROJECT_FILES = [
    "frontend/index.html",
    "frontend/login.html",
    "frontend/client-login.html",
    "frontend/client-dashboard.html",
    "frontend/client-portal.html",
    "frontend/client-onboard.html",
    "frontend/client-admin.html",
    "frontend/dashboard.html",
    "frontend/add-tasks.html",
    "frontend/my-tasks.html",
    "frontend/projects.html",
    "frontend/standup.html",
    "frontend/presentation.html",
    "frontend/project.html",
    "frontend/html-generator.html",
    "frontend/app.js",
    "frontend/optimizer.js",
    "frontend/project.js",
    "frontend/style.css",
    "backend/system_prompt.py",
    "backend/model_router.py",
    "backend/routes/ops.py",
    "backend/routes/system.py",
    "backend/app.py",
]

# Ordered list of (find, replace) pairs
REPLACEMENTS = [
    # Remove AI-generated tool name from page titles / meta
    ("| AI Workspace", ""),
    ("AI office assistant", "office workspace"),
    ("AI office assistant — code, write, design, analyze with System. Multiple conversations per employee.", 
     "Internal workspace for your team."),
    ("Agency Portal | AI Workspace", "Agency Portal"),

    # Remove "System" as a product name (the AI model renamed to System) in UI text
    # But keep backend variable names intact — we target only display strings
    ("Message System…", "Type your message…"),
    ("Message System… (or drop a file here)", "Type your message… (or drop a file here)"),
    ("Ask me anything — code, design, write, analyze, plan.", "Use the cards below or type to get started."),
    ("How can I help you today?", "What are you working on?"),
    ("Ask anything or pick a card above…", "Type here or pick a card above…"),
    ("Optimizing with System Haiku…", "Optimizing…"),
    ("Powered by Haiku · ~$0.00008", "Quick optimizer"),
    ("AI Enhanced", "Suggested"),
    ("✎ click to edit", "click to edit"),
    # Thinking label  
    ("Thinking...", "Loading…"),
    # Model names in dropdowns
    ("System 4.6 Sonnet", "Standard"),
    ("System 4.5 Haiku", "Fast"),
    ("Auto (Smart Routing)", "Auto"),
    # Prefs screen
    ("Have you used System before?", "Have you used our portal before?"),
    ("Have you used System before? ", "Have you used our portal before? "),
    ("Yes, I use it ", "Yes "),
    # Sidebar labels
    (" Memory", "Saved Notes"),
    (" Budget", "Usage"),
    (" Projects", "Projects"),
    (" Recent Chats", "Recent Chats"),
    # Welcome screen icon — replace ✦ text nodes with AP initials
    (">✦<", ">AP<"),
    # modal icon
    ('class="modal-icon">✦<', 'class="modal-icon">AP<'),
    ('class="brand-icon">✦<', 'class="brand-icon">AP<'),
    ('class="welcome-icon">✦<', 'class="welcome-icon">AP<'),
    # Logo icon in login/client-login
    ('class="logo-icon"></div>', 'class="logo-icon">AP</div>'),
    # Export button labels  
    (" PDF", "PDF"),
    (" DOCX", "DOCX"),
    (" PPT", "PPT"),
    # Remove "AI" from optimizer badge
    ("~$0.00008", ""),
    # dashboard back to index label
    (" dashboard", "Dashboard"),
    # connected status — remove techy text
    ("auto-detecting task", ""),
    # Comment in code that reveals AI origin
    ("System-like Multi-Chat UI", "Multi-Chat UI"),
    # "System" in project custom instructions placeholder
    ("Set specific instructions for how System should behave within this project (tone, role, formatting)...",
     "Set specific instructions for this project (tone, role, formatting)..."),
    # "System" in upload section
    ("Upload documents, code, or context that System can always reference.",
     "Upload documents, code, or context for this project."),
    # Projects page
    ("Projects help organise your work. Upload docs, code, and files to create themed collections System can reference again and again.",
     "Projects help organise your work. Upload docs, code, and files to create themed collections."),
    # Export alert
    ('alert(` Export error: ${e.message}`)', 'alert(`Export failed: ${e.message}`)'),
    ('alert("No AI response found yet. Ask System something first!")', 'alert("No content to export yet.")'),
    # Opt panel keep/use button labels
    ("✕ Keep original", "Keep original"),
    ("✓ Use optimized →", "Use this →"),
    # Thinking indicator label in index.html
    ("<span class=\"thinking-label\">Thinking...</span>", "<span class=\"thinking-label\">Loading…</span>"),
    # Reconnecting/thinking indicator
    ("-- shown while System thinks --", "-- shown while loading --"),
    # style.css comment
    ("System-like Multi-Chat UI", "Multi-Chat UI"),
]

def process(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    new = content
    for find, replace in REPLACEMENTS:
        new = new.replace(find, replace)
    if new != content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"Updated: {path}")

for p in PROJECT_FILES:
    process(p)

print("Done.")
