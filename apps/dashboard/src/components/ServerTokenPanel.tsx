// The laptop's Cloudflare service token on the Customize page (F10 slice 4): our
// server's model sits behind Cloudflare Access, which lets in only this token.
// Paste both parts once, Windows Hello confirms that exact save, and they are
// sealed in the vault together. The page never gets either part back: it shows
// only whether a token is saved and when. Using it needs no Windows Hello.
import { Fingerprint, Server, ShieldCheck } from "lucide-react";
import { useState } from "react";

import type { ServerTokenStatus } from "../api";
import { ago } from "../format";

interface Props {
  status: ServerTokenStatus;
  canSign: boolean;
  busy: boolean;
  onSave: (clientId: string, clientSecret: string) => void;
}

const ID_SUFFIX = ".access";
const TOKEN_PART = /^[A-Za-z0-9._-]+$/;

export function ServerTokenPanel({ status, canSign, busy, onSave }: Props) {
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const id = clientId.trim();
  const hidden = secret.trim();
  const idRight = TOKEN_PART.test(id) && id.endsWith(ID_SUFFIX) && id.length > ID_SUFFIX.length;
  const secretRight = TOKEN_PART.test(hidden) && !hidden.endsWith(ID_SUFFIX);
  const field =
    "flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-line bg-panel px-3 py-1.5 focus-within:outline-2 focus-within:outline-brand";
  const input = "min-w-0 flex-1 bg-transparent font-mono text-sm text-ink outline-none placeholder:text-faint";
  return (
    <section aria-labelledby="server-token-title" className="rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]">
      <h2 id="server-token-title" className="flex items-center gap-2 font-semibold text-ink">
        <Server className="size-4 text-faint" aria-hidden="true" />
        Server access token
      </h2>
      {status.saved && status.saved_at !== null ? (
        <p className="mt-0.5 inline-flex items-center gap-1.5 text-sm text-muted">
          <ShieldCheck className="size-4 text-ok" aria-hidden="true" />
          Saved {ago(status.saved_at)}, sealed in the vault. The laptop sends it with each call to our server; it is
          never shown again.
        </p>
      ) : (
        <p className="mt-0.5 text-sm text-muted">
          No token saved yet. Cloudflare lets only this token through to our server&apos;s model. Create it in
          Cloudflare Zero Trust (Access → Service credentials → Service tokens) and paste both parts here.
        </p>
      )}
      {!status.vault ? (
        <p className="mt-2 text-sm text-muted">The vault needs its master key first (see the Claude API key).</p>
      ) : !canSign ? (
        <p className="mt-2 text-sm text-muted">Set up your passkey first: saving the token needs Windows Hello.</p>
      ) : (
        <form
          className="mt-3 flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!idRight || !secretRight) return;
            onSave(id, hidden);
            setClientId(""); // neither part is kept on the page after it is sent
            setSecret("");
          }}
        >
          <label className={field}>
            <span className="text-sm text-muted">Client ID</span>
            <input
              type="password"
              value={clientId}
              onChange={(event) => {
                setClientId(event.target.value);
              }}
              autoComplete="off"
              spellCheck={false}
              placeholder="….access"
              className={input}
            />
          </label>
          <label className={field}>
            <span className="text-sm text-muted">Client Secret</span>
            <input
              type="password"
              value={secret}
              onChange={(event) => {
                setSecret(event.target.value);
              }}
              autoComplete="off"
              spellCheck={false}
              placeholder="Client Secret"
              className={input}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !idRight || !secretRight}
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-on-brand hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Fingerprint className="size-4" aria-hidden="true" />
            {status.saved ? "Replace token" : "Save token"}
          </button>
          {id.length > 0 && !idRight && (
            <span className="w-full text-sm text-muted">The Client ID ends with {ID_SUFFIX} and has no spaces.</span>
          )}
          {hidden.length > 0 && !secretRight && (
            <span className="w-full text-sm text-muted">
              The Client Secret has no spaces and does not end with {ID_SUFFIX} (are the two swapped?).
            </span>
          )}
        </form>
      )}
    </section>
  );
}
