// ── Text Formatting (Markdown & Highlight.js) ──────────────────────────────
if (typeof marked !== "undefined") {
  const renderer = new marked.Renderer();
  renderer.code = function(code, language, isEscaped) {
    let textStr = code;
    let langStr = language;
    // Support Marked v10+ where the first argument is a token object
    if (typeof code === 'object' && code !== null) {
      textStr = code.text;
      langStr = code.lang;
    }
    
    const ext = String(langStr || "txt").toLowerCase();
    const cleanCode = (textStr || "").trim();
    const encodedCode = cleanCode.length < 500000 ? encodeURIComponent(cleanCode) : encodeURIComponent(cleanCode.slice(0, 500000));
    
    let highlighted = cleanCode;
    const hlId = (langStr || ext || "").toLowerCase();
    if (hlId && hljs.getLanguage(hlId)) {
      highlighted = hljs.highlight(cleanCode, { language: hlId }).value;
    } else {
      highlighted = escHtml(cleanCode);
    }

    let previewBtn = '';
    if (ext === 'html' || ext === 'svg') {
      previewBtn = `<button onclick="previewArtifact(this, '${ext}')" data-code="${encodedCode}" style="background:none; border:none; color:var(--accent); cursor:pointer; font-size:0.75rem; display:flex; align-items:center; gap:4px; opacity:0.9;"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg> Preview</button>`;
    }

    const headerHtml = `
      <div class="code-header" style="display:flex; justify-content:space-between; background:var(--surface2); padding:6px 12px; border-radius:8px 8px 0 0; font-size:0.75rem; color:var(--muted); border:1px solid var(--border); border-bottom:none;">
        <span style="font-family:JetBrains Mono, monospace; text-transform:uppercase">${ext}</span>
        <div style="display:flex; gap:12px;">
          ${previewBtn}
          <button onclick="downloadCode(this, '${ext}')" data-code="${encodedCode}" style="background:none; border:none; color:var(--text); cursor:pointer; font-size:0.75rem; display:flex; align-items:center; gap:4px; opacity:0.8; transition:opacity 0.2s;" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.8'">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg> Download
          </button>
        </div>
      </div>`;

    const safeHlClass = hlId || "plain";
    return `<div class="code-block-wrapper" style="margin: 16px 0;">${headerHtml}<pre style="margin-top:0; border-top-left-radius:0; border-top-right-radius:0; border:1px solid var(--border); padding:16px; overflow-x:auto; background:#282c34;"><code class="hljs language-${safeHlClass}">${highlighted}</code></pre></div>`;
  };
  marked.setOptions({
    renderer: renderer,
    breaks: true,
    gfm: true,
    headerIds: false,
    mangle: false,
  });
}

function formatText(text) {
  if (typeof marked !== "undefined") {
    // Fix unclosed code blocks during streaming
    const codeBlocks = (text.match(/```/g) || []).length;
    if (codeBlocks % 2 !== 0) {
      text += "\n```"; 
    }
    // System-like small touch: avoid rendering empty output as "null"/"undefined"
    const out = marked.parse(String(text || ""));
    return out || "";
  }
  return `<p>${escHtml(text).replace(/\n/g, "<br>")}</p>`;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function extractFirstHtmlFence(text) {
  if (!text || typeof text !== "string") return null;
  const m = text.match(/```html\s*([\s\S]*?)```/i);
  return m ? m[1].trim() : null;
}

function showArtifactSidePreview(ext, code) {
  const pane = document.getElementById("artifacts-pane");
  const iframe = document.getElementById("artifact-iframe");
  const mainEl = document.getElementById("main");
  const artTitle = document.getElementById("artifact-title");
  if (!pane || !iframe || !mainEl) return;

  const c = String(code || "");
  window._currentArtifactCode = c;
  window._currentArtifactExt = ext;

  let doc = "";
  if (ext === "html") {
    const t = c.trim();
    doc = /<\s*html[\s>]/i.test(t)
      ? t
      : `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>${t}</body></html>`;
  } else if (ext === "svg") {
    doc = `<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0;display:flex;min-height:100dvh;align-items:center;justify-content:center;background:#f4f4f5;}</style></head><body>${c}</body></html>`;
  } else {
    doc = `<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font-family:JetBrains Mono,ui-monospace,monospace;white-space:pre-wrap;padding:16px;margin:0;background:#fff;color:#111;}</style></head><body>${escHtml(c)}</body></html>`;
  }

  iframe.srcdoc = doc;
  if (artTitle) {
    artTitle.textContent =
      ext === "html" ? "HTML preview" :
      ext === "svg" ? "SVG preview" :
      `${ext} preview`;
  }

  // Reset to Preview tab whenever a new artifact loads
  const previewPanel = document.getElementById("artifact-preview-panel");
  const codePanel    = document.getElementById("artifact-code-panel");
  const tabPreview   = document.getElementById("tab-preview");
  const tabCode      = document.getElementById("tab-code");
  if (previewPanel) previewPanel.style.display = "flex";
  if (codePanel)    codePanel.style.display    = "none";
  if (tabPreview)   tabPreview.classList.add("active");
  if (tabCode)      tabCode.classList.remove("active");

  mainEl.classList.add("main--artifact-open");
  pane.classList.add("artifacts-pane--open");
}

window.previewArtifact = function(btn, ext) {
  const raw = btn.getAttribute("data-code") || btn.dataset.code || "";
  let code = "";
  try {
    code = decodeURIComponent(raw);
  } catch (_) {
    code = raw;
  }
  showArtifactSidePreview(ext, code);
};

window.openArtifactSidePreviewHtml = function(html) {
  showArtifactSidePreview("html", html || "");
};

window.closeArtifact = function() {
  const pane = document.getElementById("artifacts-pane");
  const mainEl = document.getElementById("main");
  const iframe = document.getElementById("artifact-iframe");
  if (pane) pane.classList.remove("artifacts-pane--open");
  if (mainEl) mainEl.classList.remove("main--artifact-open");
  if (iframe) {
    try { iframe.srcdoc = ""; } catch (_) {}
  }
};

window.switchArtifactTab = function(tab) {
  const previewPanel = document.getElementById("artifact-preview-panel");
  const codePanel    = document.getElementById("artifact-code-panel");
  const codeView     = document.getElementById("artifact-code-view");
  const tabPreview   = document.getElementById("tab-preview");
  const tabCode      = document.getElementById("tab-code");
  if (!previewPanel || !codePanel) return;
  const isPreview = tab === "preview";
  previewPanel.style.display = isPreview ? "flex" : "none";
  codePanel.style.display    = isPreview ? "none" : "flex";
  if (tabPreview) tabPreview.classList.toggle("active", isPreview);
  if (tabCode)    tabCode.classList.toggle("active", !isPreview);
  if (!isPreview && codeView && window._currentArtifactCode) {
    const lang = window._currentArtifactExt === "svg" ? "xml" : "html";
    try {
      codeView.innerHTML = hljs.highlight(window._currentArtifactCode, { language: lang }).value;
    } catch (_) {
      codeView.textContent = window._currentArtifactCode;
    }
  }
};

window.copyArtifactCode = function() {
  if (!window._currentArtifactCode) return;
  navigator.clipboard.writeText(window._currentArtifactCode)
    .then(() => showToast("Copied to clipboard", "success"))
    .catch(() => showToast("Copy failed", "error"));
};

function maybeAddSidePreviewButton(actionsEl, rawText) {
  if (!actionsEl || !rawText) return;
  if (actionsEl.querySelector(".side-preview-btn")) return;
  const htmlFence = extractFirstHtmlFence(rawText);
  if (!htmlFence || htmlFence.length < 12) return;
  const prevBtn = document.createElement("button");
  prevBtn.type = "button";
  prevBtn.className = "msg-action-btn side-preview-btn";
  prevBtn.textContent = "Side preview";
  prevBtn.title = "Open the first HTML code block in the side panel";
  prevBtn.addEventListener("click", () => showArtifactSidePreview("html", htmlFence));
  const regen = actionsEl.querySelector(".regen-btn");
  if (regen) actionsEl.insertBefore(prevBtn, regen);
  else actionsEl.appendChild(prevBtn);
}

