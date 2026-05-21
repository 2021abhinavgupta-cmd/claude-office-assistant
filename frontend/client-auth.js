/**
 * client-auth.js
 * Protects client portal pages by verifying the client session with the backend.
 * Must be loaded AFTER shared-config.js
 */

async function verifyClientSession() {
  const clientInfoStr = localStorage.getItem("claude_office_client");
  if (!clientInfoStr) {
    window.location.href = "login.html";
    return null;
  }
  
  try {
    const res = await fetch(`${API}/api/auth/client_verify`, {
      method: "GET",
      headers: { "Content-Type": "application/json" }
    });
    
    if (res.status === 401) {
      localStorage.removeItem("claude_office_client");
      window.location.href = "login.html";
      return null;
    }
    
    const data = await res.json();
    if (!data.valid) {
      localStorage.removeItem("claude_office_client");
      window.location.href = "login.html";
      return null;
    }
    
    return data;
  } catch (err) {
    console.error("Auth verify failed:", err);
    return null;
  }
}

async function clientLogout() {
  try {
    await fetch(`${API}/api/auth/client_logout`, { method: "POST" });
  } catch (err) {
    console.error(err);
  }
  localStorage.removeItem("claude_office_client");
  window.location.href = "login.html";
}

// Run immediately
verifyClientSession().then(client => {
  if(client && window.onClientAuthenticated) {
    window.onClientAuthenticated(client);
  }
});
