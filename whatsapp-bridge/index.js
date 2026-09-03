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
 * direct message. Groups, status, and its own messages are ignored.
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

async function askLumina(from, text) {
  const res = await fetch(`${LUMINA_URL}/whatsapp/bridge`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
    body: JSON.stringify({ from, text }),
    signal: AbortSignal.timeout(90000), // agent tool-loop + web search can be slow
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Lumina ${res.status}: ${body.slice(0, 200)}`);
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
        // DMs only — no groups, no status, no channels/newsletters
        if (
          jid === "status@broadcast" ||
          jid.endsWith("@g.us") ||
          jid.endsWith("@newsletter") ||
          jid.endsWith("@broadcast")
        )
          continue;

        const text = extractText(msg.message);
        if (!text) continue;

        // When WhatsApp addresses a contact by LID (privacy identifier) the
        // real phone number is on key.senderPn. Fall back to the LID map, then
        // to the raw jid. Lumina identifies the user by phone number.
        let idJid = msg.key.senderPn || "";
        if (!idJid && jid.endsWith("@lid")) {
          try {
            idJid = (await sock.signalRepository?.lidMapping?.getPNForLID?.(jid)) || "";
          } catch { /* not available on this Baileys build */ }
        }
        if (!idJid) idJid = jid;
        const from = idJid.split("@")[0].split(":")[0]; // digits only
        if (jid.endsWith("@lid") && idJid !== jid)
          log(`  (resolved ${jid.split("@")[0]} -> ${from})`);
        else if (jid.endsWith("@lid"))
          log(`  (WARN: LID ${jid.split("@")[0]} — no phone number available)`);
        log(`in  ${from}: ${text.slice(0, 80)}`);

        await sock.readMessages([msg.key]).catch(() => {});
        await sock.sendPresenceUpdate("composing", jid).catch(() => {});

        let reply;
        try {
          reply = await askLumina(from, text);
        } catch (e) {
          log(`Lumina error: ${e.message}`);
          reply = "Sorry — I couldn't reach the assistant just now. Try again in a moment.";
        }

        await sock.sendPresenceUpdate("paused", jid).catch(() => {});
        if (reply) {
          await sock.sendMessage(jid, { text: reply });
          log(`out ${from}: ${reply.slice(0, 80)}`);
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
