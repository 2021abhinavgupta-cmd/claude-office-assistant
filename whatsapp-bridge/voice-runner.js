/**
 * voice-runner.js — places exactly one announcement call, then exits.
 *
 * Runs as a SEPARATE OS process from index.js, spawned per-call by the
 * bridge's /call route. This exists because of a real, confirmed incident
 * (2026-09-15): the bridge process crashed with an unusual native-looking
 * exit code (4294967295 / 0xFFFFFFFF) a few minutes after several failed
 * call-placement retries, with baileys-caller's WASM VoIP module printing
 * its own "Blocking on the main thread is very dangerous" emscripten
 * warning right before it happened. That crash took the ENTIRE bridge
 * down -- including the live, production WhatsApp text-messaging session
 * -- and the abrupt (non-graceful) process death is the leading suspect
 * for why several contacts' Signal sessions forked afterward ("Waiting
 * for this message"). Whether or not the WASM module was the exact
 * culprit, the lesson is structural: a young, single-maintainer,
 * WASM-based library placing real-time calls has no business sharing a
 * process with the production text bridge. If it crashes here, only this
 * short-lived process dies -- index.js and the live WhatsApp connection
 * are completely unaffected, and job_bridge's normal supervision doesn't
 * even need to know this happened.
 *
 * Contract: reads one line of JSON ({"to": "...", "message": "..."}) from
 * stdin, prints exactly one line of JSON ({"ok": bool, "error"?: string})
 * to stdout, then exits 0. The parent (index.js) treats ANY non-zero exit,
 * a timeout, or unparseable stdout as "the call failed, cleanly" -- it
 * never lets a crash here propagate as a crash of its own.
 */
import { placeAnnouncementCall } from "./voice.js";

process.on("uncaughtException", (e) => {
  try { process.stdout.write(JSON.stringify({ ok: false, error: `voice runner crashed: ${e && e.message ? e.message : e}` }) + "\n"); } catch { /* ignore */ }
  process.exit(1);
});
process.on("unhandledRejection", (e) => {
  try { process.stdout.write(JSON.stringify({ ok: false, error: `voice runner crashed: ${e && e.message ? e.message : e}` }) + "\n"); } catch { /* ignore */ }
  process.exit(1);
});

let input = "";
process.stdin.on("data", (c) => { input += c; });
process.stdin.on("end", async () => {
  let to, message;
  try {
    ({ to, message } = JSON.parse(input || "{}"));
  } catch {
    console.log(JSON.stringify({ ok: false, error: "bad input" }));
    process.exit(1);
  }
  try {
    const result = await placeAnnouncementCall(String(to), String(message));
    console.log(JSON.stringify(result));
    process.exit(result.ok ? 0 : 1);
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: String(e && e.message ? e.message : e) }));
    process.exit(1);
  }
});
