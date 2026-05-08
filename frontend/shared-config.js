// shared-config.js — one source of truth for employee data
window.EMPLOYEES = null;
window.EMP_DICT = null; // Helper for mapping ID -> Name

async function loadEmployees() {
    if (window.EMPLOYEES) return window.EMPLOYEES;
    const API = location.hostname === "localhost" || location.hostname === "127.0.0.1" 
        ? "http://localhost:5000" 
        : location.origin;
    
    try {
        const r = await fetch(`${API}/api/employees`);
        const data = await r.json();
        window.EMPLOYEES = data.employees || [];
        
        // Build dictionary for quick name lookups
        window.EMP_DICT = {};
        window.EMPLOYEES.forEach(e => {
            window.EMP_DICT[e.id] = e.name;
        });
        
        return window.EMPLOYEES;
    } catch (e) {
        console.error("Failed to load employees", e);
        return [];
    }
}
