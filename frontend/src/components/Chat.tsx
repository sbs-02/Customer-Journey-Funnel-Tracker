import { useState } from "react";
import type { ToolCall } from "../api";

/**
 * Shows the receipts behind a number.
 *
 * The assignment requires the agent to state which snapshot and date range every
 * figure came from. Rendering that in the UI -- rather than trusting the model to
 * mention it in prose -- means the claim is verifiable even when the model
 * forgets to say it.
 */
export function ProvenanceCard({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false);

  if (call.result.error) {
    return (
      <div className="prov prov--error">
        <strong>{call.name}</strong> could not answer: {String(call.result.error)}
      </div>
    );
  }

  const p = call.result.provenance;
  if (!p) return null;

  return (
    <div className="prov">
      <button className="prov__toggle" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} <code>{call.name}</code>
        <span className="prov__snap">snapshot {p.snapshot_id}</span>
      </button>

      {open && (
        <dl className="prov__body">
          <dt>Snapshot</dt>
          <dd><code>{p.snapshot_id}</code></dd>

          <dt>Committed</dt>
          <dd>{new Date(p.snapshot_committed_at).toLocaleString()}</dd>

          <dt>Computed</dt>
          <dd>{new Date(p.as_of_date).toLocaleString()}</dd>

          <dt>Date range</dt>
          <dd><code>{JSON.stringify(p.date_range)}</code></dd>

          <dt>Source tables</dt>
          <dd>{p.source_tables.join(", ")}</dd>

          <dt>Calculation</dt>
          <dd>{p.calculation}</dd>
        </dl>
      )}
    </div>
  );
}