import React, { useEffect, useMemo, useState } from "react";
import { api, fmtTs } from "../api.js";
import { Collapsible, GateValue, Source, StatusPill, label } from "../components/Bits.jsx";

// Consumes siliconscout.output.to_output() as-is (see config/output_schema.json). Every field is read defensively.
const DECISIONS = [
  ["RECOMMEND_ORDER", "Recommend order", "A real quote is in and every hard gate passes."],
  ["QUALIFIED_FOR_RFQ", "Qualified for RFQ", "Approved model, maker or brand of record verified, spec evidenced verbatim."],
  ["PROVISIONAL", "Provisional", "Likely fit with some evidence incomplete; the RFQ asks them to confirm."],
  ["HOLD_FOR_REVIEW", "Hold for review", "Something looks wrong (for example a lookalike name). A person should check."],
  ["CANNOT_VERIFY", "Cannot verify", "Not enough evidence to decide either way."],
  ["NOT_A_FIT", "Not a fit", "Failed at least one hard gate; the reason and source are shown."],
];

export default function JobPage({ jobId, refreshUser }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState("results");

  const load = () => api(`/api/jobs/${jobId}`).then(setData).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [jobId]);

  if (err && !data) return <div className="alert error">{err} <a href="#/">Back to jobs</a></div>;
  if (!data) return <div className="muted center">Loading job…</div>;
  if (!data.output) {
    return (
      <div className="card">
        <h2>{data.meta?.need}</h2>
        <div className="alert error">This run failed and its credit was refunded. Start a new job from the dashboard.</div>
      </div>
    );
  }
  const out = data.output;
  const update = (d) => setData((prev) => ({ ...prev, ...d }));

  const cands = out.candidates || [];
  const c = out.cost || {};
  return (
    <div className="job stack-lg">
      <section className="job-head">
        <div className="eyebrow">Job {out.job_id} · lane {out.lane || "—"} · {fmtTs(data.meta?.created_at)}</div>
        <div className="row between wrap">
          <h1 className="job-title">{out.spec?.raw_need || data.meta?.need}</h1>
          <StatusPill status={out.status} />
        </div>
        <div className="headline-stats">
          <div><span>Candidates</span><b>{cands.length}</b></div>
          <div><span>Ready for RFQ</span><b>{cands.filter((x) => ["QUALIFIED_FOR_RFQ", "RECOMMEND_ORDER"].includes(x.decision)).length}</b></div>
          <div><span>Credits charged</span><b>{data.meta?.charged ?? "—"}</b></div>
          <div><span>Engine cost</span><b>₹{(c.inr_estimate ?? 0).toLocaleString("en-IN")}</b></div>
        </div>
        {out.decision_summary && <div className={`summary ${out.status === "RECOMMEND_ORDER" ? "good" : ""}`}>{out.decision_summary}</div>}
        {data.contract_errors?.length > 0 && (
          <div className="alert warn">Output contract warnings: {data.contract_errors.join("; ")}</div>
        )}
        <div className="seg-tabs" role="tablist">
          <button role="tab" aria-selected={tab === "results"} className={tab === "results" ? "on" : ""} onClick={() => setTab("results")}>Results</button>
          <button role="tab" aria-selected={tab === "cost"} className={tab === "cost" ? "on" : ""} onClick={() => setTab("cost")}>Cost &amp; audit</button>
        </div>
      </section>
      {tab === "results" ? (
        <div className="job-grid">
          <div className="stack-lg grow">
            <SpecCard spec={out.spec || {}} lane={out.lane} />
            <Candidates candidates={cands} />
            <Rfqs jobId={jobId} rfqs={out.rfqs || []} candidates={cands} onUpdate={update} />
            <QuoteIntake jobId={jobId} out={out} onUpdate={update} />
          </div>
          <aside className="stack-lg side">
            <Decision out={out} />
            <Thread jobId={jobId} out={out} onUpdate={update} />
          </aside>
        </div>
      ) : (
        <CostAudit out={out} ledger={data.ledger || []} audit={data.audit || []} charged={data.meta?.charged} />
      )}
    </div>
  );
}

function Thread({ jobId, out, onUpdate }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [reply, setReply] = useState("");
  const send = async (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const r = await api(`/api/jobs/${jobId}/follow_up`, { method: "POST", body: { text } });
      setReply(r.reply || ""); setText("");
      const { reply: _r, ...rest } = r;
      onUpdate(rest);
    } catch (x) { setErr(x.message); } finally { setBusy(false); }
  };
  const qs = out.questions || [];
  const th = out.thread || [];
  return (
    <section className="card" id="thread">
      <h3>Follow-up</h3>
      {qs.length > 0 && (
        <div className="stack">
          <div className="k-label">Clarifying questions</div>
          {qs.map((q, i) => <div key={i} className="bubble q">{typeof q === "string" ? q : q.question || q.text || JSON.stringify(q)}</div>)}
        </div>
      )}
      {th.length > 0 && (
        <div className="stack" style={{ marginTop: ".8rem" }}>
          {th.map((t, i) => <div key={i} className={`bubble ${t.role === "user" ? "me" : ""}`}><span className="k-label">{t.role || "turn"}</span>{t.text || t.content || t.need || JSON.stringify(t)}</div>)}
        </div>
      )}
      {!qs.length && !th.length && <p className="muted small-text">No open questions. Follow-up turns for this job will appear here.</p>}
      {reply && !(out.thread || []).length && <div className="bubble" style={{ marginTop: ".8rem" }}>{reply}</div>}
      <form onSubmit={send} className="stack" style={{ marginTop: ".8rem" }}>
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="Answer a question or ask a follow-up" maxLength={2000} required />
        {err && <div className="alert error">{err}</div>}
        <button className="primary" disabled={busy || !text.trim()}>{busy ? "Sending…" : "Send"}</button>
      </form>
    </section>
  );
}

function SpecCard({ spec, lane }) {
  const reqs = Object.entries(spec.requirements || {});
  return (
    <section className="card" id="spec">
      <h3>Derived spec <span className="muted small-text">from {spec.source || "rules"}</span></h3>
      <div className="spec-grid">
        <Field k="Device" v={spec.device} wide />
        <Field k="Quantity" v={spec.quantity != null ? `${spec.quantity} ${spec.unit && spec.unit !== "units" ? `(for ${spec.unit})` : ""}` : null} />
        <Field k="Deliver to" v={spec.delivery_location} />
        <Field k="Lane" v={lane} />
        <Field k="MPN" v={spec.mpn} />
      </div>
      {reqs.length > 0 && (
        <>
          <h4>Requirements</h4>
          <div className="chips">{reqs.map(([k, v]) => <span key={k} className="chip static" title={k}>{v}</span>)}</div>
        </>
      )}
      {spec.assumptions?.length > 0 && (
        <>
          <h4>Assumptions <span className="muted small-text">check these before approving RFQs</span></h4>
          <ul className="assumptions">{spec.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </>
      )}
    </section>
  );
}

function Field({ k, v, wide }) {
  return (
    <div className={`field ${wide ? "wide" : ""}`}>
      <div className="k">{k}</div>
      <div className={`v ${v ? "" : "unknown"}`}>{v || "UNKNOWN"}</div>
    </div>
  );
}

function Candidates({ candidates }) {
  const groups = useMemo(() => {
    const known = new Set(DECISIONS.map((d) => d[0]));
    const g = DECISIONS.map(([id, t, d]) => ({ id, t, d, items: candidates.filter((c) => c.decision === id) }));
    const other = candidates.filter((c) => !known.has(c.decision));
    if (other.length) g.push({ id: "OTHER", t: "Other", d: "", items: other });
    return g.filter((x) => x.items.length);
  }, [candidates]);
  const byId = Object.fromEntries(candidates.map((c) => [c.candidate_id, c]));

  return (
    <section className="card" id="candidates">
      <h3>Candidates <span className="muted small-text">grouped by decision state, no blended scores</span></h3>
      <div className="decision-bar">
        {groups.map((g) => <span key={g.id} className={`seg d-${g.id}`} style={{ flex: g.items.length }} title={`${g.t}: ${g.items.length}`}>{g.items.length}</span>)}
      </div>
      {groups.length === 0 && <div className="empty"><b>No candidates found yet.</b><span className="muted">The registries and Crustdata returned no supplier for this spec. Try answering the questions in Follow-up or making the need more specific.</span></div>}
      {groups.map((g) => (
        <div key={g.id} className="group">
          <div className={`group-head d-${g.id}`}>
            <b>{g.t}</b> <span className="count">{g.items.length}</span>
            <span className="muted">{g.d}</span>
          </div>
          {g.items.map((c) => <CandidateCard key={c.candidate_id} c={c} byId={byId} />)}
        </div>
      ))}
    </section>
  );
}

function gateDate(g, c) {
  const m = (g.source || "").match(/as on (\d{4}-\d{2}-\d{2})/);
  if (m) return `as on ${m[1]}`;
  if (g.gate === "price_lead_stock" && c.quote?.received_at) return `quoted ${fmtTs(c.quote.received_at)}`;
  return "";
}

function CandidateCard({ c, byId }) {
  const fails = (c.gates || []).filter((g) => g.value === "FAIL").length;
  const passes = (c.gates || []).filter((g) => g.value === "PASS" || g.value === "CONFIRMED").length;
  const cc = c.company_context || {};
  return (
    <Collapsible
      title={
        <span className="cand-title">
          <span className="rank">#{c.rank ?? "–"}</span>
          <b>{c.company}</b>
          {c.model && <span className="mono model">{c.model}</span>}
          <span className={`role r-${c.role}`}>{label(c.role)}</span>
          {c.flags?.length > 0 && <span className="flag-dot" title={c.flags.join("\n")}>⚑ {c.flags.length}</span>}
        </span>
      }
      right={<span className="muted small-text nowrap">{passes} pass{fails ? ` · ${fails} fail` : ""} · {c.gates?.length || 0} gates</span>}
    >
      {c.flags?.map((f, i) => <div key={i} className="alert warn">{f}</div>)}
      <div className="cand-meta">
        {c.maker_of_record && <span>Maker of record: <b>{c.maker_of_record}</b>{c.role !== "MAKER" && " (recorded separately)"}</span>}
        {c.tied_with?.length > 0 && <span>Tied on commercial terms with {c.tied_with.map((t) => byId[t]?.company || t).join(", ")}</span>}
        {c.missing_evidence?.length > 0 && <span className="bad-text">Missing evidence: {c.missing_evidence.join(", ")}</span>}
      </div>
      {cc.matched ? (
        <div className="context muted">
          Company context (Crustdata, not approval evidence):{" "}
          {["name", "primary_domain", "hq", "country", "headcount", "employee_count_range", "year_founded"].filter((k) => cc[k]).map((k) => `${label(k)} ${cc[k]}`).join(" · ")}
        </div>
      ) : Object.keys(cc).length > 0 ? <div className="context muted">Crustdata: no company record found.</div> : null}

      <div className="table-wrap">
        <table className="table gates">
          <thead><tr><th>Gate</th><th>Value</th><th>Reason</th><th>Source &amp; verbatim passage</th><th>Date</th></tr></thead>
          <tbody>
            {(c.gates || []).map((g, i) => (
              <tr key={i} className={g.gate.startsWith("spec:") ? "sub" : ""}>
                <td className="mono nowrap">{g.gate}</td>
                <td><GateValue value={g.value} /></td>
                <td>{g.reason}</td>
                <td>
                  <Source source={g.source} />
                  {g.passage && <blockquote className="passage">“{g.passage}”</blockquote>}
                </td>
                <td className="nowrap muted">{gateDate(g, c) || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {c.registry_hits?.length > 0 && (
        <>
          <h4>Registry rows</h4>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Registry</th><th>Company / model</th><th>Ref</th><th>Valid until</th><th>As on</th><th>Label</th></tr></thead>
              <tbody>
                {c.registry_hits.map((h, i) => (
                  <tr key={i}>
                    <td>{h.title || h.registry}<div className="muted mono">sha256 {String(h.sha256 || "").slice(0, 16)}</div></td>
                    <td>{h.company}{h.brand ? ` (brand ${h.brand})` : ""} / <span className="mono">{h.model}</span></td>
                    <td className="mono">{h.ref}</td>
                    <td>{h.valid_until || "—"}</td>
                    <td className="nowrap">{h.as_on}</td>
                    <td><span className={`pill ${h.label === "TEST_FIXTURE" ? "warn" : "good"}`}>{h.label}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {c.quote && <QuoteView q={c.quote} />}
    </Collapsible>
  );
}

function QuoteView({ q }) {
  const keys = ["label", "unit_price", "quantity", "goods_value", "gst_pct", "freight", "installation", "delivery_days", "warranty_months", "approval_ref", "validity_until", "received_at"];
  return (
    <>
      <h4>Quote</h4>
      <div className="spec-grid">
        {keys.map((k) => <Field key={k} k={label(k)} v={q[k] === "UNKNOWN" ? null : k === "received_at" ? fmtTs(q[k]) : String(q[k] ?? "")} />)}
      </div>
    </>
  );
}

function Rfqs({ jobId, rfqs, candidates, onUpdate }) {
  const drafts = rfqs.filter((r) => r.status === "DRAFT");
  const [sel, setSel] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const name = Object.fromEntries(candidates.map((c) => [c.candidate_id, c]));

  const approve = async (ids) => {
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await api(`/api/jobs/${jobId}/approve`, { method: "POST", body: { rfq_ids: ids } });
      onUpdate(r);
      setSel(new Set());
      setMsg(`Approved ${r.approved.length} RFQ${r.approved.length === 1 ? "" : "s"}${r.output.rfqs.some((x) => x.status === "SENT") ? "; sent through the outbox" : ""}.`);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const toggle = (id) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <section className="card" id="rfqs">
      <div className="row between wrap">
        <h3>RFQ drafts <span className="muted small-text">nothing is sent until you approve</span></h3>
        {drafts.length > 0 && (
          <div className="row gap">
            <button disabled={busy || sel.size === 0} onClick={() => approve([...sel])}>Approve selected ({sel.size})</button>
            <button className="primary" disabled={busy} onClick={() => approve("all")}>Approve all {drafts.length} &amp; send</button>
          </div>
        )}
      </div>
      <p className="muted small-text">
        Sends go through an idempotent outbox (each RFQ has an idempotency key, so a restart never double-sends). In this
        build "send" writes an .eml to the job's outbox folder; no email leaves the machine. All drafts must be approved before sending starts.
      </p>
      {err && <div className="alert error">{err}</div>}
      {msg && <div className="alert good">{msg}</div>}
      {rfqs.length === 0 && <p className="muted">No RFQs: no candidate qualified for one.</p>}
      {rfqs.map((r) => (
        <Collapsible key={r.rfq_id}
          title={
            <span className="cand-title">
              {r.status === "DRAFT" && (
                <input type="checkbox" checked={sel.has(r.rfq_id)} onClick={(e) => e.stopPropagation()} onChange={() => toggle(r.rfq_id)} aria-label={`select ${r.rfq_id}`} />
              )}
              <b>{name[r.candidate_id]?.company || r.candidate_id}</b>
              <span className="muted">{r.to || "no contact email"}</span>
            </span>
          }
          right={<span className={`pill ${r.status === "SENT" ? "good" : r.status === "HELD_AMBIGUOUS" ? "bad" : r.status === "APPROVED" ? "info" : "warn"}`}>{label(r.status)}</span>}
        >
          <div className="muted small-text">
            Subject: <b>{r.subject}</b> · key <span className="mono">{r.idempotency_key}</span>
            {r.approved_by && <> · approved by {r.approved_by}</>}{r.sent_at && <> · sent {fmtTs(r.sent_at)}</>}
          </div>
          <pre className="email">{r.body}</pre>
        </Collapsible>
      ))}
    </section>
  );
}

function QuoteIntake({ jobId, out, onUpdate }) {
  const cands = out.candidates || [];
  const sentIds = new Set((out.rfqs || []).filter((r) => r.status === "SENT").map((r) => r.candidate_id));
  const options = cands.filter((c) => sentIds.has(c.candidate_id)).length ? cands.filter((c) => sentIds.has(c.candidate_id)) : cands;
  const blank = { candidate_id: options[0]?.candidate_id || "", label: "SUPPLIER_QUOTE", unit_price: "", quantity: out.spec?.quantity || "", delivery_days: "", warranty_months: "", approval_ref: "", gst_pct: "", freight: "", installation: "" };
  const [f, setF] = useState(blank);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(""); setOk("");
    const num = (v) => (v === "" || v == null ? null : Number(v));
    const body = { candidate_id: f.candidate_id, label: f.label, unit_price: num(f.unit_price), quantity: num(f.quantity), delivery_days: num(f.delivery_days), warranty_months: num(f.warranty_months), gst_pct: num(f.gst_pct), freight: num(f.freight), installation: num(f.installation), approval_ref: f.approval_ref || null };
    try {
      const r = await api(`/api/jobs/${jobId}/quote`, { method: "POST", body });
      onUpdate(r);
      setOk("Quote recorded and gates re-evaluated.");
      setF({ ...blank, candidate_id: f.candidate_id });
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  const selected = cands.find((c) => c.candidate_id === f.candidate_id);
  const refs = (selected?.registry_hits || []).map((h) => h.ref).filter(Boolean);

  return (
    <section className="card" id="quote">
      <h3>Quote intake <span className="muted small-text">enter a supplier reply; gates re-run against it</span></h3>
      {cands.length === 0 ? <p className="muted">No candidates to quote.</p> : (
        <form onSubmit={submit} className="quote-form">
          <label>Candidate
            <select value={f.candidate_id} onChange={set("candidate_id")}>
              {options.map((c) => <option key={c.candidate_id} value={c.candidate_id}>{c.company} {c.model ? `· ${c.model}` : ""}</option>)}
            </select>
          </label>
          <label>Price label
            <select value={f.label} onChange={set("label")}>
              <option value="SUPPLIER_QUOTE">Supplier quote</option>
              <option value="DISTRIBUTOR_LIVE">Distributor live</option>
              <option value="PUBLIC_BUDGETARY">Public budgetary (not orderable)</option>
            </select>
          </label>
          <label>Unit price (INR)<input type="number" min="0" step="0.01" value={f.unit_price} onChange={set("unit_price")} /></label>
          <label>Quantity<input type="number" min="0" value={f.quantity} onChange={set("quantity")} /></label>
          <label>Delivery days<input type="number" min="0" value={f.delivery_days} onChange={set("delivery_days")} /></label>
          <label>Warranty months<input type="number" min="0" value={f.warranty_months} onChange={set("warranty_months")} /></label>
          <label>Approval / TAC ref
            <input list="tac-refs" value={f.approval_ref} onChange={set("approval_ref")} placeholder={refs[0] || ""} />
            <datalist id="tac-refs">{refs.map((r) => <option key={r} value={r} />)}</datalist>
          </label>
          <label>GST %<input type="number" min="0" step="0.1" value={f.gst_pct} onChange={set("gst_pct")} /></label>
          <label>Freight (INR)<input type="number" min="0" value={f.freight} onChange={set("freight")} /></label>
          <label>Installation (INR)<input type="number" min="0" value={f.installation} onChange={set("installation")} /></label>
          <div className="quote-actions">
            <button className="primary" disabled={busy || !f.candidate_id}>{busy ? "Saving…" : "Add quote"}</button>
            {err && <div className="alert error">{err}</div>}
            {ok && <div className="alert good">{ok}</div>}
          </div>
        </form>
      )}
    </section>
  );
}

function Decision({ out }) {
  const rec = (out.candidates || []).find((c) => c.decision === "RECOMMEND_ORDER");
  return (
    <section className="card" id="decision">
      <h3>Decision summary</h3>
      <div className="row gap wrap"><StatusPill status={out.status} /></div>
      <p className="summary-text">{out.decision_summary || "No decision yet."}</p>
      {rec?.quote && (
        <div className="spec-grid">
          <Field k="Supplier" v={rec.company} wide />
          <Field k="Model" v={rec.model} />
          <Field k="Goods value" v={rec.quote.goods_value !== "UNKNOWN" ? `${rec.quote.currency || "INR"} ${Number(rec.quote.goods_value).toLocaleString("en-IN")}` : null} />
          <Field k="GST %" v={rec.quote.gst_pct !== "UNKNOWN" ? String(rec.quote.gst_pct) : null} />
          <Field k="Freight" v={rec.quote.freight !== "UNKNOWN" ? String(rec.quote.freight) : null} />
        </div>
      )}
      <p className="muted small-text">No order is recommended without a real supplier or live distributor quote. Tax and freight are always shown separately from goods value.</p>
    </section>
  );
}

function CostAudit({ out, ledger, audit, charged }) {
  const c = out.cost || {};
  const plan = out.crustdata_plan;
  return (
    <section className="card" id="cost">
      <h3>Cost ledger</h3>
      <div className="stat-row">
        <Stat k="SiliconScout credits charged" v={charged ?? "—"} />
        <Stat k="Paid API calls" v={c.paid_calls ?? 0} />
        <Stat k="Engine cost estimate" v={`₹${(c.inr_estimate ?? 0).toLocaleString("en-IN")}`} />
        <Stat k="Crustdata credits" v={c.crustdata_credits ?? 0} />
        <Stat k="LLM tokens in / out" v={`${c.tokens_in ?? 0} / ${c.tokens_out ?? 0}`} />
      </div>
      {plan && Object.keys(plan).length > 1 && (
        <p className="muted small-text">
          Crustdata {plan.spent ? "spent" : "dry run"}{plan.mode ? ` (${plan.mode})` : ""}
          {plan.to_send != null ? `: ${plan.to_send} companies` : ""}{plan.worst_case_credits != null ? `, worst case ${plan.worst_case_credits} credits` : ""}
          {plan.spent ? "" : ". Nothing was sent."}
        </p>
      )}
      {ledger.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Time</th><th>Provider</th><th>Operation</th><th>INR</th><th>Credits</th><th>Tokens</th><th>ms</th></tr></thead>
            <tbody>{ledger.map((r, i) => (
              <tr key={i}><td className="nowrap">{fmtTs(r.ts)}</td><td>{r.provider}</td><td>{r.op}</td><td>{r.inr_estimate}</td><td>{r.credits ?? "—"}</td><td>{r.tokens_in != null ? `${r.tokens_in}/${r.tokens_out}` : "—"}</td><td>{r.duration_ms}</td></tr>
            ))}</tbody>
          </table>
        </div>
      ) : <p className="muted">No paid calls in this job: everything ran on deterministic rules and cached data.</p>}

      <Collapsible title={<b>Audit log ({audit.length} events)</b>}>
        <div className="table-wrap">
          <table className="table audit">
            <thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead>
            <tbody>{audit.map((a, i) => (
              <tr key={i}><td className="nowrap">{fmtTs(a.ts)}</td><td className="mono">{a.event}</td><td><code className="detail">{JSON.stringify(a.detail)}</code></td></tr>
            ))}</tbody>
          </table>
        </div>
      </Collapsible>
    </section>
  );
}

function Stat({ k, v }) {
  return <div className="stat"><div className="v">{v}</div><div className="k">{k}</div></div>;
}
