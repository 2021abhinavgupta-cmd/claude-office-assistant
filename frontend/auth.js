/**
 * auth.js — Shared auth guard. Include in every protected page.
 * Usage: <script src="auth.js"></script>
 * The script auto-runs on load and redirects to login.html if session is invalid.
 */
(async function authGuard() {
  const API = location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : location.origin;

  // sessionStorage is cleared when the tab is closed — auto-logout on tab close
  const token = sessionStorage.getItem("_session_token");

  // If no token at all → go to login
  if (!token) {
    window.location.href = "login.html";
    return;
  }

  try {
    const res = await fetch(`${API}/api/auth/verify`, {
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

const API = location.hostname === "localhost" || location.hostname === "127.0.0.1"
  ? "http://localhost:5000"
  : location.origin;

window.addEventListener("beforeunload", function() {
  const user = JSON.parse(localStorage.getItem("claude_office_user") || "{}");
  if (!user.user_id) return;
  navigator.sendBeacon(
    `${API}/api/attendance/checkout`,
    JSON.stringify({ user_id: user.user_id })
  );
});

/**
 * Logout helper — call window.authLogout() from any page.
 */
window.authLogout = async function () {
  const token = sessionStorage.getItem("_session_token") || localStorage.getItem("_session_token");
  const user = JSON.parse(localStorage.getItem("claude_office_user") || "{}");
  if (user.user_id) {
    await fetch(`${API}/api/attendance/checkout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: user.user_id }),
      keepalive: true,
    }).catch(() => {});
  }
  if (token) {
    await fetch(`${API}/api/auth/logout`, {
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
