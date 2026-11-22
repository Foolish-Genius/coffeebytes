import React, { useEffect, useRef, useState } from "react";
import { api, fmtTs, inr } from "../api.js";
import { label } from "../components/Bits.jsx";

export default function BillingPage({ cfg, params, user, refreshUser }) {
  const [hist, setHist] = useState(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const confirmed = useRef(false);

  const load = () => api("/api/billing/history").then(setHist).catch((e) => setErr(e.message));

  useEffect(() => {
    const qs = new URLSearchParams(window.location.search);
    const sid = params.get("session_id") || qs.get("session_id");
    const cancelled = params.get("cancelled") || qs.get("cancelled");
    if (sid && !confirmed.current) {
      confirmed.current = true;
      setNote("Confirming your payment with Stripe…");
      api("/api/billing/confirm", { method: "POST", body: { session_id: sid } })
        .then((r) => setNote(r.granted ? "Payment received. Credits added." : r.message === "already credited" ? "Payment received. Credits added." : `Payment not completed: ${r.message}`))
        .catch((e) => setErr(e.message))
        .finally(() => { refreshUser(); load(); window.history.replaceState(null, "", "/#/billing"); });
    } else {
      if (cancelled) setNote("Checkout cancelled. No charge was made.");
      load();
      if (qs.toString()) window.history.replaceState(null, "", "/#/billing");
    }
  }, []);

  const buy = async (pack) => {
    setErr(""); setBusy(pack);
    try {
      const r = await api("/api/billing/checkout", { method: "POST", body: { pack } });
      window.location.href = r.url;
    } catch (e) {
      setErr(e.message);
      setBusy("");
    }
  };

  return (
    <div className="stack-lg">
      <section className="card">
        <div className="row between wrap">
          <div>
            <h2>Credits</h2>
            <p className="muted">One sourcing job costs {cfg.credits_per_job} credit. Approving RFQs and entering quotes are free.</p>
          </div>
          <div className="balance-big"><b>{user.is_admin ? "∞" : user.credits}</b><span>{user.is_admin ? "admin, unlimited" : "credits"}</span></div>
        </div>
        <div className="alert info">
          <b>Stripe test mode.</b> No real money moves. Pay with test card <span className="mono">4242 4242 4242 4242</span>, any future expiry, any CVC.
        </div>
        {!cfg.stripe_enabled && (
          <div className="alert warn">
            Payments are not configured on this server{cfg.stripe_live_key_rejected ? " (a live key was found and refused: test keys only)" : ""}.
            Add <span className="mono">STRIPE_SECRET_KEY=sk_test_…</span> and <span className="mono">STRIPE_WEBHOOK_SECRET=whsec_…</span> to <span className="mono">.env</span> and restart.
          </div>
        )}
        {user.is_admin && <div className="alert good">You are signed in as admin, so jobs are never charged. Use the Admin page to see users and payments or to adjust a user's credits.</div>}
        {note && <div className="alert good">{note}</div>}
        {err && <div className="alert error">{err}</div>}
        <div className="packs">
          {(cfg.packs || []).map((p) => (
            <div key={p.id} className={`pack ${p.id === "team" ? "featured" : ""}`}>
              {p.id === "team" && <span className="ribbon">Most teams</span>}
              <div className="pack-name">{p.name}</div>
              <div className="pack-credits"><b>{p.credits}</b> sourcing jobs</div>
              <div className="pack-price">{inr(p.amount)}</div>
              <div className="muted small-text">{inr(Math.round(p.amount / p.credits))} per job</div>
              <button className="primary" disabled={!cfg.stripe_enabled || !!busy || user.is_admin} onClick={() => buy(p.id)}>
                {busy === p.id ? "Opening Stripe…" : "Buy with Stripe"}
              </button>
            </div>
          ))}
        </div>
      </section>

      <section className="card">
        <h3>Credit history</h3>
        {!hist ? <p className="muted">Loading…</p> : hist.history.length === 0 ? <p className="muted">No credit activity yet.</p> : (
          <table className="table">
            <thead><tr><th>Date</th><th>Type</th><th>Detail</th><th className="num">Change</th><th className="num">Balance</th></tr></thead>
            <tbody>
              {hist.history.map((h, i) => (
                <tr key={i}>
                  <td className="nowrap">{fmtTs(h.ts)}</td>
                  <td>{label(h.kind)}</td>
                  <td>{h.kind === "job_run" || h.kind === "refund" ? <a href={`#/jobs/${h.ref}`}>{h.note || h.ref}</a> : h.note}</td>
                  <td className={`num ${h.delta > 0 ? "good-text" : "bad-text"}`}>{h.delta > 0 ? `+${h.delta}` : h.delta}</td>
                  <td className="num">{h.balance_after}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
