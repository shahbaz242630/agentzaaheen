// Our server model's own key on the Customize page (F10 slice 6): the server's
// model answers only calls that carry it, on top of the Cloudflare token above.
// Paste it once, Windows Hello confirms that exact save, and it is sealed in its
// own place in the vault. The page never gets the key back: it shows only whether
// one is saved and when. Using it needs no Windows Hello.
import { Cpu, Fingerprint, ShieldCheck } from "lucide-react";
import { useState } from "react";

import type { ModelKeyStatus } from "../api";
import { ago } from "../format";

interface Props {
  status: ModelKeyStatus;
  canSign: boolean;
  busy: boolean;
  onSave: (key: string) => void;
}

const MIN_CHARS = 32;
const MAX_CHARS = 256;
const KEY_CHARS = /^[A-Za-z0-9_-]+$/;
const CLAUDE_PREFIX = "sk-ant-";

export function ModelKeyPanel({ status, canSign, busy, onSave }: Props) {
  const [key, setKey] = useState("");
  const cleaned = key.trim();
  const looksRight =
    KEY_CHARS.test(cleaned) &&
    cleaned.length >= MIN_CHARS &&
    cleaned.length <= MAX_CHARS &&
    !cleaned.startsWith(CLAUDE_PREFIX);
  return (
    <section aria-labelledby="model-key-title" className="rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]">
      <h2 id="model-key-title" className="flex items-center gap-2 font-semibold text-ink">
        <Cpu className="size-4 text-faint" aria-hidden="true" />
        Server model key
      </h2>
      {status.saved && status.saved_at !== null ? (
        <p className="mt-0.5 inline-flex items-center gap-1.5 text-sm text-muted">
          <ShieldCheck className="size-4 text-ok" aria-hidden="true" />
          Saved {ago(status.saved_at)}, locked in the vault. It goes with each call to our server's model.
        </p>
      ) : (
        <p className="mt-0.5 text-sm text-muted">
          Not saved yet. Our server's model answers only calls that carry its own key, as well as the token above.
          Paste it once; Windows Hello confirms the save.
        </p>
      )}
      {!status.vault ? (
        <p className="mt-2 text-sm text-muted">Set up the vault's master key first (see the Claude key above).</p>
      ) : !canSign ? (
        <p className="mt-2 text-sm text-muted">Set up your passkey first: the model key is saved with Windows Hello.</p>
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
            <span className="text-sm text-muted">Model key</span>
            <input
              type="password"
              value={key}
              onChange={(event) => {
                setKey(event.target.value);
              }}
              autoComplete="off"
              spellCheck={false}
              placeholder="letters and digits"
              className="min-w-0 flex-1 bg-transparent font-mono text-sm text-ink outline-none placeholder:text-faint"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !looksRight}
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-on-brand hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Fingerprint className="size-4" aria-hidden="true" />
            {status.saved ? "Replace model key" : "Save model key"}
          </button>
          {cleaned.length > 0 && !looksRight && (
            <span className="w-full text-sm text-muted">
              {cleaned.startsWith(CLAUDE_PREFIX)
                ? "That is a Claude key. This box is for our server's model key."
                : `The model key is ${MIN_CHARS} or more letters and digits, with no spaces.`}
            </span>
          )}
        </form>
      )}
    </section>
  );
}
