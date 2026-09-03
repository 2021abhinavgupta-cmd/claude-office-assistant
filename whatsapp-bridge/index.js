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

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
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

if (!TOKEN) {
  console.error("bridge: set WHATSAPP_BRIDGE_TOKEN (or STORAGE_SYNC_TOKEN / FLASK_SECRET_KEY).");
  process.exit(1);
}

const log = (...a) => console.log(`[${new Date().toTimeString().slice(0, 8)}]`, ...a);

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

// In a group we only act when the bot was addressed: a "lumina ..." prefix,
// an @-mention of the bot, or a reply to one of its own messages. Returns the
// cleaned question, or null to ignore the message.
const GROUP_PREFIX = /^\s*(@?lumina|@?assistant)[\s:,\-]+|^\s*[/!](ask|lumina)\s+/i;
function groupQuery(text, addressed) {
  if (GROUP_PREFIX.test(text)) return text.replace(GROUP_PREFIX, "").trim();
  if (addressed) return text.replace(/@\d{5,}/g, "").trim(); // strip mention tokens
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
  const data = await res.json();
  return data.reply || null;
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
        if (!rawText) continue;

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
          const q = groupQuery(rawText, addressed);
          if (!q) continue;
          text = q;
          groupId = jid;
          groupNm = await groupName(sock, jid);
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

        let reply;
        try {
          reply = await askLumina(from, text, groupId, groupNm);
        } catch (e) {
          log(`Lumina error: ${e.message}`);
          // in a group, stay quiet on error rather than posting noise for all
          reply = isGroup
            ? null
            : "Sorry — I couldn't reach the assistant just now. Try again in a moment.";
        }

        await sock.sendPresenceUpdate("paused", jid).catch(() => {});
        if (reply) {
          await sock.sendMessage(jid, { text: reply });
          log(`out ${from}: ${reply.slice(0, 80)}`);
        } else if (isGroup) {
          log(`  (no reply — group ${bareJid(jid)} not on allow-list, or sender not staff)`);
        }
      } catch (e) {
        log(`handler error: ${e.message}`);
      }
    }
  });
}

start().catch((e) => {
  console.error("bridge failed to start:", e);
  process.exit(1);
});
