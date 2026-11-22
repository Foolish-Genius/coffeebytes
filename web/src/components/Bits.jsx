import React, { useState } from "react";

const STATUS_TONE = {
  RECOMMEND_ORDER: "good", RFQS_OUTSTANDING: "info", AWAITING_APPROVAL: "warn", RUNNING: "info", NEW: "muted",
  HELD_FOR_PERSON: "bad", NO_QUALIFIED_SUPPLIER: "bad", CANNOT_VERIFY: "muted", ERROR: "bad",
};

export const label = (s) => (s || "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());

export function StatusPill({ status }) {
  return <span className={`pill ${STATUS_TONE[status] || "muted"}`}>{label(status) || "—"}</span>;
}

const GATE_TONE = { PASS: "good", CONFIRMED: "good", PARTIAL: "warn", UNKNOWN: "muted", TO_CONFIRM: "muted", FAIL: "bad", "N/A": "na" };
const GATE_ICON = { PASS: "✓", CONFIRMED: "✓", PARTIAL: "◐", UNKNOWN: "?", TO_CONFIRM: "…", FAIL: "✕", "N/A": "–" };

export function GateValue({ value }) {
  return (
    <span className={`gate-v ${GATE_TONE[value] || "muted"}`}>
      <span aria-hidden="true">{GATE_ICON[value] || "·"}</span> {value}
    </span>
  );
}

export function Collapsible({ title, children, defaultOpen = false, right }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`collapsible ${open ? "open" : ""}`}>
      <div className="collapsible-head" role="button" tabIndex={0} aria-expanded={open} onClick={() => setOpen(!open)}
        onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) { e.preventDefault(); setOpen(!open); } }}>
        <span className="caret">{open ? "▾" : "▸"}</span>
        <span className="grow">{title}</span>
        {right}
      </div>
      {open && <div className="collapsible-body">{children}</div>}
    </div>
  );
}

export function Source({ source }) {
  if (!source) return <span className="muted">—</span>;
  if (/^https?:\/\//.test(source)) {
    return <a href={source} target="_blank" rel="noreferrer noopener" className="src">{source}</a>;
  }
  return <span className="src">{source}</span>;
}
