// Signing in to the dashboard (ADR-SEC-010): nothing shows before Windows Hello, Sign out closes it,
// and the server's "sign in first" (3 hours idle, 12 hours, a restart) brings the sign-in screen back.
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import sample from "./contract/sample.json";

const SIGNED = { id: "signed-answer" };

interface Server {
  signedIn: boolean;
  required: boolean;
  enrolled: boolean;
  // Set to make the next sign-in fail with this code.
  refuse: string | null;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

// A pretend dashboard server: every route but the open ones answers 401 until signed in.
function serve(server: Server) {
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    const post = init?.method === "POST";
    if (path === "/api/session" && !post) {
      return Promise.resolve(json(200, { required: server.required, signed_in: server.signedIn }));
    }
    if (path === "/api/passkey") {
      return Promise.resolve(json(200, { enrolled: server.enrolled, enrolment_open: false }));
    }
    if (path === "/api/session/options") return Promise.resolve(json(200, { options: { challenge: "c" } }));
    if (path === "/api/session" && post) {
      if (server.refuse) {
        return Promise.resolve(json(server.refuse === "session.wait" ? 429 : 403, { detail: server.refuse }));
      }
      server.signedIn = true;
      return Promise.resolve(json(200, { required: true, signed_in: true }));
    }
    if (path === "/api/session/sign-out") {
      server.signedIn = false;
      return Promise.resolve(json(200, { required: true, signed_in: false }));
    }
    if (server.required && !server.signedIn) return Promise.resolve(json(401, { detail: "session.needed" }));
    if (path === "/api/workspaces") return Promise.resolve(json(200, sample.projects));
    if (path === "/api/models") return Promise.resolve(json(200, sample.models));
    if (path === "/api/registry") return Promise.resolve(json(200, sample.registry));
    if (path === "/api/summaries") return Promise.resolve(json(200, { connected: false, workspaces: {} }));
    if (path === "/api/briefings") return Promise.resolve(json(200, { connected: false, briefings: [] }));
    return Promise.resolve(json(404, { detail: "not.found" }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
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

function calls(fetch: ReturnType<typeof serve>) {
  return (fetch.mock.calls as unknown as [string, RequestInit | undefined][]).map(([path, init]) => ({
    path,
    method: init?.method ?? "GET",
    headers: (init?.headers ?? {}) as Record<string, string>,
  }));
}

const server = (over: Partial<Server> = {}): Server => ({
  signedIn: false,
  required: true,
  enrolled: true,
  refuse: null,
  ...over,
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("signing in", () => {
  it("shows only the sign-in screen until Windows Hello confirms, then the dashboard", async () => {
    const fetch = serve(server());
    const hello = windowsHello();
    render(<App />);
    const button = await screen.findByRole("button", { name: "Sign in with Windows Hello" });
    expect(screen.queryByRole("heading", { name: "The Board" })).not.toBeInTheDocument();
    expect(screen.getByText(/Confirm it's you/)).toBeInTheDocument();

    await userEvent.click(button);
    expect(await screen.findByRole("heading", { level: 1, name: "The Board" })).toBeInTheDocument();
    expect(hello.get).toHaveBeenCalledTimes(1);
    expect(hello.get).toHaveBeenCalledWith({ publicKey: { parsed: { challenge: "c" } } }); // the server's challenge
    const signIn = calls(fetch).find((c) => c.path === "/api/session" && c.method === "POST");
    expect(signIn).toBeDefined();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("opens straight on the dashboard when this browser is already signed in", async () => {
    serve(server({ signedIn: true }));
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "The Board" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("asks nothing and shows no Sign out where sign-in is not needed", async () => {
    serve(server({ required: false }));
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "The Board" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("signs out with the button and shows the sign-in screen again", async () => {
    const state = server({ signedIn: true });
    const fetch = serve(state);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: "Sign in with Windows Hello" })).toBeInTheDocument();
    expect(calls(fetch).some((c) => c.path === "/api/session/sign-out" && c.method === "POST")).toBe(true);
    expect(state.signedIn).toBe(false);
  });

  it("brings the sign-in screen back, saying why, when the server says the sign-in ended", async () => {
    const state = server({ signedIn: true });
    serve(state);
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "The Board" });
    state.signedIn = false; // 3 hours idle on the server
    const nav = screen.getByRole("navigation", { name: "Workspaces" });
    await userEvent.click(within(nav).getByRole("button", { name: /Zaaheen/ }));
    expect(await screen.findByRole("button", { name: "Sign in with Windows Hello" })).toBeInTheDocument();
    expect(screen.getByText(/You were signed out/)).toBeInTheDocument();
  });

  it("explains a refused passkey and the one-minute wait, and stays on the sign-in screen", async () => {
    const state = server({ refuse: "session.sign_in_failed" });
    serve(state);
    windowsHello();
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign in with Windows Hello" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That passkey was not accepted");
    state.refuse = "session.wait";
    await userEvent.click(screen.getByRole("button", { name: "Sign in with Windows Hello" }));
    expect(await screen.findByText(/Sign-in waits one minute/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "The Board" })).not.toBeInTheDocument();
  });

  it("says nothing changed when Windows Hello is closed", async () => {
    serve(server());
    windowsHello("cancels");
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign in with Windows Hello" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Windows Hello was closed");
  });

  it("with no passkey yet, offers the set-up instead of a sign-in that can't work", async () => {
    serve(server({ enrolled: false }));
    windowsHello();
    render(<App />);
    const button = await screen.findByRole("button", { name: "Sign in with Windows Hello" });
    expect(await screen.findByText(/No passkey yet/)).toBeInTheDocument();
    expect(button).toBeDisabled();
  });

  it("at 127.0.0.1 points to localhost, where passkeys work", async () => {
    serve(server());
    vi.stubGlobal("location", { hostname: "127.0.0.1", port: "8765" });
    render(<App />);
    expect(await screen.findByRole("link", { name: "http://localhost:8765" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in with Windows Hello" })).not.toBeInTheDocument();
  });

  it("marks the page's own calls as the Chairman's actions", async () => {
    const fetch = serve(server({ signedIn: true }));
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "The Board" });
    const reads = calls(fetch).filter((c) => c.path === "/api/workspaces");
    expect(reads.length).toBeGreaterThan(0);
    expect(reads.every((c) => c.headers["X-AgentZaaheen-Active"] === "1")).toBe(true);
  });
});
