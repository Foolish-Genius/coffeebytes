import React, { useEffect, useState } from "react";
import { api, fmtTs, go } from "../api.js";
import { StatusPill } from "../components/Bits.jsx";

const EXAMPLES = [
  "Need GPS trackers for 300 trucks, deliver to Pune",
  "AIS-140 VLTD with panic button for 120 school buses in Bengaluru",
  "Need 2000 pcs of STM32F103C8T6",
];

export default function Dashboard({ cfg, user, refreshUser }) {
  const [need, setNeed] = useState(EXAMPLES[0]);
  const [source, setSource] = useState("fixture");
  const [jobs, setJobs] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/api/jobs").then(setJobs).catch((e) => setErr(e.message));
  }, []);

  const run = async (e) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const r = await api("/api/jobs", { method: "POST", body: { need, source } });
      await refreshUser();
      go(`/jobs/${r.job_id}`);
    } catch (e2) {
      setErr(e2.message);
      refreshUser();
    } finally {
      setBusy(false);
    }
  };

  const cost = cfg.credits_per_job;
  const short = !user.is_admin && user.credits < cost;

  return (
    <div className="stack-lg">
      <section className="card hero">
        <h2>New sourcing job</h2>
        <p className="muted">Describe the operating need in plain words. SiliconScout derives the spec, checks the approval registries, and drafts RFQs for your approval.</p>
        <form onSubmit={run} className="stack">
          <textarea rows={3} value={need} onChange={(e) => setNeed(e.target.value)} maxLength={2000} required />
          <div className="chips">
            {EXAMPLES.map((x) => (
              <button type="button" key={x} className="chip" onClick={() => setNeed(x)}>{x}</button>
            ))}
          </div>
          <div className="row between wrap">
            <label className="inline">
              Discovery source
              <select value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="fixture">Demo candidates (fixture)</option>
                <option value="registry">Seed from approval registries</option>
              </select>
            </label>
            <div className="row gap">
              <span className="muted">{user.is_admin ? <>Admin: <b>no charge</b></> : <>Costs <b>{cost}</b> credit · you have <b>{user.credits}</b></>}</span>
              {short ? (
                <a className="btn primary" href="#/billing">Buy credits to run</a>
              ) : (
                <button className="primary" disabled={busy}>{busy ? "Running…" : "Run sourcing job"}</button>
              )}
            </div>
          </div>
          {err && <div className="alert error">{err}</div>}
          {busy && <div className="alert info">Translating the need, checking registries and gating candidates. This usually takes a few seconds.</div>}
        </form>
        <EngineStatus cfg={cfg} />
      </section>

      <section className="card">
        <h3>Your jobs</h3>
        {jobs === null ? (
          <p className="muted">Loading…</p>
        ) : jobs.length === 0 ? (
          <p className="muted">No jobs yet. Run the trucks demo above to see a full sourcing run.</p>
        ) : (
          <table className="table clickable">
            <thead><tr><th>Need</th><th>Status</th><th>Source</th><th>Started</th></tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} onClick={() => go(`/jobs/${j.id}`)}>
                  <td><a href={`#/jobs/${j.id}`}>{j.need}</a><div className="muted mono">{j.id}</div></td>
                  <td><StatusPill status={j.status} /></td>
                  <td>{j.source}</td>
                  <td className="nowrap">{fmtTs(j.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function EngineStatus({ cfg }) {
  const crust = { live: "Crustdata live", dry_run: "Crustdata dry-run (no spend)", off: "Crustdata off" }[cfg.crustdata] || "";
  return (
    <div className="engine muted">
      Engine mode <b>{cfg.engine_mode}</b> · {cfg.llm_enabled ? "Claude fallback on (cost-capped)" : "rules-only"} · {crust}
      {cfg.engine_mode === "demo" && <> · registry rows are <b>TEST_FIXTURE</b> data</>}
    </div>
  );
}
