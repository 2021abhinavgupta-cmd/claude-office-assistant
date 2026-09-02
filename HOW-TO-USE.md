# How to use the new Lumina features

Plain-language guide. No coding needed for anything here.

---

## 1. The WhatsApp assistant

Message the assistant's WhatsApp number and it answers using Lumina's live data
and your uploaded documents. It only replies — it never messages first.

**Who can use it**
- **Team members** whose WhatsApp number is saved in Lumina — full access.
- **Clients** whose number is added to their portal account — they only ever
  see their own deliverables, nothing internal.
- Anyone else gets a polite "this number isn't linked" and nothing more.

**Things the team can ask**
- "What are my tasks?"
- "What's due this week for Omotec?"
- "Which clients have overdue work?"
- "What's the caption for MMGA's Friday reel?"
- "What's the script for Omotec's next video?"
- "What's in the Omotec brand brief about colours?"
- "Give me the team overview."
- "What's the latest news on <topic>?" (it can search the web)

**Things a client can ask** (about their own work only)
- "What's the status of my deliverables?"
- "What's coming up this week?"
- "What's the caption for my next post?"

**Follow-up questions work** — it remembers the last few messages for about 6 hours.

**To add a team member or client number:** ask whoever manages Lumina to add the
WhatsApp number (with country code) to `config/employees.json` for staff, or to
the client's portal account in the Client Admin screen.

---

## 2. The knowledge folder (using the laptop as storage)

There is a folder on the always-on laptop: **`C:\LuminaKnowledge`**

- **Add information:** drop files into that folder — PDF, Word, text, Markdown,
  Excel, CSV. Make a sub-folder per client if you like
  (`C:\LuminaKnowledge\Omotec\brief.pdf`).
- Within about 30 seconds it's uploaded to Lumina and the assistant can answer
  from it.
- **Remove information:** delete the file from the folder. It's removed from
  Lumina on the next sync too.
- **Change information:** edit and save the file. The new version replaces the
  old one automatically.

You don't run anything — a background program on the laptop does the syncing.

**What the assistant can now find:** the assistant matches on *meaning*, not just
exact words. Asking about "refunds" will find a document that only says "returns
and cancellations".

---

## 3. The content calendars ("Sheets")

You don't need to put these anywhere — the assistant already reads them live.
Each client's Sheets view in Lumina (post type, brief, content, idea,
script/copy, caption, status, assignee, dates, file link) is available over
WhatsApp. Just ask by client name, e.g. "show me Omotec's content plan" or
"what's the idea for MMGA's post on the 5th".

---

## 4. The laptop companion (runs by itself)

One program on the laptop, started automatically every time the laptop logs in.
It does seven things:

| Job | What it does |
|---|---|
| Knowledge sync | keeps `C:\LuminaKnowledge` mirrored to Lumina |
| Web research | (optional) re-fetches web pages you list and adds them to the knowledge base |
| DB backup | once a day, saves a copy of Lumina's database to `C:\lumina-backups\`; can also copy it offsite, and grabs the client-files archive weekly |
| Health check | pings Lumina every few minutes, times it, alerts you if it's **down or slow** |
| WhatsApp bridge | keeps the WhatsApp connection alive (once the number is paired) |
| Daily brief | at ~9am and ~7pm, sends you a summary: overdue tasks, what's due, who's submitted standups, sync issues, budget |
| Sheets watchdog | every ~20 min, checks each client's Google-Sheet sync and alerts if one is stuck |

Nothing to do day-to-day. If you reboot the laptop, it starts again on its own.

**Optional — web research:** create a file
`C:\LuminaKnowledge\research_sources.txt` with one line per page, like
`Competitor blog | https://example.com/blog`. Those pages get pulled in and kept
current.

**Optional — daily brief delivery:** the brief only reaches you if you set an
`ALERT_TARGETS` (or `DIGEST_TARGETS`) environment variable to a notification
address — a Telegram bot, a Discord webhook, or an email address in
[apprise](https://github.com/caronc/apprise) URL form
(e.g. `tgram://<bot-token>/<chat-id>`). Without it, the brief is just written to
the console.

**Optional — offsite backups:** add `--backup-mirror "D:\GoogleDriveFolder"` to
the launch command to also copy each backup into a cloud-synced folder, or set
`BACKUP_RCLONE_REMOTE=name:path` if you use rclone. Right now backups only live
on this one laptop.

**Optional — auto-fix stuck sheets:** add `--sheets-autopull` to the launch
command and the watchdog will also force a re-sync when it spots a problem,
instead of only alerting.

---

## 5. Connecting the WhatsApp number (one-time)

The assistant needs a dedicated WhatsApp number linked to it. A brand-new number
is blocked by WhatsApp from linking a device for the first several hours — use
the account normally (set a name/photo, message a couple of contacts) and try
again later.

When WhatsApp lets you "Link a Device":

1. Open **Task Scheduler** on the laptop → right-click **Lumina companion** → **End**.
2. Open PowerShell and run:
   ```
   cd "e:\ABhinav\projects\office assitant\whatsapp-bridge"
   node index.js
   ```
3. A QR code appears. On the dedicated phone: **WhatsApp → Settings → Linked
   Devices → Link a Device** → scan it (within ~20 seconds; if it changes,
   press Ctrl+C and run `node index.js` again).
4. Wait for `connected. Bridging ...` then press **Ctrl+C**.
5. Task Scheduler → right-click **Lumina companion** → **Properties** →
   **Actions** tab → select the row → **Edit** → delete ` --no-bridge` from the
   end of the arguments → **OK**.
6. Right-click **Lumina companion** → **Run**.

Test it: message the number from another phone — you should get a reply.

---

## 6. If something seems off

- **Assistant not replying:** check the laptop is on and logged in. In Task
  Scheduler, **Lumina companion** should show **Running**. Right-click → **Run**
  if not.
- **A document isn't being found:** confirm the file is actually in
  `C:\LuminaKnowledge` and is one of the supported types (PDF, Word, text,
  Markdown, Excel, CSV). Wait 30 seconds after adding it.
- **"Try again later" when linking WhatsApp:** that's WhatsApp's new-number
  limit, not a fault. Wait a few hours and retry.
- **Assistant gives a wrong or stale answer about tasks:** the task data comes
  straight from Lumina/Notion — fix it there and re-ask.
