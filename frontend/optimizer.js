// ══════════════════════════════════════════════════════════════════════════════
// ⚡ PROMPT OPTIMIZER — Split Preview Panel (Option B)
// Loaded after app.js via <script src="optimizer.js">
// ══════════════════════════════════════════════════════════════════════════════
(function initPromptOptimizer() {
  const optimizeBtn    = document.getElementById("optimize-btn");
  const optBackdrop    = document.getElementById("opt-backdrop");
  const optPanel       = document.getElementById("opt-panel");
  const optOriginalEl  = document.getElementById("opt-original");
  const optOptimizedEl = document.getElementById("opt-optimized");
  const optLoadingEl   = document.getElementById("opt-loading");
  const optCostDisplay = document.getElementById("opt-cost-display");
  const optDiffHint    = document.getElementById("opt-diff-hint");
  const optCloseBtn    = document.getElementById("opt-close-btn");
  const optKeepBtn     = document.getElementById("opt-keep-btn");
  const optUseBtn      = document.getElementById("opt-use-btn");
  const inputEl        = document.getElementById("msg-input");

  if (!optimizeBtn || !optPanel) return;

  // API base (re-use same detection as app.js)
  const _API = (() => {
    const h = window.location.hostname;
    if (!h || h === "localhost" || h === "127.0.0.1") return "http://localhost:5000";
    return window.location.origin;
  })();

  // ── Panel helpers ─────────────────────────────────────────────────────────
  function openPanel() {
    optBackdrop.classList.remove("hidden");
    optPanel.classList.remove("hidden");
    void optPanel.offsetWidth; // retrigger animation
  }

  function closePanel() {
    optBackdrop.classList.add("hidden");
    optPanel.classList.add("hidden");
    optimizeBtn.classList.remove("loading");
    optimizeBtn.disabled = !inputEl?.value?.trim();
  }

  function countWords(str) {
    return str.trim().split(/\s+/).filter(Boolean).length;
  }

  function showDiffHint(original, optimized) {
    const a = countWords(original), b = countWords(optimized);
    const d = b - a;
    const col = d > 0 ? "var(--accent)" : "var(--haiku-color)";
    optDiffHint.innerHTML =
      `${a} words &rarr; <strong style="color:${col}">${b} words (${d > 0 ? "+" : ""}${d})</strong>`;
  }

  function toast(msg, type) {
    if (typeof showToast === "function") showToast(msg, type);
  }

  // ── ⚡ Click → call API ──────────────────────────────────────────────────
  optimizeBtn.addEventListener("click", async () => {
    const prompt = inputEl?.value?.trim();
    if (!prompt || prompt.length < 5) {
      toast("Type a longer message first to optimize", "info");
      return;
    }

    // Reset panel state
    optOriginalEl.textContent  = prompt;
    optOptimizedEl.value       = "";
    optCostDisplay.textContent = "";
    optCostDisplay.classList.remove("visible");
    optDiffHint.innerHTML      = "";
    optLoadingEl.classList.remove("hidden");
    optUseBtn.disabled         = true;
    optimizeBtn.classList.add("loading");
    optimizeBtn.disabled       = true;
    openPanel();

    try {
      const res  = await fetch(`${_API}/api/optimize-prompt`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ prompt }),
      });
      const data = await res.json();

      if (!res.ok || data.error) {
        closePanel();
        toast("❌ " + (data.error || "Optimization failed"), "error");
        return;
      }

      // Populate results
      optOptimizedEl.value = data.optimized;
      showDiffHint(data.original, data.optimized);

      if (data.cost_usd != null) {
        optCostDisplay.textContent = "⚡ $" + data.cost_usd.toFixed(6);
        optCostDisplay.classList.add("visible");
      }

    } catch (_) {
      closePanel();
      toast("⚠️ Could not connect to server", "error");
    } finally {
      optLoadingEl.classList.add("hidden");
      optUseBtn.disabled = false;
      optimizeBtn.classList.remove("loading");
      optimizeBtn.disabled = !inputEl?.value?.trim();
    }
  });

  // ── Accept: paste optimized into input, close panel ──────────────────────
  optUseBtn.addEventListener("click", () => {
    const optimized = optOptimizedEl.value.trim();
    if (inputEl && optimized) {
      inputEl.value = optimized;
      inputEl.dispatchEvent(new Event("input")); // trigger resize + enable send
      inputEl.focus();
    }
    closePanel();
    toast("✓ Optimized prompt applied — press Enter to send", "success");
  });

  // ── Reject / Close ────────────────────────────────────────────────────────
  optKeepBtn.addEventListener("click",  closePanel);
  optCloseBtn.addEventListener("click", closePanel);
  optBackdrop.addEventListener("click", closePanel);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !optPanel.classList.contains("hidden")) closePanel();
  });

  // Keep ⚡ disabled while input is empty
  if (inputEl) {
    inputEl.addEventListener("input", () => {
      if (!optimizeBtn.classList.contains("loading"))
        optimizeBtn.disabled = !inputEl.value.trim();
    });
    optimizeBtn.disabled = !inputEl.value.trim();
  }
})();
