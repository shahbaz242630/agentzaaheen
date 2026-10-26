import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelKeyStatus, RegistryPage } from "../api";
import sample from "../contract/sample.json";
import { CustomizeView } from "./CustomizeView";

const page = { ...(sample.registry as RegistryPage), switches: true };
const KEY = "a1".repeat(32); // built here, never the real one
const SIGNED = { id: "cred", response: { signature: "sig" } };
const NONE: ModelKeyStatus = { switches: true, vault: true, saved: false, saved_at: null };
const SAVED: ModelKeyStatus = { ...NONE, saved: true, saved_at: new Date().toISOString() };

type Route = [status: number, body: unknown];

// A pretend server: GETs answer the passkey and every key's status; each POST answers the next reply queued for it.
function serve(start: ModelKeyStatus, replies: Record<string, Route[]> = {}, enrolled = true) {
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

describe("our server model's key on the Customize page", () => {
  it("saving asks Windows Hello to sign that exact save, then says only that a key is saved", async () => {
    const credentials = windowsHello();
    const fetch = serve(NONE, {
      "/api/model-key": [
        [409, { detail: "model_key.needs_passkey" }],
        [200, SAVED],
      ],
      "/api/model-key/options": [[200, { options: { challenge: "c" } }]],
    });
    show();
    expect(await screen.findByRole("heading", { name: "Server model key" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Model key"), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Save model key" }));
    expect(await screen.findByText(/locked in the vault/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace model key" })).toBeInTheDocument();
    expect(credentials.get).toHaveBeenCalledTimes(1);
    expect(posts(fetch)).toEqual([
      { path: "/api/model-key", body: { key: KEY } },
      { path: "/api/model-key/options", body: { key: KEY } },
      { path: "/api/model-key", body: { key: KEY, credential: SIGNED } },
    ]);
    expect(screen.getByLabelText("Model key")).toHaveValue(""); // not kept on the page
    expect(document.body.textContent).not.toContain(KEY);
  });

  it("the key field hides what is typed", async () => {
    windowsHello();
    serve(NONE);
    show();
    expect(await screen.findByLabelText("Model key")).toHaveAttribute("type", "password");
  });

  it("something too short cannot be sent", async () => {
    windowsHello();
    const fetch = serve(NONE);
    show();
    await userEvent.type(await screen.findByLabelText("Model key"), "abc123");
    expect(screen.getByRole("button", { name: "Save model key" })).toBeDisabled();
    expect(screen.getByText(/32 or more letters and digits/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Model key"), "{Enter}"); // Enter submits the form itself
    expect(posts(fetch)).toEqual([]);
  });

  it("the Claude key pasted in this box is caught and not sent", async () => {
    windowsHello();
    const fetch = serve(NONE);
    show();
    await userEvent.type(await screen.findByLabelText("Model key"), "sk-ant-api03-" + "k".repeat(40));
    expect(screen.getByRole("button", { name: "Save model key" })).toBeDisabled();
    expect(screen.getByText(/That is a Claude key/)).toBeInTheDocument();
    expect(posts(fetch)).toEqual([]);
  });

  it("closing Windows Hello saves nothing and says so", async () => {
    windowsHello("cancels");
    serve(NONE, {
      "/api/model-key": [[409, { detail: "model_key.needs_passkey" }]],
      "/api/model-key/options": [[200, { options: { challenge: "c" } }]],
    });
    show();
    await userEvent.type(await screen.findByLabelText("Model key"), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Save model key" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Windows Hello was closed before confirming/);
    expect(screen.getByText(/Not saved yet/)).toBeInTheDocument();
  });

  it("a key the server refuses is explained in plain words", async () => {
    windowsHello();
    serve(NONE, { "/api/model-key": [[409, { detail: "model_key.bad_key" }]] });
    show();
    await userEvent.type(await screen.findByLabelText("Model key"), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Save model key" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/doesn't look like our server model's key/);
  });

  it("without the vault's master key offers no field", async () => {
    windowsHello();
    serve({ ...NONE, vault: false });
    show();
    expect(await screen.findByText(/Set up the vault's master key first/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Model key")).not.toBeInTheDocument();
  });

  it("without a passkey asks for one first", async () => {
    windowsHello();
    serve(NONE, {}, false);
    show();
    expect(await screen.findByText(/the model key is saved with Windows Hello/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Model key")).not.toBeInTheDocument();
  });

  it("without a database the panel is not shown", async () => {
    windowsHello();
    serve({ ...NONE, switches: false });
    show();
    expect(await screen.findByRole("heading", { name: "Customize" })).toBeInTheDocument();
    expect(screen.queryByText("Server model key")).not.toBeInTheDocument();
  });
});
