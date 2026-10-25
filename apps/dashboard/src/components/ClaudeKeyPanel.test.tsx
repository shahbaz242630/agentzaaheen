import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ClaudeKeyStatus, RegistryPage } from "../api";
import sample from "../contract/sample.json";
import { CustomizeView } from "./CustomizeView";

const page = { ...(sample.registry as RegistryPage), switches: true };
const KEY = "sk-ant-api03-" + "k".repeat(40); // built here, never a real key
const SIGNED = { id: "cred", response: { signature: "sig" } };
const NONE: ClaudeKeyStatus = { switches: true, vault: true, saved: false, saved_at: null };
const SAVED: ClaudeKeyStatus = { ...NONE, saved: true, saved_at: new Date().toISOString() };

type Route = [status: number, body: unknown];

// A pretend server: GETs answer the passkey and the key's status; each POST answers the next reply queued for it.
function serve(start: ClaudeKeyStatus, replies: Record<string, Route[]> = {}, enrolled = true) {
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    if (init?.method !== "POST") {
      const body = path === "/api/passkey" ? { enrolled, enrolment_open: false } : start;
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
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
    .map(([path, init]) => ({ path, body: JSON.parse(init?.body as string) as Record<string, unknown> }));
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

function show() {
  render(<CustomizeView page={page} projectName={(id) => id} onChange={vi.fn()} />);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the Claude key on the Customize page", () => {
  it("saving asks Windows Hello to sign that exact save, then says only that a key is saved", async () => {
    const credentials = windowsHello();
    const fetch = serve(NONE, {
      "/api/claude-key": [
        [409, { detail: "claude_key.needs_passkey" }],
        [200, SAVED],
      ],
      "/api/claude-key/options": [[200, { options: { challenge: "c" } }]],
    });
    show();
    expect(await screen.findByText(/No key saved yet/)).toBeInTheDocument();
    await userEvent.type(screen.getByPlaceholderText("sk-ant-…"), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Save key" }));
    expect(await screen.findByText(/sealed in the vault/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace key" })).toBeInTheDocument();
    expect(credentials.get).toHaveBeenCalledTimes(1);
    expect(posts(fetch)).toEqual([
      { path: "/api/claude-key", body: { key: KEY } },
      { path: "/api/claude-key/options", body: { key: KEY } },
      { path: "/api/claude-key", body: { key: KEY, credential: SIGNED } },
    ]);
    expect(screen.getByPlaceholderText("sk-ant-…")).toHaveValue(""); // not kept on the page
    expect(document.body.textContent).not.toContain(KEY);
  });

  it("the key field hides what is typed", async () => {
    windowsHello();
    serve(NONE);
    show();
    expect(await screen.findByPlaceholderText("sk-ant-…")).toHaveAttribute("type", "password");
  });

  it("something that is not a Claude key cannot be sent", async () => {
    windowsHello();
    const fetch = serve(NONE);
    show();
    await userEvent.type(await screen.findByPlaceholderText("sk-ant-…"), "sk-proj-abc");
    expect(screen.getByRole("button", { name: "Save key" })).toBeDisabled();
    expect(screen.getByText(/start with sk-ant- and have no spaces/)).toBeInTheDocument();
    await userEvent.type(screen.getByPlaceholderText("sk-ant-…"), "{Enter}"); // Enter submits the form itself
    expect(posts(fetch)).toEqual([]);
  });

  it("closing Windows Hello saves nothing and says so", async () => {
    windowsHello("cancels");
    serve(NONE, {
      "/api/claude-key": [[409, { detail: "claude_key.needs_passkey" }]],
      "/api/claude-key/options": [[200, { options: { challenge: "c" } }]],
    });
    show();
    await userEvent.type(await screen.findByPlaceholderText("sk-ant-…"), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Save key" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Windows Hello was closed before confirming/);
    expect(screen.getByText(/No key saved yet/)).toBeInTheDocument();
  });

  it("without the vault's master key says how to make it, and offers no field", async () => {
    windowsHello();
    serve({ ...NONE, vault: false });
    show();
    expect(await screen.findByText(/tools.vault_key make-key/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("sk-ant-…")).not.toBeInTheDocument();
  });

  it("without a passkey asks for one first", async () => {
    windowsHello();
    serve(NONE, {}, false);
    show();
    expect(await screen.findByText(/Set up your passkey first: saving the key needs Windows Hello/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("sk-ant-…")).not.toBeInTheDocument();
  });
});
