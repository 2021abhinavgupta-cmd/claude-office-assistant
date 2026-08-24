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

// Hide immediately to prevent flash of protected content while auth check is in flight
document.documentElement.style.visibility = 'hidden';

(async function authGuard() {
  const authApi = getAuthApiBase();

  try {
    const res = await fetch(`${authApi}/api/auth/verify`, {
      credentials: "include"
    });
    const data = await res.json();

    if (!data.valid) {
      localStorage.removeItem("agency_portal_user");
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
    localStorage.setItem("agency_portal_user", JSON.stringify(user));
    window.__currentUser = user;
    // Auth passed — reveal the page
    document.documentElement.style.visibility = 'visible';

    // Presence heartbeat: immediate + every 60s. Real checkout is inferred
    // server-side from staleness of this timestamp (see task_scheduler.py's
    // attendance_presence_sweep job) -- NOT from a pagehide/beforeunload
    // event. An unload-event approach was tried once (see CLAUDE.md gotcha
    // #70/#71) and reverted: pagehide fires identically on a real tab close
    // and on clicking a link to another Lumina page (separate .html files,
    // not an SPA), so it wrote a checkout on every internal navigation --
    // the resulting write volume was implicated in a production DB hang.
    // Heartbeat-only avoids firing anything on navigation at all.
    sendAttendancePing(authApi, user.user_id);
    setInterval(() => sendAttendancePing(authApi, user.user_id), 60000);

  } catch (e) {
    // Network error — reveal page then redirect to login
    document.documentElement.style.visibility = 'visible';
    console.warn("[auth.js] Network error during verify — redirecting to login:", e.message);
    window.location.href = "login.html?error=network";
  }
})();

function sendAttendancePing(authApi, userId) {
  fetch(`${authApi}/api/attendance/ping`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  }).catch(() => {});
}

/**
 * Logout helper — call window.authLogout() from any page.
 */
window.authLogout = async function () {
  const authApi = getAuthApiBase();
  const token = sessionStorage.getItem("_session_token") || localStorage.getItem("_session_token");
  const user = JSON.parse(localStorage.getItem("agency_portal_user") || "{}");
  if (user.user_id) {
    await fetch(`${authApi}/api/attendance/checkout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: user.user_id }),
      keepalive: true,
    }).catch(() => {});
  }
    await fetch(`${authApi}/api/auth/logout`, {
      method: "POST",
      headers: { 
        "Content-Type": "application/json"
      },
      credentials: "include",
      body: JSON.stringify({}),
    }).catch(() => {});
  sessionStorage.removeItem("_session_token");
  localStorage.removeItem("agency_portal_user");
  window.location.href = "login.html";
};
