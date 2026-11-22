import React, { useEffect, useState } from "react";
import { api, fmtTs, go, inr } from "../api.js";
import { StatusPill } from "../components/Bits.jsx";

export default function AdminPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const load = () => api("/api/admin/overview").then(setD).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);

  if (err) return <div className="alert error">{err}</div>;
  if (!d) return <div className="muted center">Loading…</div>;

  return (
    <div className="stack-lg">
      <section className="card">
        <h2>Admin</h2>
        <p className="muted">You have unlimited credits. Everyone else's balance comes only from what they pay through Stripe, or from credits you grant here.</p>
        <div className="stat-row">
          <div className="stat"><div className="v">{d.totals.users}</div><div className="k">Accounts</div></div>
          <div className="stat"><div className="v">{inr(d.totals.paid_amount)}</div><div className="k">Paid (Stripe test)</div></div>
          <div className="stat"><div className="v">{d.totals.credits_outstanding}</div><div className="k">User credits outstanding</div></div>
          <div className="stat"><div className="v">{d.jobs.length}</div><div className="k">Jobs</div></div>
        </div>
      </section>

      <section className="card">
        <h3>Users</h3>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Account</th><th>Sign-in</th><th className="num">Paid</th><th className="num">Jobs</th><th className="num">Credits</th><th>Adjust credits</th></tr></thead>
            <tbody>{d.users.map((u) => <UserRow key={u.id} u={u} onDone={load} />)}</tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h3>Payments</h3>
        {d.payments.length === 0 ? <p className="muted">No payments yet.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Date</th><th>Account</th><th>Pack</th><th className="num">Credits</th><th className="num">Amount</th><th>Via</th><th>Stripe session</th></tr></thead>
              <tbody>{d.payments.map((p) => (
                <tr key={p.stripe_session_id}>
                  <td className="nowrap">{fmtTs(p.ts)}</td><td>{p.email}</td><td>{p.pack}</td>
                  <td className="num">{p.credits}</td><td className="num">{inr(p.amount || 0)}</td><td>{p.via}</td>
                  <td className="mono">{p.stripe_session_id}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h3>All jobs</h3>
        {d.jobs.length === 0 ? <p className="muted">No jobs yet.</p> : (
          <table className="table clickable">
            <thead><tr><th>Need</th><th>Owner</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>{d.jobs.map((j) => (
              <tr key={j.id} onClick={() => go(`/jobs/${j.id}`)}>
                <td><a href={`#/jobs/${j.id}`}>{j.need}</a></td><td>{j.email}</td>
                <td><StatusPill status={j.status} /></td><td className="nowrap">{fmtTs(j.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function UserRow({ u, onDone }) {
  const [delta, setDelta] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      await api(`/api/admin/users/${u.id}/credits`, { method: "POST", body: { delta: Number(delta) } });
      setDelta("");
      onDone();
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <tr>
      <td>{u.email}{u.is_admin && <span className="pill info" style={{ marginLeft: 6 }}>Admin</span>}<div className="muted small-text">joined {fmtTs(u.created_at)}</div></td>
      <td>{[u.google && "Google", u.has_password && "Password"].filter(Boolean).join(", ") || "—"}</td>
      <td className="num">{inr(u.paid_amount || 0)}</td>
      <td className="num">{u.jobs}</td>
      <td className="num"><b>{u.is_admin ? "∞" : u.credits}</b></td>
      <td>
        {u.is_admin ? <span className="muted small-text">unlimited</span> : (
          <form onSubmit={submit} className="row gap">
            <input type="number" value={delta} onChange={(e) => setDelta(e.target.value)} placeholder="+5 or -2" style={{ width: 100 }} required />
            <button className="small" disabled={busy || !delta || Number(delta) === 0}>Apply</button>
            {err && <span className="bad-text small-text">{err}</span>}
          </form>
        )}
      </td>
    </tr>
  );
}
