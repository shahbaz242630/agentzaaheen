import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentOut, WorkspaceAgents } from "../api";
import { AgentsPanel, capWords, centsFrom, money } from "./AgentsPanel";

const AGENT_ID = "11111111-2222-4333-8444-555555555555";
const AGENT_PATH = `/api/workspaces/eva/agents/${AGENT_ID}`;
const monitoring: AgentOut = {
  id: AGENT_ID,
  role_id: "monitoring",
  title: "Monitoring",
  enabled: false,
  model: "local-gemma",
  cap_minor: null,
  spent_minor: 0,
  newer_version: false,
};
const sales: AgentOut = { ...monitoring, id: "22222222-2222-4333-8444-555555555555", role_id: "sales", title: "Sales" };
const empty: WorkspaceAgents = { enabled: false, cap_minor: null, spent_minor: 0, agents: [], switches: true };
const one: WorkspaceAgents = { ...empty, agents: [monitoring] };
const ROLES = [
  { id: "monitoring", title: "Monitoring", mission: "Watch.", permission: "read" },
  { id: "sales", title: "Sales", mission: "Sell.", permission: "draft" },
];
const SIGNED = { id: "cred", response: { signature: "sig" } };
// The scout has its own tests (ScoutPanel.test.tsx); here it is never connected.
const NO_SCOUT = {
  connected: false,
  state: "none",
  stage: null,
  repo: null,
  branch: null,
  started_at: null,
  finished_at: null,
  reason: null,
  report: null,
};

type Route = [status: number, body: unknown];

// A pretend server: GETs answer the agents and roles; each POST answers the next reply queued for its path.
function serve(start: WorkspaceAgents, replies: Record<string, Route[]> = {}) {
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    if (init?.method !== "POST") {
      const body = path === "/api/roles" ? ROLES : path.endsWith("/scout") ? NO_SCOUT : start;
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the Agents tab", () => {
  it("without the laptop database says how to get agents, and offers nothing to press", async () => {
    serve({ ...empty, switches: false });
    render(<AgentsPanel slug="eva" name="Eva" />);
    expect(await screen.findByText(/Agents need the laptop database/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("says nothing can spend until a cap is set", async () => {
    serve(one);
    render(<AgentsPanel slug="eva" name="Eva" />);
    expect(await screen.findByText(/Nothing can spend until you set a cap/)).toBeInTheDocument();
    expect(screen.getByText(/Model: Gemma \(local\) · cap not set · spent \$0\.00 this month/)).toBeInTheDocument();
  });

  it("adds an agent from the library, offering only roles the workspace does not have yet", async () => {
    const fetch = serve(one, { "/api/workspaces/eva/agents": [[200, { ...one, agents: [monitoring, sales] }]] });
    render(<AgentsPanel slug="eva" name="Eva" />);
    const picker = await screen.findByLabelText("Add an agent from the role library");
    expect(within(picker).queryByRole("option", { name: /Monitoring/ })).not.toBeInTheDocument();
    await userEvent.selectOptions(picker, "sales");
    await userEvent.click(screen.getByRole("button", { name: "Add agent" }));
    expect(await screen.findByText("Sales")).toBeInTheDocument();
    expect(posts(fetch)).toEqual([{ path: "/api/workspaces/eva/agents", body: { role_id: "sales" } }]);
  });

  it("switching on asks Windows Hello to sign that exact change, then sends it again with the signature", async () => {
    const credentials = windowsHello();
    const fetch = serve(one, {
      [AGENT_PATH]: [
        [409, { detail: "agents.needs_passkey" }],
        [200, { ...one, agents: [{ ...monitoring, enabled: true }] }],
      ],
      [`${AGENT_PATH}/options`]: [[200, { options: { challenge: "c" } }]],
    });
    render(<AgentsPanel slug="eva" name="Eva" />);
    await userEvent.click(await screen.findByRole("button", { name: "Switch Monitoring on" }));
    expect(await screen.findByRole("button", { name: "Switch Monitoring off" })).toBeInTheDocument();
    expect(credentials.get).toHaveBeenCalledTimes(1);
    expect(posts(fetch)).toEqual([
      { path: AGENT_PATH, body: { enabled: true, cap_minor: null } },
      { path: `${AGENT_PATH}/options`, body: { enabled: true, cap_minor: null } },
      { path: AGENT_PATH, body: { enabled: true, cap_minor: null, credential: SIGNED } },
    ]);
  });

  it("switching off never asks for Windows Hello", async () => {
    const credentials = windowsHello();
    const on = { ...one, agents: [{ ...monitoring, enabled: true, cap_minor: 500 }] };
    const fetch = serve(on, { [AGENT_PATH]: [[200, one]] });
    render(<AgentsPanel slug="eva" name="Eva" />);
    await userEvent.click(await screen.findByRole("button", { name: "Switch Monitoring off" }));
    expect(await screen.findByRole("button", { name: "Switch Monitoring on" })).toBeInTheDocument();
    expect(credentials.get).not.toHaveBeenCalled();
    expect(posts(fetch)).toEqual([{ path: AGENT_PATH, body: { enabled: false, cap_minor: 500 } }]);
  });

  it("closing Windows Hello changes nothing and says so", async () => {
    windowsHello("cancels");
    serve(one, {
      [AGENT_PATH]: [[409, { detail: "agents.needs_passkey" }]],
      [`${AGENT_PATH}/options`]: [[200, { options: { challenge: "c" } }]],
    });
    render(<AgentsPanel slug="eva" name="Eva" />);
    await userEvent.click(await screen.findByRole("button", { name: "Switch Monitoring on" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Windows Hello was closed, so nothing changed.");
    expect(screen.getByRole("button", { name: "Switch Monitoring on" })).toBeInTheDocument();
  });

  it("sets the workspace's monthly cap in dollars, sent as cents", async () => {
    windowsHello();
    const fetch = serve(one, {
      "/api/workspaces/eva/settings": [
        [409, { detail: "agents.needs_passkey" }],
        [200, { ...one, cap_minor: 2550 }],
      ],
      "/api/workspaces/eva/settings/options": [[200, { options: { challenge: "c" } }]],
    });
    render(<AgentsPanel slug="eva" name="Eva" />);
    await userEvent.type(await screen.findByLabelText("Monthly cap for Eva (USD)"), "25.50");
    const [save] = screen.getAllByRole("button", { name: "Save cap" });
    await userEvent.click(save as HTMLElement);
    expect(await screen.findByText(/Monthly cap \$25\.50/)).toBeInTheDocument();
    expect(posts(fetch)[0]).toEqual({ path: "/api/workspaces/eva/settings", body: { enabled: false, cap_minor: 2550 } });
  });
});

describe("caps and money in words", () => {
  it("reads dollars typed by a person, refusing anything odd", () => {
    expect(centsFrom("25.5")).toBe(2550);
    expect(centsFrom("$10")).toBe(1000);
    expect(centsFrom("")).toBeNull();
    for (const bad of ["-1", "1.234", "ten", "1e5"]) expect(centsFrom(bad)).toBe("bad");
  });

  it("shows cents as dollars, and no cap as not set", () => {
    expect(money(2550)).toBe("$25.50");
    expect(capWords(null)).toBe("not set");
    expect(capWords(500)).toBe("$5.00");
  });
});
