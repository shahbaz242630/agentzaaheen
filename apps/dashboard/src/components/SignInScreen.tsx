// The sign-in screen (ADR-SEC-010): one Windows Hello touch opens the dashboard. Shown when the
// dashboard asks for sign-in and this browser has none (never signed in, signed out, 3 hours idle,
// 12 hours reached, or the dashboard restarted). With no passkey yet, or a lost one, the passkey is
// set up here with the code printed at start, and that signs in too.
import { Fingerprint, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";

import { ApiError, api, type PasskeyStatus } from "../api";
import { PASSKEY_PROBLEM, PasskeyProblem, onPasskeyAddress, setUpPasskey, signInWithPasskey } from "../passkey";
import { PasskeyPanel } from "./PasskeyPanel";

const SIGN_IN_PROBLEM: Record<string, string> = {
  "session.sign_in_failed": "That passkey was not accepted, so you are not signed in. Try again.",
  "session.wait": "Too many tries that failed. Sign-in waits one minute; then try again.",
  "registry.no_passkey": "There is no passkey yet. Set one up below with the code shown at start.",
  "session.store_unavailable": "The laptop database is not answering, so sign-in can't be checked. Is it running?",
};

function explain(failure: unknown): string {
  if (failure instanceof PasskeyProblem) return PASSKEY_PROBLEM[failure.reason];
  const code = failure instanceof ApiError ? failure.code : null;
  return (code && SIGN_IN_PROBLEM[code]) ?? "Sign-in did not work. Try again.";
}

interface Props {
  // Why the screen is showing: a sign-in that ended says so, a first visit does not.
  ended: boolean;
  onSignedIn: () => void;
}

export function SignInScreen({ ended, onSignedIn }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [passkey, setPasskey] = useState<PasskeyStatus | null>(null);
  const onAddress = onPasskeyAddress();

  useEffect(() => {
    let live = true;
    api.passkey().then(
      (status) => {
        if (live) setPasskey(status);
      },
      () => {
        if (live) setPasskey(null);
      },
    );
    return () => {
      live = false;
    };
  }, []);

  function signIn() {
    setBusy(true);
    setError(null);
    signInWithPasskey().then(onSignedIn, (failure: unknown) => {
      setError(explain(failure));
      setBusy(false);
    });
  }

  function setUp(code: string) {
    setBusy(true);
    setError(null);
    setUpPasskey(code).then(onSignedIn, (failure: unknown) => {
      setError(explain(failure));
      setBusy(false);
    });
  }

  const port = window.location.port ? `:${window.location.port}` : "";
  return (
    <div className="flex h-full items-center justify-center bg-canvas p-5">
      <main className="w-full max-w-md space-y-4">
        <section
          aria-labelledby="sign-in-title"
          className="rounded-2xl border border-line bg-panel p-6 shadow-[0_1px_2px_oklch(0_0_0/0.04)]"
        >
          <div className="flex items-center gap-2.5">
            <img src="/favicon.svg" alt="" className="size-7" />
            <h1 id="sign-in-title" className="text-lg font-semibold tracking-tight text-ink">
              Sign in to Agent Zaaheen
            </h1>
          </div>
          <p className="mt-2 text-sm text-muted">
            {ended
              ? "You were signed out: after 3 hours with nothing done, after 12 hours, by Sign out, or because the dashboard restarted."
              : "Confirm it's you with Windows Hello to open the dashboard."}
          </p>
          {onAddress ? (
            <button
              type="button"
              onClick={signIn}
              disabled={busy || passkey?.enrolled === false}
              className="mt-4 inline-flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-on-brand hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Fingerprint className="size-4" aria-hidden="true" />
              {busy ? "Waiting for Windows Hello…" : "Sign in with Windows Hello"}
            </button>
          ) : (
            <p className="mt-4 flex items-start gap-2 rounded-xl border border-warn bg-warn-soft px-4 py-3 text-sm text-ink">
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
              <span>
                Sign-in only works at{" "}
                <a className="font-medium underline" href={`http://localhost${port}/`}>
                  http://localhost{port}
                </a>
                . Open the dashboard there.
              </span>
            </p>
          )}
          {error && (
            <p role="alert" className="mt-3 text-sm text-alert">
              {error}
            </p>
          )}
        </section>
        {onAddress && passkey && (passkey.enrolment_open || !passkey.enrolled) && (
          <PasskeyPanel status={passkey} onAddress busy={busy} onSetUp={setUp} />
        )}
      </main>
    </div>
  );
}
