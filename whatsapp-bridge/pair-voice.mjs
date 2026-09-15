// One-off: pair baileys-caller's own WhatsApp call session (separate from
// index.js's text-bridge session, even though it's the same phone number --
// a distinct linked-device slot with its own auth store). Run this directly
// in a visible console window (e.g. `Start-Process cmd -ArgumentList "/k",
// "node pair-voice.mjs"` from the whatsapp-bridge dir) so the QR code
// baileys-caller prints straight to stdout can actually be scanned --
// there's no "qr" event to listen for, it's printed as a side effect of
// connect(). Exits on its own once paired (or after a timeout), leaving the
// session in ./voice-auth for voice.js to reuse on every real call
// afterward. Re-run this if voice calling ever starts failing with a
// logged-out-style error (mirrors the text bridge's own re-pair story).
import { VoipClient } from "baileys-caller";
import fs from "fs";

const dir = process.env.VOICE_AUTH_DIR || "./voice-auth";
fs.mkdirSync(dir, { recursive: true });

console.log("Connecting baileys-caller VoIP client -- watch for a QR code below.");
console.log("Scan it with the SAME WhatsApp number already paired to the text bridge.");
console.log("");

const client = new VoipClient({ authDir: dir });

const timeout = setTimeout(() => {
  console.log("Timed out after 5 minutes waiting for pairing to complete.");
  process.exit(1);
}, 300000);

client.connect()
  .then(() => {
    clearTimeout(timeout);
    console.log("CONNECTED -- voice call session is paired and saved in", dir);
    process.exit(0);
  })
  .catch((e) => {
    clearTimeout(timeout);
    console.log("CONNECT FAILED:", e && e.stack ? e.stack : e);
    process.exit(1);
  });
