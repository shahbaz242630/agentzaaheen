import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RegistryPage, ServerTokenStatus } from "../api";
import sample from "../contract/sample.json";
import { CustomizeView } from "./CustomizeView";

const page = { ...(sample.registry as RegistryPage), switches: true };
const ID = "0".repeat(32) + ".access"; // built here, never a real token
const SECRET = "f".repeat(64);
const SIGNED = { id: "cred", response: { signature: "sig" } };
const NONE: ServerTokenStatus = {
  switches: true,
  vault: true,
  saved: false,
  saved_at: null,
};
const SAVED: ServerTokenStatus = {
  ...NONE,
  saved: true,
  saved_at: new Date().toISOString(),
};
const NO_CLAUDE = {
  switches: false,
  vault: false,
  saved: false,
  saved_at: null,
};

type Route = [status: number, body: unknown];

// A pretend server: GETs answer the passkey and the token's status (the Claude key's panel stays hidden);
// each POST answers the next reply queued for it.
function serve(start: ServerTokenStatus, replies: Record<string, Route[]> = {}, enrolled = true) {
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    if (init?.method !== "POST") {
      const gets: Record<string, unknown> = {
        "/api/passkey": { enrolled, enrolment_open: false },
        "/api/claude-key": NO_CLAUDE,
      };
      return Promise.resolve(new Response(JSON.stringify(gets[path] ?? start), { status: 200 }));
    }
    const [status, body] = replies[path]?.shift() ?? [404, { detail: "nope" }];
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function posts(fetch: ReturnType<typeof serve>) {
  return (fetch.mock.calls as unknown as [string, RequestInit | undefined][])
    .filter(([, init]) => init?.method === "POST")
    .map(([path, init]) => ({
      path,
      body: JSON.parse(init?.body as string) as Record<string, unknown>,
    }));
}

// A pretend Windows Hello at localhost: it signs with a fixed answer, or the person closes it.
function windowsHello(outcome: "signs" | "cancels" = "signs") {
  class FakeCredential {
    toJSON() {
      return SIGNED;
    }
  }
  const credentials = {
    get: vi.fn(() =>
      outcome === "signs"
        ? Promise.resolve(new FakeCredential())
        : Promise.reject(new DOMException("closed", "NotAllowedError")),
    ),
  };
  vi.stubGlobal(
    "PublicKeyCredential",
    Object.assign(FakeCredential, {
      parseCreationOptionsFromJSON: (o: unknown) => ({ parsed: o }),
      parseRequestOptionsFromJSON: (o: unknown) => ({ parsed: o }),
    }),
  );
  vi.stubGlobal("navigator", { credentials });
  vi.stubGlobal("location", { hostname: "localhost", port: "8765" });
  return credentials;
}

async function show() {
  render(<CustomizeView page={page} projectName={(id) => id} onChange={vi.fn()} />);
  await userEvent.click(screen.getByRole("tab", { name: /Passkeys/ }));
}

async function paste(id: string, secret: string) {
  if (id) await userEvent.type(await screen.findByPlaceholderText("….access"), id);
  if (secret) await userEvent.type(screen.getByPlaceholderText("Client Secret"), secret);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the server access token on the Customize page", () => {
  it("saving asks Windows Hello to sign that exact save, then says only that a token is saved", async () => {
    const credentials = windowsHello();
    const fetch = serve(NONE, {
      "/api/server-token": [
        [409, { detail: "server_token.needs_passkey" }],
        [200, SAVED],
      ],
      "/api/server-token/options": [[200, { options: { challenge: "c" } }]],
    });
    await show();
    expect(await screen.findByText(/No token saved yet/)).toBeInTheDocument();
    await paste(ID, SECRET);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByText(/sealed in the vault/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace token" })).toBeInTheDocument();
    expect(credentials.get).toHaveBeenCalledTimes(1);
    const both = { client_id: ID, client_secret: SECRET };
    expect(posts(fetch)).toEqual([
      { path: "/api/server-token", body: both },
      { path: "/api/server-token/options", body: both },
      { path: "/api/server-token", body: { ...both, credential: SIGNED } },
    ]);
    expect(screen.getByPlaceholderText("….access")).toHaveValue(""); // neither part kept on the page
    expect(screen.getByPlaceholderText("Client Secret")).toHaveValue("");
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("both fields hide what is typed", async () => {
    windowsHello();
    serve(NONE);
    await show();
    expect(await screen.findByPlaceholderText("….access")).toHaveAttribute("type", "password");
    expect(screen.getByPlaceholderText("Client Secret")).toHaveAttribute("type", "password");
  });

  it("a Client ID without .access cannot be sent", async () => {
    windowsHello();
    const fetch = serve(NONE);
    await show();
    await paste("abc123", SECRET);
    expect(screen.getByRole("button", { name: "Save token" })).toBeDisabled();
    expect(screen.getByText(/The Client ID ends with .access/)).toBeInTheDocument();
    await userEvent.type(screen.getByPlaceholderText("Client Secret"), "{Enter}"); // Enter submits the form itself
    expect(posts(fetch)).toEqual([]);
  });

  it("swapped parts cannot be sent", async () => {
    windowsHello();
    const fetch = serve(NONE);
    await show();
    await paste(ID, ID);
    expect(screen.getByRole("button", { name: "Save token" })).toBeDisabled();
    expect(screen.getByText(/are the two swapped/)).toBeInTheDocument();
    expect(posts(fetch)).toEqual([]);
  });

  it("a missing secret cannot be sent", async () => {
    windowsHello();
    serve(NONE);
    await show();
    await paste(ID, "");
    expect(screen.getByRole("button", { name: "Save token" })).toBeDisabled();
  });

  it("closing Windows Hello saves nothing and says so", async () => {
    windowsHello("cancels");
    serve(NONE, {
      "/api/server-token": [[409, { detail: "server_token.needs_passkey" }]],
      "/api/server-token/options": [[200, { options: { challenge: "c" } }]],
    });
    await show();
    await paste(ID, SECRET);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Windows Hello was closed before confirming/);
    expect(screen.getByText(/No token saved yet/)).toBeInTheDocument();
  });

  it("a refused token says what is wrong in plain words", async () => {
    windowsHello();
    serve(NONE, {
      "/api/server-token": [[409, { detail: "server_token.bad_token" }]],
    });
    await show();
    await paste(ID, SECRET);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/doesn't look like a Cloudflare service token/);
  });

  it("without the vault's master key offers no fields", async () => {
    windowsHello();
    serve({ ...NONE, vault: false });
    await show();
    expect(await screen.findByText(/The vault needs its master key first/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("….access")).not.toBeInTheDocument();
  });

  it("without a passkey asks for one first", async () => {
    windowsHello();
    serve(NONE, {}, false);
    await show();
    expect(await screen.findByText(/saving the token needs Windows Hello/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("….access")).not.toBeInTheDocument();
  });

  it("without a database the panel is not shown", async () => {
    windowsHello();
    serve({ ...NONE, switches: false });
    await show();
    expect(await screen.findByRole("heading", { name: "Customize" })).toBeInTheDocument();
    expect(screen.queryByText("Server access token")).not.toBeInTheDocument();
  });
});
