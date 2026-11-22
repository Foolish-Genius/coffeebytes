import React, { useState } from "react";
import { api } from "../api.js";

const ERRORS = {
  google: "Google sign-in was cancelled or failed. Try again or use email.",
  google_email: "Your Google account has no verified email.",
  google_off: "Google sign-in is not configured on this server yet.",
};

export default function AuthPage({ cfg, params, onAuthed }) {
  const [mode, setMode] = useState("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [err, setErr] = useState(ERRORS[params.get("error")] || "");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const u = await api(mode === "signup" ? "/api/auth/signup" : "/api/auth/login", {
        method: "POST",
        body: { email, password, name },
      });
      onAuthed(u);
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-wrap">
      <section className="auth-pitch">
        <div className="brand big"><span className="logo">S</span> SiliconScout <span className="ver">3.0</span></div>
        <h1>Source AIS-140 trackers you can actually defend.</h1>
        <p>
          Directories tell you who says they make trackers. SiliconScout tells you whose model holds a government
          approval, who actually makes it, and drafts the RFQs for your sign-off.
        </p>
        <ul className="ticks">
          <li>Plain need in, spec and assumptions out</li>
          <li>Every gate shows its source and the verbatim passage</li>
          <li>Nothing is sent until a person approves it</li>
        </ul>
      </section>
      <section className="card auth-card">
        <div className="tabs">
          <button className={mode === "signin" ? "on" : ""} onClick={() => setMode("signin")}>Sign in</button>
          <button className={mode === "signup" ? "on" : ""} onClick={() => setMode("signup")}>Create account</button>
        </div>
        {cfg.google_enabled && (
          <>
            <a className="btn google" href="/api/auth/google/start">
              <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.4 30.2 0 24 0 14.6 0 6.6 5.4 2.6 13.2l7.8 6.1C12.3 13.6 17.6 9.5 24 9.5z"/><path fill="#4285F4" d="M46.1 24.6c0-1.6-.1-3.1-.4-4.6H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.8c4.3-4 6.9-9.9 6.9-17.1z"/><path fill="#FBBC05" d="M10.4 28.7c-.5-1.4-.8-3-.8-4.7s.3-3.2.8-4.7l-7.8-6.1C1 16.5 0 20.1 0 24s1 7.5 2.6 10.8l7.8-6.1z"/><path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.8c-2.1 1.4-4.8 2.3-8.5 2.3-6.4 0-11.7-4.1-13.6-9.8l-7.8 6.1C6.6 42.6 14.6 48 24 48z"/></svg>
              Continue with Google
            </a>
            <div className="divider"><span>or with email</span></div>
          </>
        )}
        <form onSubmit={submit} className="stack">
          {mode === "signup" && (
            <label>Name <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></label>
          )}
          <label>Work email <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" /></label>
          <label>
            Password
            <input type="password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "signup" ? "new-password" : "current-password"} />
            {mode === "signup" && <small className="muted">At least 8 characters.</small>}
          </label>
          {err && <div className="alert error">{err}</div>}
          <button className="primary" disabled={busy}>
            {busy ? "Please wait…" : mode === "signup" ? "Create account" : "Sign in"}
          </button>
        </form>
        <p className="muted small-text">
          Each sourcing job costs {cfg.credits_per_job} credit. Credits are bought on the Credits page (Stripe test mode).
        </p>
      </section>
    </div>
  );
}
