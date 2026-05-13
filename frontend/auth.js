/**
 * auth.js — Shared auth guard. Include in every protected page.
 * Usage: <script src="auth.js"></script>
 * The script auto-runs on load and redirects to login.html if session is invalid.
 */
function getAuthApiBase() {
  return location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : location.origin;
}

(async function authGuard() {
  const authApi = getAuthApiBase();

  // sessionStorage is cleared when the tab is closed — auto-logout on tab close
  const token = sessionStorage.getItem("_session_token");

  // If no token at all → go to login
  if (!token) {
    window.location.href = "login.html";
    return;
  }

  try {
    const res = await fetch(`${authApi}/api/auth/verify`, {
      headers: { "Authorization": `Bearer ${token}` }
    });
    const data = await res.json();

    if (!data.valid) {
      sessionStorage.removeItem("_session_token");
      localStorage.removeItem("claude_office_user");
      window.location.href = "login.html";
      return;
    }

    // Refresh stored user data from server truth
    const user = {
      user_id:  data.user_id,
      name:     data.name,
      role:     data.role,
      is_admin: data.is_admin,
    };
    localStorage.setItem("claude_office_user", JSON.stringify(user));
    window.__currentUser = user;

  } catch (e) {
    // Network error — redirect to login, don't allow through
    console.warn("[auth.js] Network error during verify — redirecting to login:", e.message);
    window.location.href = "login.html?error=network";
  }
})();

/**
 * beforeunload fires on tab close AND on same-origin navigation / refresh.
 * We must NOT record checkout when the user is still using the app (only changing pages).
 * Skip checkout briefly after: same-origin link click, same-origin form submit, refresh shortcuts.
 */
(function setupAttendanceUnloadGuard() {
  const SKIP_KEY = "_attendance_skip_unload_checkout_ts";
  const WINDOW_MS = 4000;

  function markInternalNavigation() {
    sessionStorage.setItem(SKIP_KEY, String(Date.now()));
  }

  document.addEventListener(
    "click",
    function (e) {
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a || a.target === "_blank") return;
      try {
        const u = new URL(a.href, location.href);
        if (u.origin === location.origin) markInternalNavigation();
      } catch (_) {}
    },
    true
  );

  document.addEventListener(
    "submit",
    function (e) {
      const f = e.target;
      if (!f || f.tagName !== "FORM") return;
      try {
        const action = f.getAttribute("action");
        if (!action || action.startsWith("#")) return;
        const u = new URL(action, location.href);
        if (u.origin === location.origin) markInternalNavigation();
      } catch (_) {}
    },
    true
  );

  window.addEventListener("keydown", function (e) {
    if (e.key === "F5" || ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "r")) {
      markInternalNavigation();
    }
    // Browser Back/Forward (keyboard) — still same session, not leaving work
    if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      markInternalNavigation();
    }
  });

  window.addEventListener("beforeunload", function () {
    const user = JSON.parse(localStorage.getItem("claude_office_user") || "{}");
    if (!user.user_id) return;
    const ts = Number(sessionStorage.getItem(SKIP_KEY) || 0);
    if (Date.now() - ts < WINDOW_MS) return;

    const authApi = getAuthApiBase();
    navigator.sendBeacon(
      `${authApi}/api/attendance/checkout`,
      JSON.stringify({ user_id: user.user_id })
    );
  });
})();

/**
 * Logout helper — call window.authLogout() from any page.
 */
window.authLogout = async function () {
  const authApi = getAuthApiBase();
  const token = sessionStorage.getItem("_session_token") || localStorage.getItem("_session_token");
  const user = JSON.parse(localStorage.getItem("claude_office_user") || "{}");
  if (user.user_id) {
    await fetch(`${authApi}/api/attendance/checkout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: user.user_id }),
      keepalive: true,
    }).catch(() => {});
  }
  if (token) {
    await fetch(`${authApi}/api/auth/logout`, {
      method: "POST",
      headers: { 
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`
      },
      body: JSON.stringify({ token }),
    }).catch(() => {});
  }
  sessionStorage.removeItem("_session_token");
  localStorage.removeItem("claude_office_user");
  window.location.href = "login.html";
};
