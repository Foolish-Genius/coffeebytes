import React, { useCallback, useEffect, useState } from "react";
import { api, go } from "./api.js";
import AuthPage from "./pages/AuthPage.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import JobPage from "./pages/JobPage.jsx";
import BillingPage from "./pages/BillingPage.jsx";
import AdminPage from "./pages/AdminPage.jsx";

// Hash routing keeps the backend a plain static mount: #/, #/auth, #/jobs/<id>, #/billing?session_id=...
function parseHash() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  return { path, params: new URLSearchParams(qs || "") };
}

export default function App() {
  const [route, setRoute] = useState(parseHash());
  const [user, setUser] = useState(undefined); // undefined = loading, null = signed out
  const [cfg, setCfg] = useState(null);

  const refreshUser = useCallback(async () => {
    try {
      setUser(await api("/api/me"));
    } catch {
      setUser(null);
    }
  }, []);

  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener("hashchange", on);
    api("/api/config").then(setCfg).catch(() => setCfg({}));
    refreshUser();
    return () => window.removeEventListener("hashchange", on);
  }, [refreshUser]);

  if (user === undefined || cfg === null) return <div className="center muted">Loading…</div>;

  if (!user) return <AuthPage cfg={cfg} params={route.params} onAuthed={(u) => { setUser(u); go("/"); }} />;

  const logout = async () => {
    await api("/api/auth/logout", { method: "POST" });
    setUser(null);
    go("/auth");
  };

  let page;
  const m = route.path.match(/^\/jobs\/([\w-]+)/);
  if (m) page = <JobPage key={m[1]} jobId={m[1]} cfg={cfg} refreshUser={refreshUser} />;
  else if (route.path.startsWith("/admin") && user.is_admin) page = <AdminPage />;
  else if (route.path.startsWith("/billing")) page = <BillingPage cfg={cfg} params={route.params} user={user} refreshUser={refreshUser} />;
  else page = <Dashboard cfg={cfg} user={user} refreshUser={refreshUser} />;

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/">
          <span className="logo">S</span> SiliconScout <span className="ver">3.0</span>
        </a>
        <nav>
          <a href="#/" className={route.path === "/" ? "active" : ""}>Jobs</a>
          <a href="#/billing" className={route.path.startsWith("/billing") ? "active" : ""}>Credits</a>
          {user.is_admin && <a href="#/admin" className={route.path.startsWith("/admin") ? "active" : ""}>Admin</a>}
        </nav>
        <div className="userbox">
          {user.is_admin ? (
            <span className="balance admin" title="Admin: jobs are never debited">Admin · ∞ credits</span>
          ) : (
            <a className="balance" href="#/billing" title="Credit balance">
              <b>{user.credits}</b> credit{user.credits === 1 ? "" : "s"}
            </a>
          )}
          <span className="muted email">{user.email}</span>
          <button className="ghost small" onClick={logout}>Sign out</button>
        </div>
      </header>
      <main className="content">{page}</main>
    </div>
  );
}
