/**
 * Lumina WhatsApp bridge — REPLY ONLY.
 *
 * Links a *dedicated* WhatsApp number (not your personal one — it can be
 * banned) to Lumina's WhatsApp agent using Baileys (unofficial WhatsApp
 * Web protocol, no Meta approval needed).
 *
 * Flow: someone messages the dedicated number
 *       -> this bridge POSTs {from, text} to  LUMINA_URL/whatsapp/bridge
 *       -> Lumina runs the agent (identity, tools, web search, context)
 *       -> bridge sends the returned reply back
 *
 * It NEVER initiates a conversation. It only ever replies to an inbound
 * message. Status, channels and its own messages are ignored. Group
 * messages are answered only when the bot is addressed ("lumina ...",
 * an @-mention, or a reply to it) AND Lumina's group allow-list permits
 * that group AND the asker is a known employee — see whatsapp_agent.py.
 *
 * Setup:
 *   cd whatsapp-bridge && npm install
 *   set LUMINA_URL=https://lumina.mmga.agency
 *   set WHATSAPP_BRIDGE_TOKEN=<same value you set on Railway>
 *   node index.js           # scan the QR with the dedicated number, once
 *
 * The pairing/session is saved in ./auth (gitignored). Delete that folder
 * to unpair / switch numbers.
 *
 * Keep alive across reboots: Task Scheduler "At log on" ->
 *   node.exe <path>\whatsapp-bridge\index.js
 */

import http from "http";
import fs from "fs";
import path from "path";
import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import pino from "pino";

const LUMINA_URL = (process.env.LUMINA_URL || "https://lumina.mmga.agency").replace(/\/+$/, "");
const TOKEN =
  process.env.WHATSAPP_BRIDGE_TOKEN ||
  process.env.STORAGE_SYNC_TOKEN ||
  process.env.FLASK_SECRET_KEY ||
  "";
const AUTH_DIR = process.env.AUTH_DIR || "./auth";
// loopback-only endpoint the laptop companion uses to push a proactive
// message (e.g. the noon attendance roll-call). Strangers still only ever
// get a reply — nothing on WhatsApp can trigger an outbound, only something
// on this machine holding TOKEN.
const SEND_PORT = parseInt(process.env.BRIDGE_HTTP_PORT || "8787", 10);

if (!TOKEN) {
  console.error("bridge: set WHATSAPP_BRIDGE_TOKEN (or STORAGE_SYNC_TOKEN / FLASK_SECRET_KEY).");
  process.exit(1);
}

const log = (...a) => console.log(`[${new Date().toTimeString().slice(0, 8)}]`, ...a);

let currentSock = null;   // set on every (re)connect, used by the send endpoint

// ── stickers ────────────────────────────────────────────────────────────────
// A local pool the bot can send at random or by mood. You teach it stickers
// by DMing "!stickers" and then sending it the stickers you want (from your
// Business app favourites, forwarded here). Files + a _tags.json map live in
// ./stickers/ (gitignored). Nothing here ever fetches from the internet.
const STICKER_DIR = process.env.STICKER_DIR || "./stickers";
const STICKER_TAGS_FILE = path.join(STICKER_DIR, "_tags.json");
// chance (1 in N) of tacking a random sticker onto a normal DM reply
const STICKER_RATE = Math.max(0, parseInt(process.env.STICKER_RATE || "4", 10));
let stickerTags = {};                 // { "file.webp": ["smug","bruh"] }
const stickerCapture = new Map();     // jid -> { until: ms, lastFile: string|null, lastAt: ms }

// Burst reactions: when a group the bot operates in goes lively (many messages,
// several people, short window) it reads the recent chat and MAY drop a fitting
// sticker. Heavily throttled: a long cooldown, and Lumina says "none" most of
// the time. Only runs for groups where the bot has already answered something
// (i.e. allow-listed + operational).
const BURST_WINDOW_MS = parseInt(process.env.BURST_WINDOW_MS || "90000", 10);
const BURST_COUNT = parseInt(process.env.BURST_COUNT || "6", 10);
const BURST_COOLDOWN_MS = parseInt(process.env.BURST_COOLDOWN_MS || "1800000", 10); // 30 min
const groupBuf = new Map();       // jid -> [{ name, text, ts }]
const lastBurst = new Map();      // jid -> ms of last burst check
const activeGroups = new Set();   // groups Lumina has replied in at least once

function pushGroupMsg(jid, name, text) {
  let buf = groupBuf.get(jid);
  if (!buf) { buf = []; groupBuf.set(jid, buf); }
  const now = Date.now();
  buf.push({ name, text, ts: now });
  while (buf.length > 20 || (buf.length && now - buf[0].ts > 300000)) buf.shift();
}

async function maybeReactToBurst(sock, jid, groupNm) {
  if (STICKER_RATE <= 0 || !activeGroups.has(jid) || !stickerFiles().length) return;
  const now = Date.now();
  const recent = (groupBuf.get(jid) || []).filter((m) => now - m.ts <= BURST_WINDOW_MS);
  const people = new Set(recent.map((m) => m.name));
  if (recent.length < BURST_COUNT || people.size < 2) return;
  if (now - (lastBurst.get(jid) || 0) < BURST_COOLDOWN_MS) return;
  lastBurst.set(jid, now);   // start the cooldown whether or not we react
  const tags = Array.from(
    new Set(Object.values(stickerTags).flat().map((t) => String(t).toLowerCase()))
  );
  try {
    const res = await fetch(`${LUMINA_URL}/whatsapp/bridge`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
      body: JSON.stringify({
        burst: true, group_id: jid, group_name: groupNm, tags,
        messages: recent.map((m) => ({ name: m.name, text: m.text })),
      }),
      signal: AbortSignal.timeout(30000),
    });
    if (!res.ok) return;
    const mood = (await res.json())?.sticker;
    if (mood) {
      const sp = pickSticker(mood);
      if (sp) {
        await sendSticker(sock, jid, sp);
        log(`burst react in ${bareJid(jid)}: ${mood}`);
      }
    }
  } catch (e) {
    log(`burst react error: ${e.message}`);
  }
}

function loadStickerTags() {
  try {
    stickerTags = JSON.parse(fs.readFileSync(STICKER_TAGS_FILE, "utf8"));
  } catch {
    stickerTags = {};
  }
}
function saveStickerTags() {
  try {
    fs.writeFileSync(STICKER_TAGS_FILE, JSON.stringify(stickerTags, null, 2));
  } catch (e) {
    log(`sticker tags save failed: ${e.message}`);
  }
}
function stickerFiles() {
  try {
    return fs.readdirSync(STICKER_DIR).filter((f) => /\.webp$/i.test(f));
  } catch {
    return [];
  }
}
function pickSticker(mood) {
  const files = stickerFiles();
  if (!files.length) return null;
  const m = (mood || "").trim().toLowerCase();
  if (m && m !== "random") {
    const tagged = files.filter((f) =>
      (stickerTags[f] || []).some((t) => t.toLowerCase().includes(m) || m.includes(t.toLowerCase()))
    );
    if (tagged.length) return path.join(STICKER_DIR, tagged[Math.floor(Math.random() * tagged.length)]);
  }
  return path.join(STICKER_DIR, files[Math.floor(Math.random() * files.length)]);
}
async function sendSticker(sock, jid, filePath) {
  try {
    await sock.sendMessage(jid, { sticker: fs.readFileSync(filePath) });
    log(`sticker -> ${bareJid(jid)}: ${path.basename(filePath)}`);
    return true;
  } catch (e) {
    log(`sticker send failed: ${e.message}`);
    return false;
  }
}
try {
  fs.mkdirSync(STICKER_DIR, { recursive: true });
} catch {
  /* ignore */
}
loadStickerTags();

function startSendServer() {
  const srv = http.createServer((req, res) => {
    if (req.method !== "POST" || req.url !== "/send") {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    if ((req.headers["authorization"] || "") !== `Bearer ${TOKEN}`) {
      res.writeHead(401);
      res.end("unauthorized");
      return;
    }
    let body = "";
    req.on("data", (c) => {
      body += c;
      if (body.length > 65536) req.destroy();
    });
    req.on("end", async () => {
      try {
        const { to, text } = JSON.parse(body || "{}");
        if (!to || !text) {
          res.writeHead(400);
          res.end('{"error":"to and text required"}');
          return;
        }
        if (!currentSock) {
          res.writeHead(503);
          res.end('{"error":"not connected"}');
          return;
        }
        const sent = await currentSock.sendMessage(String(to), { text: String(text) });
        const waId = sent?.key?.id || null;
        log(`push -> ${to}: ${String(text).slice(0, 80)}`);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true, id: waId }));
      } catch (e) {
        log(`push error: ${e.message}`);
        res.writeHead(500);
        res.end(JSON.stringify({ error: e.message }));
      }
    });
  });
  srv.on("error", (e) => {
    if (e.code === "EADDRINUSE") {
      // a previous instance hasn't released the port yet — retry
      log(`send port ${SEND_PORT} busy, retrying in 3s`);
      setTimeout(() => srv.listen(SEND_PORT, "127.0.0.1"), 3000);
    } else {
      log(`send server error: ${e.message}`);
    }
  });
  srv.listen(SEND_PORT, "127.0.0.1", () => log(`send endpoint on 127.0.0.1:${SEND_PORT}`));
}

// Baileys redelivers messages on reconnect — remember the last N ids so a
// redelivery doesn't trigger a second reply.
const seen = new Set();
function alreadyHandled(id) {
  if (!id) return false;
  if (seen.has(id)) return true;
  seen.add(id);
  if (seen.size > 500) seen.delete(seen.values().next().value);
  return false;
}

function extractText(message) {
  if (!message) return "";
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    ""
  ).trim();
}

// digits of a jid ("<phone>:<device>@s.whatsapp.net" / "<id>@lid" -> "<phone>")
function bareJid(j) {
  return (j || "").split("@")[0].split(":")[0];
}

// In a group we act when the bot was addressed: the word "lumina" anywhere in
// the message, an @-mention of the bot, or a reply to one of its own messages.
// Returns the question to forward, or null to ignore the message.
const GROUP_PREFIX = /^\s*(@?lumina|@?assistant)[\s:,\-]+|^\s*[/!](ask|lumina)\s+/i;
const GROUP_NAME_RE = /\blumina\b/i;
function groupQuery(text, addressed) {
  if (GROUP_PREFIX.test(text)) return text.replace(GROUP_PREFIX, "").trim();
  if (GROUP_NAME_RE.test(text)) return text.trim();          // name mentioned anywhere
  if (addressed) return text.replace(/@\d{5,}/g, "").trim(); // @-mention or reply
  return null;
}

const _groupNames = new Map();
async function groupName(sock, jid) {
  if (_groupNames.has(jid)) return _groupNames.get(jid);
  let name = "";
  try {
    name = (await sock.groupMetadata(jid))?.subject || "";
  } catch {
    /* no metadata access */
  }
  _groupNames.set(jid, name);
  return name;
}

async function askLumina(from, text, groupId, groupNm) {
  const body = { from, text };
  if (groupId) {
    body.group_id = groupId;
    if (groupNm) body.group_name = groupNm;
  }
  const res = await fetch(`${LUMINA_URL}/whatsapp/bridge`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(90000), // agent tool-loop + web search can be slow
  });
  if (!res.ok) {
    const bodyTxt = await res.text().catch(() => "");
    throw new Error(`Lumina ${res.status}: ${bodyTxt.slice(0, 200)}`);
  }
  return await res.json();   // { reply, sticker?, sticker_cmd? }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: "silent" }),
    browser: ["Lumina Bridge", "Chrome", "1.0.0"],
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });
  currentSock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      console.log("\nScan this QR with the DEDICATED WhatsApp number (Linked devices > Link a device):\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      log(`connected. Bridging  ${LUMINA_URL}  (reply-only).`);
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        log("logged out on the phone. Delete ./auth and re-pair. Exiting.");
        process.exit(1);
      }
      if (code === DisconnectReason.connectionReplaced) {
        // another bridge instance took over this session — don't fight it,
        // just exit. (If it was a stale orphan, the supervisor restarts us.)
        log("connection replaced by another instance. Exiting.");
        process.exit(1);
      }
      log(`connection closed (${code}); reconnecting in 3s…`);
      setTimeout(start, 3000);
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      try {
        if (!msg.message || msg.key.fromMe) continue;
        if (alreadyHandled(msg.key.id)) continue;
        const jid = msg.key.remoteJid || "";   // conversation address (may be a @lid)
        // no status, no channels/newsletters/broadcasts
        if (
          jid === "status@broadcast" ||
          jid.endsWith("@newsletter") ||
          jid.endsWith("@broadcast")
        )
          continue;

        const isGroup = jid.endsWith("@g.us");
        const rawText = extractText(msg.message);

        // ── sticker capture (DM only) ──────────────────────────────────────
        const cap = !isGroup ? stickerCapture.get(jid) : null;
        const capActive = cap && cap.until > Date.now();
        if (!isGroup && msg.message?.stickerMessage) {
          if (!capActive) continue;    // not teaching right now — ignore stray stickers
          try {
            const buf = await downloadMediaMessage(msg, "buffer", {}, {
              reuploadRequest: sock.updateMediaMessage,
            });
            const fname = `s_${Date.now().toString(36)}.webp`;
            fs.writeFileSync(path.join(STICKER_DIR, fname), buf);
            cap.lastFile = fname;
            cap.lastAt = Date.now();
            const n = stickerFiles().length;
            await sock.sendMessage(jid, { text: `Saved (${n} total). Send a one-word tag now to categorise it, or another sticker. "!stickers done" to stop.` });
            log(`sticker saved from ${bareJid(jid)}: ${fname} (${n} total)`);
          } catch (e) {
            log(`sticker capture failed: ${e.message}`);
            await sock.sendMessage(jid, { text: "Couldn't save that one, try again." }).catch(() => {});
          }
          continue;
        }
        if (!rawText) continue;

        // during capture, a short word right after a saved sticker = its tag(s)
        if (capActive && cap.lastFile && Date.now() - (cap.lastAt || 0) < 45000
            && !/^!/.test(rawText) && rawText.trim().split(/\s+/).length <= 3) {
          const tags = rawText.toLowerCase().split(/[\s,]+/).filter(Boolean);
          stickerTags[cap.lastFile] = Array.from(new Set([...(stickerTags[cap.lastFile] || []), ...tags]));
          saveStickerTags();
          cap.lastAt = 0;   // one tag set per sticker
          await sock.sendMessage(jid, { text: `Tagged: ${tags.join(", ")}` });
          continue;
        }

        let text = rawText;
        let groupId = null;
        let groupNm = null;

        if (isGroup) {
          // only respond when the bot was addressed
          const ctx = msg.message?.extendedTextMessage?.contextInfo;
          const mentioned = Array.isArray(ctx?.mentionedJid)
            ? ctx.mentionedJid.map(bareJid)
            : [];
          const botIds = [bareJid(sock.user?.id), bareJid(sock.user?.lid)].filter(Boolean);
          const addressed =
            mentioned.some((m) => botIds.includes(m)) ||
            botIds.includes(bareJid(ctx?.participant)); // reply to the bot's msg

          groupNm = await groupName(sock, jid);
          // observe every message in the group for burst detection
          pushGroupMsg(jid, msg.pushName || bareJid(msg.key.participant || "") || "someone", rawText);
          maybeReactToBurst(sock, jid, groupNm).catch(() => {});

          const q = groupQuery(rawText, addressed);
          if (!q) continue;
          text = q;
          groupId = jid;
        }

        // Identity is the person, by phone number. In a DM that's key.senderPn;
        // in a group it's key.participantPn. WhatsApp increasingly addresses
        // people by LID (privacy id) — fall back to the LID->PN map, then raw.
        const pnField = isGroup ? msg.key.participantPn : msg.key.senderPn;
        const rawPart = isGroup ? (msg.key.participant || "") : jid;
        let idJid = pnField || "";
        if (!idJid && rawPart.endsWith("@lid")) {
          try {
            idJid = (await sock.signalRepository?.lidMapping?.getPNForLID?.(rawPart)) || "";
          } catch { /* not available on this Baileys build */ }
        }
        if (!idJid) idJid = rawPart;
        const from = bareJid(idJid);
        if (rawPart.endsWith("@lid") && idJid !== rawPart)
          log(`  (resolved LID ${bareJid(rawPart)} -> ${from})`);
        else if (rawPart.endsWith("@lid"))
          log(`  (WARN: LID ${bareJid(rawPart)} — no phone number available)`);

        if (isGroup)
          log(`grp ${bareJid(jid)}${groupNm ? ` (${groupNm})` : ""} <- ${from}: ${text.slice(0, 80)}`);
        else log(`in  ${from}: ${text.slice(0, 80)}`);

        await sock.readMessages([msg.key]).catch(() => {});
        await sock.sendPresenceUpdate("composing", jid).catch(() => {});

        let data;
        try {
          data = await askLumina(from, text, groupId, groupNm);
        } catch (e) {
          log(`Lumina error: ${e.message}`);
          // in a group, stay quiet on error rather than posting noise for all
          data = {
            reply: isGroup
              ? null
              : "Sorry — I couldn't reach the assistant just now. Try again in a moment.",
          };
        }

        // sticker-management directives (DM only, employee-gated by Lumina)
        if (!isGroup && data?.sticker_cmd) {
          const c = data.sticker_cmd;
          if (c === "on") {
            stickerCapture.set(jid, { until: Date.now() + 5 * 60000, lastFile: null, lastAt: 0 });
          } else if (c === "off") {
            stickerCapture.delete(jid);
          } else if (c === "clear") {
            for (const f of stickerFiles()) {
              try { fs.unlinkSync(path.join(STICKER_DIR, f)); } catch { /* ignore */ }
            }
            stickerTags = {};
            saveStickerTags();
          } else if (c === "list") {
            const fl = stickerFiles();
            data.reply = `${fl.length} sticker(s) saved, ${fl.filter((f) => (stickerTags[f] || []).length).length} tagged.`;
          }
        }

        const reply = data?.reply || null;
        await sock.sendPresenceUpdate("paused", jid).catch(() => {});
        if (reply) {
          await sock.sendMessage(jid, { text: reply });
          if (isGroup) activeGroups.add(jid);   // burst detection may run here now
          log(`out ${from}: ${reply.slice(0, 80)}`);
          // a sticker the agent explicitly asked for, or (DM only) random flair
          let sp = null;
          if (data?.sticker) sp = pickSticker(data.sticker);
          else if (!isGroup && STICKER_RATE > 0 && Math.random() < 1 / STICKER_RATE)
            sp = pickSticker(null);
          if (sp) await sendSticker(sock, jid, sp);
        } else if (isGroup) {
          log(`  (no reply — group ${bareJid(jid)} not on allow-list, or sender not staff)`);
        }
      } catch (e) {
        log(`handler error: ${e.message}`);
      }
    }
  });
}

startSendServer();

start().catch((e) => {
  console.error("bridge failed to start:", e);
  process.exit(1);
});
