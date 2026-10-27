// The passkey's place on the Customize page (ADR-SEC-007): set it up with the
// code printed at start (or replace a lost one the same way), then show that it
// is ready. A page opened at 127.0.0.1 is told to use localhost, because
// passkeys need a name, not an IP.
import { Fingerprint, ShieldCheck, TriangleAlert } from "lucide-react";
import { useState } from "react";

import type { PasskeyStatus } from "../api";

interface Props {
  status: PasskeyStatus;
  onAddress: boolean;
  busy: boolean;
  onSetUp: (code: string) => void;
}

export function PasskeyPanel({ status, onAddress, busy, onSetUp }: Props) {
  const [code, setCode] = useState("");
  const port = window.location.port ? `:${window.location.port}` : "";
  if (!onAddress) {
    return (
      <p className="flex items-start gap-2 rounded-xl border border-warn bg-warn-soft px-4 py-3 text-sm text-ink">
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
        <span>
          Passkeys only work at{" "}
          <a className="font-medium underline" href={`http://localhost${port}/`}>
            http://localhost{port}
          </a>
          . Open the dashboard there to approve requests.
        </span>
      </p>
    );
  }
  if (!status.enrolment_open) {
    return (
      <section aria-labelledby="passkey-title" className="rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]">
        <h2 id="passkey-title" className="flex items-center gap-2 font-semibold text-ink">
          <Fingerprint className="size-4 text-faint" aria-hidden="true" />
          Passkey
        </h2>
        {status.enrolled ? (
          <p className="mt-0.5 inline-flex items-center gap-1.5 text-sm text-muted">
            <ShieldCheck className="size-4 shrink-0 text-ok" aria-hidden="true" />
            Your passkey is set up: approvals ask Windows Hello.
          </p>
        ) : (
          <p className="mt-0.5 text-sm text-muted">No passkey yet. Start the dashboard with --enrol-passkey to set one up.</p>
        )}
      </section>
    );
  }
  const valid = /^[0-9a-f]{8}$/.test(code);
  const replacing = status.enrolled;
  return (
    <section aria-labelledby="passkey-title" className="rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]">
      <h2 id="passkey-title" className="flex items-center gap-2 font-semibold text-ink">
        <Fingerprint className="size-4 text-faint" aria-hidden="true" />
        {replacing ? "Replace your passkey" : "Set up your passkey"}
      </h2>
      <p className="mt-0.5 text-sm text-muted">
        Type the code shown in the terminal where the dashboard started, then confirm with Windows Hello. This passkey
        is the only way to approve requests.
      </p>
      {replacing && (
        <p className="mt-2 flex items-start gap-2 text-sm text-ink">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
          Only do this if your passkey is lost: your current passkey stops working as soon as the new one is set up.
        </p>
      )}
      <form
        className="mt-3 flex flex-wrap items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (valid) onSetUp(code);
        }}
      >
        <label className="flex items-center gap-2 rounded-xl border border-line bg-panel px-3 py-1.5 focus-within:outline-2 focus-within:outline-brand">
          <span className="text-sm text-muted">Code</span>
          <input
            value={code}
            onChange={(event) => {
              // A pasted code may bring quotes or spaces with it: keep only the code's own characters.
              setCode(event.target.value.toLowerCase().replace(/[^0-9a-f]/g, "").slice(0, 8));
            }}
            autoComplete="off"
            className="w-24 bg-transparent font-mono text-sm text-ink outline-none"
          />
        </label>
        {code.length > 0 && !valid && (
          <span className="text-sm text-muted tabular">{code.length} of 8 characters</span>
        )}
        <button
          type="submit"
          disabled={busy || !valid}
          className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-on-brand hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Fingerprint className="size-4" aria-hidden="true" />
          {replacing ? "Replace passkey" : "Set up passkey"}
        </button>
      </form>
    </section>
  );
}
