/**
 * voice.js — one-way WhatsApp announcement calls.
 *
 * Rings a real WhatsApp call to a number, speaks one message out loud via
 * local TTS, then hangs up. No listening back (see the design decision in
 * CLAUDE.md: full two-way voice was scoped out as much higher risk for
 * little extra value here — this only needs to say something, not hold a
 * conversation).
 *
 * Zero recurring cost by construction: the call rides WhatsApp itself (no
 * PSTN/telecom minute charges — this is NOT a phone call in the Twilio
 * sense, it's a WhatsApp voice call, same free-to-place mechanism as
 * calling a contact from the app), and speech is synthesized locally with
 * Piper (open source, MIT, CPU, no API key, no per-call cost).
 *
 * Everything here is lazy and best-effort: if baileys-caller isn't
 * installed, Node is too old, ffmpeg is missing, or Piper isn't set up,
 * this module reports a clear error instead of crashing the bridge — the
 * base text bridge (index.js) must keep working with zero setup even if
 * voice was never configured.
 *
 * ── One-time setup (separate from the text bridge's own pairing) ──────────
 *   1. Node >= 20 (the text bridge itself only needs >= 18 — only voice
 *      calling needs the newer runtime baileys-caller requires).
 *   2. ffmpeg on PATH (baileys-caller decodes/resamples audio through it).
 *   3. `npm install` in this directory (baileys-caller is an optional git
 *      dependency — see package.json; if it fails to build, voice calling
 *      just stays unavailable, nothing else breaks).
 *   4. Install Piper (https://github.com/OHF-Voice/piper1-gpl — a prebuilt
 *      Windows binary is published on its Releases page) and download one
 *      voice model (e.g. en_US-lessac-medium — a .onnx + .onnx.json pair).
 *      Set PIPER_BIN to the piper.exe path and PIPER_VOICE_MODEL to the
 *      .onnx model path.
 *   5. First real call triggers baileys-caller's own QR pairing (a SEPARATE
 *      link from the text bridge's — baileys-caller manages its own
 *      auth/session, it doesn't reuse index.js's `sock`). Scan it with the
 *      SAME dedicated number. Session saved in VOICE_AUTH_DIR thereafter.
 *
 * ── Genuinely unverified ────────────────────────────────────────────────
 * baileys-caller is a small, single-maintainer, not-on-npm library
 * wrapping WhatsApp's VoIP WASM stack. This module has been written
 * carefully against its documented API but has NOT been exercised against
 * a real call — see the CLAUDE.md gotcha for this feature before assuming
 * it works. If it doesn't, the fallback is: use place_announcement_call's
 * failure message (which surfaces the real error) to see what broke.
 */

import { execFile } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const VOICE_AUTH_DIR = process.env.VOICE_AUTH_DIR || "./voice-auth";
const PIPER_BIN = process.env.PIPER_BIN || "piper";
const PIPER_VOICE_MODEL = process.env.PIPER_VOICE_MODEL || "";
const CALL_MAX_MS = Math.max(10000, parseInt(process.env.VOICE_CALL_MAX_MS || "45000", 10));
const PIPER_TIMEOUT_MS = 20000;

let voipClient = null;   // lazy singleton, created on first real call
let voipReady = false;

function nodeMajor() {
  return parseInt(process.versions.node.split(".")[0], 10);
}

/** Synthesize `text` to a temp WAV file via Piper. Throws with a clear
 * message on any failure (missing binary, missing model, bad text). */
function synthesize(text) {
  return new Promise((resolve, reject) => {
    if (!PIPER_VOICE_MODEL) {
      reject(new Error("PIPER_VOICE_MODEL not set — voice calling needs a Piper .onnx model"));
      return;
    }
    const outFile = path.join(os.tmpdir(), `lumina-voice-${Date.now()}-${Math.random().toString(36).slice(2)}.wav`);
    const proc = execFile(
      PIPER_BIN,
      ["--model", PIPER_VOICE_MODEL, "--output_file", outFile],
      { timeout: PIPER_TIMEOUT_MS },
      (err) => {
        if (err) {
          reject(new Error(`piper TTS failed: ${err.message}`));
          return;
        }
        if (!fs.existsSync(outFile)) {
          reject(new Error("piper ran but produced no audio file"));
          return;
        }
        resolve(outFile);
      }
    );
    proc.stdin.write(text);
    proc.stdin.end();
    proc.on("error", (e) => reject(new Error(`couldn't start piper (${PIPER_BIN}): ${e.message}`)));
  });
}

/** Lazily create + connect the VoipClient. Its own QR pairing prints to
 * this same console on first run, same as the text bridge's own QR. */
async function ensureVoipClient() {
  if (voipClient && voipReady) return voipClient;
  if (nodeMajor() < 20) {
    throw new Error(`voice calling needs Node >=20 (this process is running ${process.versions.node})`);
  }
  let VoipClient;
  try {
    ({ VoipClient } = await import("baileys-caller"));
  } catch (e) {
    throw new Error(
      `baileys-caller isn't installed/built (${e.message}). ` +
      `Run "npm install" in whatsapp-bridge, and if it doesn't auto-build, ` +
      `see its README for the manual "npm run build" step.`
    );
  }
  try {
    fs.mkdirSync(VOICE_AUTH_DIR, { recursive: true });
  } catch { /* ignore */ }
  voipClient = new VoipClient({ authDir: VOICE_AUTH_DIR });
  await voipClient.connect();
  voipReady = true;
  return voipClient;
}

/**
 * Place a one-way announcement call: ring `toDigits`, speak `text`, hang
 * up. Returns { ok: true } or { ok: false, error }. Never throws — every
 * failure mode (Node too old, lib not installed, ffmpeg missing, Piper
 * missing/failed, the call itself failing) is caught and reported so the
 * caller (index.js's /call route) can relay a real reason back to Lumina,
 * which relays it back to whoever asked over WhatsApp text.
 */
export async function placeAnnouncementCall(toDigits, text) {
  let wavPath = null;
  try {
    const client = await ensureVoipClient();
    wavPath = await synthesize(text);
    const call = await client.call(toDigits, { audioSource: wavPath, durationMs: CALL_MAX_MS });
    await Promise.race([
      call.waitForEnd(),
      new Promise((resolve) => setTimeout(() => {
        try { call.end(); } catch { /* ignore */ }
        resolve();
      }, CALL_MAX_MS + 5000)),
    ]);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  } finally {
    if (wavPath) {
      try { fs.unlinkSync(wavPath); } catch { /* ignore */ }
    }
  }
}
