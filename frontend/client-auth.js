/**
 * client-auth.js
 * Protects client portal pages by verifying the client session with the backend.
 * Must be loaded AFTER shared-config.js
 */
var API = window.API || (location.hostname === "localhost" || location.hostname === "127.0.0.1" ? "http://localhost:5000" : location.origin);

async function verifyClientSession() {
  const clientInfoStr = localStorage.getItem("agency_portal_client");
  if (!clientInfoStr) {
    window.location.href = "client-login.html";
    return null;
  }
  
  try {
    const res = await fetch(`${API}/api/auth/client_verify`, {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      credentials: "include"
    });
    
    if (res.status === 401) {
      localStorage.removeItem("agency_portal_client");
      window.location.href = "client-login.html";
      return null;
    }
    
    const data = await res.json();
    if (!data.valid) {
      localStorage.removeItem("agency_portal_client");
      window.location.href = "client-login.html";
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
    await fetch(`${API}/api/auth/client_logout`, { method: "POST", credentials: "include" });
  } catch (err) {
    console.error(err);
  }
  localStorage.removeItem("agency_portal_client");
  window.location.href = "client-login.html";
}

// Run immediately
verifyClientSession().then(client => {
  if(client && window.onClientAuthenticated) {
    window.onClientAuthenticated(client);
  }
});
