// Windows Hello through the browser's own WebAuthn (ADR-SEC-007, ADR-SEC-010). The server
// makes the options and checks the answers; this file only hands them over.
// It uses the browser's JSON helpers (Chrome and Edge 129+), so no library.
import { ApiError, api, type RegistryPage } from "./api";

export class PasskeyProblem extends Error {
  constructor(readonly reason: "unsupported" | "wrong_address" | "cancelled") {
    super(reason);
  }
}

// What each problem means, in the Chairman's words (Customize and the sign-in screen).
export const PASSKEY_PROBLEM: Record<PasskeyProblem["reason"], string> = {
  unsupported: "This browser can't use passkeys. Use Chrome or Edge (version 129 or newer).",
  wrong_address: "Passkeys only work when the dashboard is opened at localhost.",
  cancelled:
    "Windows Hello was closed before confirming, so nothing changed. If Bitwarden or another password manager opened instead, choose its option to use another device.",
};

// Passkeys need a name, not an IP: the page must be opened at localhost.
export function onPasskeyAddress(): boolean {
  return window.location.hostname === "localhost";
}

function supported(): boolean {
  return (
    typeof PublicKeyCredential !== "undefined" &&
    typeof PublicKeyCredential.parseCreationOptionsFromJSON === "function" &&
    typeof PublicKeyCredential.parseRequestOptionsFromJSON === "function"
  );
}

function ready(): void {
  if (!onPasskeyAddress()) throw new PasskeyProblem("wrong_address");
  if (!supported()) throw new PasskeyProblem("unsupported");
}

async function windowsHello<T>(ask: () => Promise<T>): Promise<T> {
  try {
    return await ask();
  } catch (failure) {
    // The person closed the Windows Hello box, or it timed out.
    if (failure instanceof DOMException && failure.name === "NotAllowedError") throw new PasskeyProblem("cancelled");
    throw failure;
  }
}

function asJson(credential: Credential | null): unknown {
  if (!(credential instanceof PublicKeyCredential)) throw new PasskeyProblem("cancelled");
  return credential.toJSON();
}

// Set up the Chairman's passkey on this laptop (once), using the code printed at start.
export async function setUpPasskey(code: string) {
  ready();
  const options = await api.enrolOptions(code);
  const publicKey = PublicKeyCredential.parseCreationOptionsFromJSON(
    options as unknown as PublicKeyCredentialCreationOptionsJSON,
  );
  const credential = await windowsHello(() => navigator.credentials.create({ publicKey }));
  return api.enrol(code, asJson(credential));
}

// Approve one request: Windows Hello signs a challenge made for that request only.
export async function approveWithPasskey(requestId: string): Promise<RegistryPage> {
  ready();
  const options = await api.approveOptions(requestId);
  const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(
    options as unknown as PublicKeyCredentialRequestOptionsJSON,
  );
  const credential = await windowsHello(() => navigator.credentials.get({ publicKey }));
  return api.approve(requestId, asJson(credential));
}

// Sign in to the dashboard (ADR-SEC-010): Windows Hello signs a challenge made for signing in only.
export async function signInWithPasskey() {
  ready();
  const options = await api.signInOptions();
  const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(
    options as unknown as PublicKeyCredentialRequestOptionsJSON,
  );
  const credential = await windowsHello(() => navigator.credentials.get({ publicKey }));
  return api.signIn(asJson(credential));
}

// A change that may need the Chairman's passkey: try it; if the server says it is riskier
// ("agents.needs_passkey", "workspaces.needs_passkey"), have Windows Hello sign a challenge made for
// exactly this change, then send it again.
export async function withPasskeyIfNeeded<T>(
  attempt: () => Promise<T>,
  options: () => Promise<Record<string, unknown>>,
  signed: (credential: unknown) => Promise<T>,
): Promise<T> {
  try {
    return await attempt();
  } catch (failure) {
    if (!(failure instanceof ApiError) || !failure.code?.endsWith(".needs_passkey")) throw failure;
  }
  ready();
  const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(
    (await options()) as unknown as PublicKeyCredentialRequestOptionsJSON,
  );
  const credential = await windowsHello(() => navigator.credentials.get({ publicKey }));
  return signed(asJson(credential));
}
