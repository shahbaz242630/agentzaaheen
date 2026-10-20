import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PasskeyStatus, RegistryPage } from "../api";
import sample from "../contract/sample.json";
import { CustomizeView } from "./CustomizeView";

const base = sample.registry as RegistryPage;
const page: RegistryPage = { ...base, switches: true };
const REQUEST_ID = "6f1c7a52-3b9e-4d0a-9a43-1f6f1d2b8c11";
const skillOff: RegistryPage = { ...page, skills: page.skills.map((s) => ({ ...s, state: "off" as const })) };
const waiting: RegistryPage = {
  ...skillOff,
  requests: [
    { id: REQUEST_ID, kind: "skill", item_id: "triage-failing-build", requested_at: "2026-10-20T12:00:00Z", stale: false },
  ],
};
const ENROLLED: PasskeyStatus = { enrolled: true, enrolment_open: false };

type Route = [status: number, body: unknown];

// A pretend server: GET /api/passkey answers ``passkey``; each POST path answers from ``routes``.
function serve(routes: Record<string, Route>, passkey: PasskeyStatus = ENROLLED) {
  const fetch = vi.fn((path: string) => {
    const [status, body] = path === "/api/passkey" ? [200, passkey] : (routes[path] ?? [404, { detail: "nope" }]);
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function posts(fetch: ReturnType<typeof serve>) {
  return (fetch.mock.calls as unknown as [string, RequestInit | undefined][])
    .filter(([, init]) => init?.method === "POST")
    .map(([path, init]) => ({ path, init: init as RequestInit, body: JSON.parse(init?.body as string) as unknown }));
}

// A pretend Windows Hello: the browser's WebAuthn calls, answering with a fixed credential (or a cancel).
function windowsHello(outcome: "signs" | "cancels" = "signs") {
  class FakeCredential {
    toJSON() {
      return { id: "cred", response: { signature: "sig" } };
    }
  }
  const credentials = {
    create: vi.fn(() => Promise.resolve(new FakeCredential())),
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
  // Only ``credentials`` is read by the code under test.
  vi.stubGlobal("navigator", { credentials });
  return credentials;
}

function stubAddress(hostname: string) {
  // Only ``hostname`` and ``port`` are read by the code under test.
  vi.stubGlobal("location", { hostname, port: "8765" });
}

function atLocalhost() {
  stubAddress("localhost");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function view(shown: RegistryPage, onChange = vi.fn()) {
  render(<CustomizeView page={shown} projectName={(id) => id} onChange={onChange} />);
  return onChange;
}

describe("the registry switches", () => {
  it("no longer shows the one-way heads-up: off can be undone by approval", async () => {
    serve({});
    view(page);
    expect(await screen.findByText(/Your passkey is set up/)).toBeInTheDocument();
    expect(screen.queryByText(/Heads up/)).not.toBeInTheDocument();
  });

  it("asks before switching off, and 'Keep it on' sends nothing", async () => {
    const fetch = serve({});
    view(page);
    const paddle = screen.getByRole("article", { name: "Paddle" });
    expect(paddle).toHaveTextContent("On");
    await userEvent.click(within(paddle).getByRole("button", { name: "Switch off" }));
    expect(within(paddle).getByRole("group", { name: "Switch Paddle off?" })).toHaveTextContent("with your passkey");
    await userEvent.click(within(paddle).getByRole("button", { name: "Keep it on" }));
    expect(posts(fetch)).toEqual([]);
  });

  it("switches off over our own guarded POST and shows the new page", async () => {
    const fetch = serve({ "/api/registry/switch-off": [200, skillOff] });
    const onChange = view(page);
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    const triage = screen.getByRole("article", { name: "Triage failing build" });
    await userEvent.click(within(triage).getByRole("button", { name: "Switch off" }));
    await userEvent.click(within(triage).getByRole("button", { name: "Switch off" }));
    const [sent] = posts(fetch);
    expect(sent?.path).toBe("/api/registry/switch-off");
    expect(sent?.init.headers).toMatchObject({ "X-Yaqzan-Request": "1", "Content-Type": "application/json" });
    expect(sent?.body).toEqual({ kind: "skill", id: "triage-failing-build" });
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(skillOff);
    });
  });

  it("lists what is off under Discover with 'Ask to switch on', which files a request", async () => {
    const fetch = serve({ "/api/registry/requests": [200, waiting] });
    const onChange = view(skillOff);
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    expect(screen.getByText("No skills are on.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    const triage = screen.getByRole("article", { name: "Triage failing build" });
    await userEvent.click(within(triage).getByRole("button", { name: "Ask to switch on" }));
    expect(posts(fetch)[0]?.body).toEqual({ kind: "skill", id: "triage-failing-build" });
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(waiting);
    });
  });

  it("approves with Windows Hello: options for that request, a signature, then the new page", async () => {
    atLocalhost();
    const hello = windowsHello();
    const fetch = serve({
      [`/api/registry/requests/${REQUEST_ID}/approve/options`]: [200, { options: { challenge: "abc" } }],
      [`/api/registry/requests/${REQUEST_ID}/approve`]: [200, page],
    });
    const onChange = view(waiting);
    const list = await screen.findByRole("region", { name: "Waiting for approval" });
    await vi.waitFor(() => {
      expect(within(list).getByRole("button", { name: /^Approve/ })).toBeEnabled();
    });
    await userEvent.click(within(list).getByRole("button", { name: /^Approve/ }));
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(page);
    });
    expect(hello.get).toHaveBeenCalledWith({ publicKey: { parsed: { challenge: "abc" } } });
    expect(posts(fetch).map((p) => p.path)).toEqual([
      `/api/registry/requests/${REQUEST_ID}/approve/options`,
      `/api/registry/requests/${REQUEST_ID}/approve`,
    ]);
    expect(posts(fetch)[1]?.body).toEqual({ credential: { id: "cred", response: { signature: "sig" } } });
  });

  it("says so plainly when Windows Hello is closed, and changes nothing", async () => {
    atLocalhost();
    windowsHello("cancels");
    const fetch = serve({
      [`/api/registry/requests/${REQUEST_ID}/approve/options`]: [200, { options: { challenge: "abc" } }],
    });
    const onChange = view(waiting);
    const approve = await screen.findByRole("button", { name: /^Approve/ });
    await vi.waitFor(() => {
      expect(approve).toBeEnabled();
    });
    await userEvent.click(approve);
    expect(await screen.findByRole("alert")).toHaveTextContent("Windows Hello was closed");
    expect(onChange).not.toHaveBeenCalled();
    expect(posts(fetch).map((p) => p.path)).not.toContain(`/api/registry/requests/${REQUEST_ID}/approve`);
  });

  it("refuses without a passkey, and withdraws", async () => {
    const fetch = serve({
      [`/api/registry/requests/${REQUEST_ID}/refuse`]: [200, skillOff],
      [`/api/registry/requests/${REQUEST_ID}/withdraw`]: [200, skillOff],
    });
    view(waiting);
    const list = screen.getByRole("region", { name: "Waiting for approval" });
    await userEvent.click(within(list).getByRole("button", { name: /^Refuse/ }));
    await vi.waitFor(() => {
      expect(posts(fetch).map((p) => p.path)).toContain(`/api/registry/requests/${REQUEST_ID}/refuse`);
    });
    await userEvent.click(within(list).getByRole("button", { name: /Withdraw/ }));
    await vi.waitFor(() => {
      expect(posts(fetch).map((p) => p.path)).toContain(`/api/registry/requests/${REQUEST_ID}/withdraw`);
    });
  });

  it("cannot approve without a passkey, at the wrong address, or when the files changed", async () => {
    serve({}, { enrolled: false, enrolment_open: false });
    view(waiting);
    expect(await screen.findByText(/No passkey yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Approve/ })).toBeDisabled();
    vi.unstubAllGlobals();
    cleanupAndRender({ ...waiting, requests: waiting.requests.map((r) => ({ ...r, stale: true })) });
    expect(await screen.findByText(/Its files changed since you asked/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Approve/ })).toBeDisabled();
  });

  it("tells a page opened at 127.0.0.1 to use localhost", async () => {
    serve({});
    stubAddress("127.0.0.1");
    view(waiting);
    const link = await screen.findByRole("link", { name: "http://localhost:8765" });
    expect(link).toHaveAttribute("href", "http://localhost:8765/");
    expect(screen.getByRole("button", { name: /^Approve/ })).toBeDisabled();
  });

  it("sets up the passkey with the start-up code and Windows Hello", async () => {
    atLocalhost();
    const hello = windowsHello();
    const fetch = serve(
      {
        "/api/passkey/enrol/options": [200, { options: { challenge: "xyz" } }],
        "/api/passkey/enrol": [200, ENROLLED],
      },
      { enrolled: false, enrolment_open: true },
    );
    view(page);
    const panel = await screen.findByRole("region", { name: "Set up your passkey" });
    const submit = within(panel).getByRole("button", { name: "Set up passkey" });
    expect(submit).toBeDisabled();
    await userEvent.type(within(panel).getByRole("textbox", { name: "Code" }), "0A1B2C3D");
    await userEvent.click(submit);
    expect(await screen.findByText(/Your passkey is set up/)).toBeInTheDocument();
    expect(hello.create).toHaveBeenCalledOnce();
    expect(posts(fetch).map((p) => p.body)).toEqual([
      { code: "0a1b2c3d" },
      { code: "0a1b2c3d", credential: { id: "cred", response: { signature: "sig" } } },
    ]);
  });

  it("offers to replace a lost passkey, with a warning, when started with the code", async () => {
    serve({}, { enrolled: true, enrolment_open: true });
    atLocalhost();
    view(page);
    const panel = await screen.findByRole("region", { name: "Replace your passkey" });
    expect(panel).toHaveTextContent("your current passkey stops working");
    expect(within(panel).getByRole("button", { name: "Replace passkey" })).toBeDisabled();
  });

  it("keeps only the code's own characters when it is pasted with quotes or spaces", async () => {
    serve({}, { enrolled: false, enrolment_open: true });
    atLocalhost();
    view(page);
    const panel = await screen.findByRole("region", { name: "Set up your passkey" });
    const box = within(panel).getByRole("textbox", { name: "Code" });
    await userEvent.click(box);
    await userEvent.paste(" `10F9E6EC` ");
    expect(box).toHaveValue("10f9e6ec");
    expect(within(panel).getByRole("button", { name: "Set up passkey" })).toBeEnabled();
    await userEvent.clear(box);
    await userEvent.type(box, "10f9e6e");
    expect(panel).toHaveTextContent("7 of 8 characters");
    expect(within(panel).getByRole("button", { name: "Set up passkey" })).toBeDisabled();
  });

  it("explains a wrong set-up code", async () => {
    atLocalhost();
    windowsHello();
    serve(
      { "/api/passkey/enrol/options": [403, { detail: "passkey.wrong_code" }] },
      { enrolled: false, enrolment_open: true },
    );
    view(page);
    const panel = await screen.findByRole("region", { name: "Set up your passkey" });
    await userEvent.type(within(panel).getByRole("textbox", { name: "Code" }), "ffffffff");
    await userEvent.click(within(panel).getByRole("button", { name: "Set up passkey" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("doesn't match the one shown");
  });

  it("explains a refusal in plain words and changes nothing", async () => {
    serve({ "/api/registry/requests": [409, { detail: "registry.already_asked" }] });
    const onChange = view(skillOff);
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    await userEvent.click(screen.getByRole("button", { name: "Ask to switch on" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You have already asked");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("refuses an answer that does not match the contract", async () => {
    serve({ "/api/registry/switch-off": [200, { ...skillOff, switches: "yes" }] });
    const onChange = view(page);
    const paddle = screen.getByRole("article", { name: "Paddle" });
    await userEvent.click(within(paddle).getByRole("button", { name: "Switch off" }));
    await userEvent.click(within(paddle).getByRole("button", { name: "Switch off" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That did not work");
    expect(onChange).not.toHaveBeenCalled();
  });
});

function cleanupAndRender(shown: RegistryPage) {
  cleanup();
  serve({});
  view(shown);
}
