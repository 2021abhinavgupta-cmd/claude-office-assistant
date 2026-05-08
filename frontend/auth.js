/**
 * auth.js — Shared auth guard. Include in every protected page.
 * Usage: <script src="auth.js"></script>
 * The script auto-runs on load and redirects to login.html if session is invalid.
 */
(async function authGuard() {
  const API = location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : location.origin;

  const token = localStorage.getItem("_session_token");

  // If no token at all → go to login
  if (!token) {
    window.location.href = "login.html";
    return;
  }

  try {
    const res = await fetch(`${API}/api/auth/verify?token=${encodeURIComponent(token)}`);
    const data = await res.json();

    if (!data.valid) {
      localStorage.removeItem("_session_token");
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
    // Network error — allow through (offline graceful degradation)
    console.warn("[auth.js] Network error during verify — allowing through:", e.message);
  }
})();

/**
 * Logout helper — call window.authLogout() from any page.
 */
window.authLogout = async function () {
  const API = location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : location.origin;
  const token = localStorage.getItem("_session_token");
  if (token) {
    await fetch(`${API}/api/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }).catch(() => {});
  }
  localStorage.removeItem("_session_token");
  localStorage.removeItem("claude_office_user");
  window.location.href = "login.html";
};
