// The Claude API key's place on the Customize page (ADR-033, F10 slice 2c): paste
// it once, Windows Hello confirms that exact save, and it is sealed in the vault.
// The page never gets the key back: it shows only whether one is saved and when.
// Using the key needs no Windows Hello (the agents run unattended); only saving
// or replacing it does.
import { Fingerprint, KeyRound, ShieldCheck, TriangleAlert } from "lucide-react";
import { useState } from "react";

import type { ClaudeKeyStatus } from "../api";
import { ago } from "../format";

interface Props {
  status: ClaudeKeyStatus;
  canSign: boolean;
  busy: boolean;
  onSave: (key: string) => void;
}

const PREFIX = "sk-ant-";

export function ClaudeKeyPanel({ status, canSign, busy, onSave }: Props) {
  const [key, setKey] = useState("");
  const cleaned = key.trim();
  const looksRight = cleaned.startsWith(PREFIX) && cleaned.length > PREFIX.length && !/\s/.test(cleaned);
  return (
    <section aria-labelledby="claude-key-title" className="rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]">
      <h2 id="claude-key-title" className="flex items-center gap-2 font-semibold text-ink">
        <KeyRound className="size-4 text-faint" aria-hidden="true" />
        Claude API key
      </h2>
      {status.saved && status.saved_at !== null ? (
        <p className="mt-0.5 inline-flex items-center gap-1.5 text-sm text-muted">
          <ShieldCheck className="size-4 text-ok" aria-hidden="true" />
          Saved {ago(status.saved_at)}, sealed in the vault. Agents open it for each call; it is never shown again.
        </p>
      ) : (
        <p className="mt-0.5 text-sm text-muted">
          No key saved yet. Paste it once; Windows Hello confirms the save, then it is sealed in the vault. Nothing
          calls Claude until it is saved.
        </p>
      )}
      {!status.vault ? (
        <p className="mt-2 flex items-start gap-2 text-sm text-ink">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
          <span>
            The vault has no master key yet. Run{" "}
            <code className="font-mono text-xs">uv run python -m tools.vault_key make-key</code>, then restart the
            dashboard.
          </span>
        </p>
      ) : !canSign ? (
        <p className="mt-2 text-sm text-muted">Set up your passkey first: saving the key needs Windows Hello.</p>
      ) : (
        <form
          className="mt-3 flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!looksRight) return;
            onSave(cleaned);
            setKey(""); // never kept on the page after it is sent
          }}
        >
          <label className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-line bg-panel px-3 py-1.5 focus-within:outline-2 focus-within:outline-brand">
            <span className="text-sm text-muted">Key</span>
            <input
              type="password"
              value={key}
              onChange={(event) => {
                setKey(event.target.value);
              }}
              autoComplete="off"
              spellCheck={false}
              placeholder="sk-ant-…"
              className="min-w-0 flex-1 bg-transparent font-mono text-sm text-ink outline-none placeholder:text-faint"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !looksRight}
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-on-brand hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Fingerprint className="size-4" aria-hidden="true" />
            {status.saved ? "Replace key" : "Save key"}
          </button>
          {cleaned.length > 0 && !looksRight && (
            <span className="w-full text-sm text-muted">Claude API keys start with {PREFIX} and have no spaces.</span>
          )}
        </form>
      )}
    </section>
  );
}
