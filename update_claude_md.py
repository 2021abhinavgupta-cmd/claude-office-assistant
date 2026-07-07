with open('CLAUDE.md', 'r', encoding='utf-8') as f:
    content = f.read()

old_item_25 = """25. **Social Media Creation Dates & Standups** — For social media tasks, the "Creation Date" is stored inside the description/notes (e.g. `Creation Date: 2026-06-09`). 
    - **Calendar View**: The task appears as a normal pill on its `Post Day` (due_date). It ALSO appears as a blue `[Start]` pill on its `Creation Date`, but ONLY if the Creation Date has arrived (i.e., `<= today`). Future creation dates are hidden from the calendar to avoid clutter.
    - **Standup Auto-Fill**: Social media tasks bypass the normal "pull if due within 7 days" logic. They are ONLY auto-filled into a user's daily standup if today's date EXACTLY matches the Creation Date (meaning the work starts today), or if they are already `in_progress`, or if they reach their actual Post Day (`due_date`). This prevents premature cluttering of standups with future social media posts."""

new_item_25 = """25. **Social Media Creation Dates & Standups** — For social media tasks, the "Creation Date" is natively extracted from a dedicated Notion Date property or Created Time property (bypassing the old method of parsing the text description). 
    - **Calendar View**: The task appears as a normal pill on its `Post Day` (due_date). It ALSO appears as a blue `[Start]` pill on its `Creation Date`, but ONLY if the Creation Date has arrived (i.e., `<= today`). Future creation dates are hidden from the calendar to avoid clutter.
    - **Standup Auto-Fill**: Social media tasks bypass the normal "pull if due within 7 days" logic. They are ONLY auto-filled into a user's daily standup if today's date EXACTLY matches the Creation Date (meaning the work starts today), or if they are already `in_progress`, or if they reach their actual Post Day (`due_date`). This prevents premature cluttering of standups with future social media posts.

35. **Notion People Properties & Auto-Fill** — The "Assigned To" column in Notion may be a `people` property rather than a simple `multi_select` dropdown. 
    - **Parsing**: `notion_store.py` extracts the full names from `people` properties (e.g., "Abhinav Gupta"). 
    - **Fuzzy Matching**: Since the local employee mappings (`config/employees.json`) use first names ("Abhinav"), the backend uses fuzzy substring matching (`ename.lower() in notion_name.lower()`) to safely map tasks.
    - **API Filtering Bypass**: The Notion API errors out if you attempt to use a `multi_select` filter on a `people` property. Therefore, the "Auto-Fill" button (which only requests tasks for a specific user) fetches *all* tasks from Notion and filters them entirely in Python, avoiding API query rejections.
    
36. **Notion Column Fallbacks (Title Extraction)** — When determining a task's title from Notion, `notion_store.py` checks for the following properties in order: `"Task"` -> `"Post Title"` -> `"Post"`. This ensures tasks from Social Media boards (which use "Post 17") are not skipped as "Untitled" tasks."""

content = content.replace(old_item_25, new_item_25)

with open('CLAUDE.md', 'w', encoding='utf-8') as f:
    f.write(content)
