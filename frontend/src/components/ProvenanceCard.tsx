import { useState } from "react";
import type { ToolCall } from "../api";

/**
 * The backend types date_range loosely, so read the common {start, end} shape
 * and fall back to the raw value for anything else rather than dropping it.
 */
function dateRange(range: Record<string, unknown>): string {
  const start = range?.start;
  const end = range?.end;
  if (typeof start === "string" && typeof end === "string") {
    return `${start} → ${end}`;
  }
  return JSON.stringify(range);
}

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
    <div className={`prov${open ? " prov--open" : ""}`}>
      <button
        className="prov__toggle"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="prov__caret" aria-hidden="true">
          ▶
        </span>
        <span className="prov__name">{call.name}</span>
        <span className="prov__snap">snapshot {p.snapshot_id}</span>
      </button>

      {/* The drawer stays mounted so its height can animate 0fr → 1fr. */}
      <div className="prov__drawer" inert={!open}>
        <div className="prov__clip">
          {/* The snapshot id is already stamped on the strip above, so the
              drawer answers the next questions instead of repeating it. */}
          <dl className="prov__body">
            <dt>Committed</dt>
            <dd>{new Date(p.snapshot_committed_at).toLocaleString()}</dd>

            <dt>Computed</dt>
            <dd>{new Date(p.as_of_date).toLocaleString()}</dd>

            <dt>Date range</dt>
            <dd><code>{dateRange(p.date_range)}</code></dd>

            <dt>Source tables</dt>
            <dd className="prov__tables">
              {p.source_tables.map((t) => <code key={t}>{t}</code>)}
            </dd>

            <dt>Calculation</dt>
            <dd>{p.calculation}</dd>
          </dl>
        </div>
      </div>
    </div>
  );
}
