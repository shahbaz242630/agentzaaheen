// The Customize page's switches (ADR-030): off at once (after a confirm on the
// page), "Ask to switch on" files a request, and a waiting request can be
// withdrawn, refused, or approved with your passkey (Windows Hello, ADR-SEC-007).
import { Clock, Fingerprint, Power, TriangleAlert, Undo2, X } from "lucide-react";
import { useState } from "react";

import type { ItemState, Kind, SwitchRequest } from "../api";

const STATE: Record<ItemState, { label: string; dot: string }> = {
  on: { label: "On", dot: "bg-ok" },
  off: { label: "Off", dot: "bg-unknown" },
  changed: { label: "Off: changed since approved", dot: "bg-warn" },
  view_only: { label: "View only (unsigned)", dot: "bg-unknown" },
};

// The server's reason codes, in plain English.
const REASON: Record<string, string> = {
  "registry.not_allowed": "That isn't allowed from here.",
  "registry.unknown_item": "That item no longer exists. Reload the page.",
  "registry.view_only": "Outside plugins are view only until they can be signed and checked.",
  "registry.not_on": "It is already off.",
  "registry.not_off": "It is already on.",
  "registry.already_asked": "You have already asked; it is waiting for approval.",
  "registry.unknown_request": "That request was already withdrawn or decided.",
  "registry.read_only": "Switches need the laptop database. Start the dashboard with --switches.",
  "registry.store_unavailable": "The laptop database did not answer. Is it running?",
  "registry.stale": "Its files changed since you asked, so it can't be approved. Withdraw it and ask again.",
  "registry.no_passkey": "Set up your passkey first (start the dashboard with --enrol-passkey).",
  "registry.passkey_failed": "Windows Hello did not confirm it, so nothing was switched on. Please try again.",
  "passkey.wrong_code": "That code doesn't match the one shown when the dashboard started.",
  "passkey.enrol_closed": "Passkey set-up is closed. Start the dashboard with --enrol-passkey to open it.",
  "passkey.not_verified": "Windows Hello did not confirm it. Please try again.",
  "claude_key.bad_key": "That doesn't look like a Claude API key (they start with sk-ant-). Copy it again from the Claude Console.",
  "claude_key.passkey_failed": "Windows Hello did not confirm it, so the key was not saved. Please try again.",
  "claude_key.no_vault": "The vault has no master key yet. Run: uv run python -m tools.vault_key make-key, then restart the dashboard.",
  "claude_key.read_only": "Saving the Claude key needs the laptop database. Start the dashboard with --switches.",
  "claude_key.store_unavailable": "The laptop database did not answer. Is it running?",
};

export function reason(code: string | null): string {
  return (code && REASON[code]) ?? "That did not work. Please try again.";
}

export function StatePill({ state }: { state: ItemState }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-muted">
      <span className={`size-1.5 rounded-full ${STATE[state].dot}`} aria-hidden="true" />
      {STATE[state].label}
    </span>
  );
}

const BUTTON = [
  "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
  "disabled:cursor-not-allowed disabled:opacity-50",
].join(" ");
const QUIET = `${BUTTON} border border-line bg-panel text-ink hover:bg-raised`;
const STRONG = `${BUTTON} bg-alert text-on-brand hover:opacity-90`;
const APPROVE = `${BUTTON} bg-brand text-on-brand hover:opacity-90`;

interface SwitchProps {
  name: string;
  state: ItemState;
  waiting: boolean;
  busy: boolean;
  onSwitchOff: () => void;
  onRequest: () => void;
}

export function SwitchControl({ name, state, waiting, busy, onSwitchOff, onRequest }: SwitchProps) {
  const [confirming, setConfirming] = useState(false);
  if (state === "view_only") return null;
  if (state === "on") {
    return confirming ? (
      <div role="group" aria-label={`Switch ${name} off?`} className="flex flex-col gap-2 rounded-xl bg-raised p-3">
        <p className="text-sm text-ink">
          Switch {name} off? To switch it back on, you ask and then approve it with your passkey.
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            className={STRONG}
            disabled={busy}
            onClick={() => {
              setConfirming(false);
              onSwitchOff();
            }}
          >
            <Power className="size-4" aria-hidden="true" />
            Switch off
          </button>
          <button
            type="button"
            className={QUIET}
            onClick={() => {
              setConfirming(false);
            }}
          >
            Keep it on
          </button>
        </div>
      </div>
    ) : (
      <button
        type="button"
        className={`${QUIET} self-start`}
        disabled={busy}
        onClick={() => {
          setConfirming(true);
        }}
      >
        <Power className="size-4" aria-hidden="true" />
        Switch off
      </button>
    );
  }
  if (waiting) {
    return (
      <p className="inline-flex items-center gap-1.5 text-sm text-muted">
        <Clock className="size-4" aria-hidden="true" />
        Waiting for your approval
      </p>
    );
  }
  return (
    <button type="button" className={`${QUIET} self-start`} disabled={busy} onClick={onRequest}>
      Ask to switch on
    </button>
  );
}

interface RequestsProps {
  requests: SwitchRequest[];
  title: (kind: Kind, id: string) => string;
  busy: boolean;
  canApprove: boolean;
  onWithdraw: (requestId: string) => void;
  onApprove: (requestId: string) => void;
  onRefuse: (requestId: string) => void;
}

const KIND_LABEL: Record<Kind, string> = { connector: "Connector", skill: "Skill", plugin: "Plugin" };

export function RequestList({ requests, title, busy, canApprove, onWithdraw, onApprove, onRefuse }: RequestsProps) {
  if (requests.length === 0) return null;
  return (
    <section aria-labelledby="waiting-title" className="rounded-2xl border border-line bg-panel px-5 py-4">
      <h2 id="waiting-title" className="font-semibold text-ink">
        Waiting for approval
      </h2>
      <p className="mt-0.5 text-sm text-muted">
        Nothing switches on until you approve it with your passkey (Windows Hello asks each time).
      </p>
      <ul className="mt-3 flex flex-col divide-y divide-line">
        {requests.map((request) => (
          <li key={request.id} className="flex flex-wrap items-center justify-between gap-3 py-2.5">
            <div className="min-w-0">
              <p className="font-medium text-ink">{title(request.kind, request.item_id)}</p>
              <p className="text-xs text-muted">
                {KIND_LABEL[request.kind]} · asked {new Date(request.requested_at).toLocaleString()}
              </p>
              {request.stale && (
                <p className="mt-1 inline-flex items-center gap-1.5 text-xs text-ink">
                  <TriangleAlert className="size-3.5 text-warn" aria-hidden="true" />
                  Its files changed since you asked, so it can't be approved. Withdraw it and ask again.
                </p>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={APPROVE}
                disabled={busy || request.stale || !canApprove}
                aria-label={`Approve ${title(request.kind, request.item_id)} with your passkey`}
                onClick={() => {
                  onApprove(request.id);
                }}
              >
                <Fingerprint className="size-4" aria-hidden="true" />
                Approve
              </button>
              <button
                type="button"
                className={QUIET}
                disabled={busy}
                aria-label={`Refuse ${title(request.kind, request.item_id)}`}
                onClick={() => {
                  onRefuse(request.id);
                }}
              >
                <X className="size-4" aria-hidden="true" />
                Refuse
              </button>
              <button
                type="button"
                className={QUIET}
                disabled={busy}
                aria-label={`Withdraw the request for ${title(request.kind, request.item_id)}`}
                onClick={() => {
                  onWithdraw(request.id);
                }}
              >
                <Undo2 className="size-4" aria-hidden="true" />
                Withdraw
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
