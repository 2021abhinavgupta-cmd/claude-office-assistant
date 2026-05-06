# 🚀 Claude Office Assistant - Complete User Guide

Welcome to the **Claude Office Assistant**. This is not just a standard chat interface; it is a highly customized, production-grade AI platform specifically tailored for your team's workflow and budget constraints. 

This document outlines the hidden superpowers built into the app and how to squeeze the absolute maximum value out of it.

---

## 🧠 1. The Auto-Memory & Team Cross-Talk System
This is one of the most powerful features of the app. Claude is actively maintaining a persistent memory bank for every single employee (Vidit, Nupur, Abhinav, Kshitij, Raj, Mohit, Tanaya, Happy).

* **Auto-Extraction:** You don't have to tell Claude to remember things. If you say *"I'm working on a dark-mode UI today,"* Claude will secretly execute an invisible XML command to save that to your profile. The next time you open the app, it already knows.
* **Cross-Assigning Memories:** Because Claude knows your entire roster, you can give it instructions for *other* people!
  * **Example Prompt:** *"Hey Claude, please remember that whenever Happy generates videos this week, they need to be in 9:16 aspect ratio."* 
  * Claude will save this to Happy's profile. When Happy logs in, his videos will automatically be 9:16.

## 🔀 2. Smart Cost-Routing (Saving you 80%)
You have a strict $20/month budget limit. To maximize this, the app does **not** use the expensive Claude 3.5 Sonnet model for everything.
Instead, the backend analyzes every prompt you send and routes it automatically:
* **Haiku (Ultra-Cheap & Fast):** Automatically used for emails, summaries, text analysis, legal questions, and internal announcements.
* **Sonnet (Complex & Expensive):** Strictly reserved for intense logic tasks like writing Code, Deep Data Analysis, and HTML Design.

## 📅 3. The Meeting Assistant
Stop wasting 20 minutes formatting meeting notes.
* **How to use:** Copy and paste the raw, messy transcript (or bullet points) from any meeting into the chat box.
* **What happens:** The app detects it is a meeting and forces Claude into an Executive Assistant persona. It will instantly spit out a perfectly structured document with: 
  1. An Executive Summary 
  2. Key Decisions 
  3. Action Items (with checkboxes and owner names) 
  4. An agenda for the next sync.

## 📢 4. Internal Announcements Generator
Need to tell the team the server is going down or a holiday is coming up?
* **How to use:** Click the "Announcement" button on the home screen or type *"office update: server down sat 10am-2pm"*.
* **What happens:** It automatically drafts a polite, highly professional office memo with a Subject line, proper formatting, and greetings, ready to be copy-pasted directly into your WhatsApp group or Email.

## 🗂️ 5. Project Knowledge Bases
If your team is working on a long-term initiative (e.g., "Website Redesign"), do not start a blank chat every time!
* **How to use:** Open the left sidebar and click "New Project". 
* **The Magic:** You can upload static PDF/TXT files and write "Custom Instructions" for the project. Every time an employee starts a new chat *inside* that project, those documents and rules are injected into Claude's brain for free. It acts as a shared brain for the team.

## ⚡ 6. The Prompt Optimizer Button
See the small `⚡` button next to the chat box? Use it when you only have the energy to type a 4-word prompt.
* **How to use:** Type *"Write a python login script"* and click `⚡`.
* **What happens:** The backend will secretly ping Claude's cheapest model to expand your 4 words into a massive, highly-detailed prompt (specifying edge cases, security, styling, etc.), and *then* run it. You get senior-level prompt engineering with zero effort.

## 📊 7. Automated Weekly Digest
As an admin, you don't need to manually check the dashboard to see who is using the budget.
* **How to use:** Run `python scripts/weekly_summary.py` every Friday.
* **What happens:** It queries the high-speed SQLite database and generates a Markdown file (`logs/weekly_digest.md`) showing:
  - Total API cost for the week.
  - The Top 3 most active employees.
  - The exact tasks the team relies on the most.

## 🎨 8. Pre-Programmed Tone Profiles
When someone logs into the app, Claude changes its personality to match them perfectly:
* **Happy:** Creative, highly visual, focused on video mechanics.
* **Abhinav:** Technical, code-centric, uses bullet points.
* **Vidit:** Formal, detailed, highly analytical.
You never need to type *"Keep it brief"* or *"Format as bullets"* again—it is hardcoded into your login.

---

### 💻 Infrastructure Notes for the Admin
- **Concurrency:** The server runs on `gunicorn` with `gevent` workers. It can effortlessly handle all 8 of your employees streaming text at the exact same time without blocking.
- **Database:** It uses SQLite with Write-Ahead Logging (`WAL` mode). It is completely thread-safe and costs $0.
- **Network Optimization:** All API responses are heavily compressed via `Flask-Compress`, making the app lightning-fast on office WiFi, and all user input is aggressively stripped of hidden whitespaces/newlines to shave down your Anthropic token bill. 
