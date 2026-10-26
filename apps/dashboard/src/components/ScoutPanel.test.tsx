import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentOut, RoleAdvice, RoleChoice, ScoutView, WorkspaceAgents } from "../api";
import { AgentsPanel } from "./AgentsPanel";
import { ScoutPanel } from "./ScoutPanel";

const SCOUT = "/api/workspaces/sage/scout";
const AGENTS = "/api/workspaces/sage/agents";
const SETTINGS = "/api/workspaces/sage/settings";

const ROLES: RoleChoice[] = [
  { id: "monitoring", title: "Monitoring", mission: "Watch.", permission: "read" },
  { id: "analytics", title: "Analytics", mission: "Count.", permission: "read" },
  { id: "marketing", title: "Marketing", mission: "Promote.", permission: "draft" },
  { id: "finance", title: "Finance", mission: "Money.", permission: "draft" },
  { id: "legal", title: "Legal", mission: "Contracts.", permission: "draft" },
];

function advice(role_id: string, title: string, recommend: boolean, extra: Partial<RoleAdvice> = {}): RoleAdvice {
  return {
    role_id,
    title,
    recommend,
    reason: recommend ? `${title} is needed.` : `No sign that ${title} is needed yet.`,
    files: recommend ? [".github/workflows/ci.yml"] : [],
    removed_files: 0,
    downgraded: false,
    ...extra,
  };
}

const NONE: ScoutView = {
  connected: true,
  state: "none",
  stage: null,
  repo: "acme/sage",
  branch: null,
  started_at: null,
  finished_at: null,
  reason: null,
  report: null,
};
const RUNNING: ScoutView = {
  ...NONE,
  state: "running",
  stage: "thinking",
  branch: "main",
  started_at: "2026-10-24T20:00:00+00:00",
};
const DONE: ScoutView = {
  ...RUNNING,
  state: "done",
  stage: "finished",
  finished_at: "2026-10-24T20:05:00+00:00",
  report: {
    summary: "Sage is a small web app with a database.",
    roles: [
      advice("monitoring", "Monitoring", true, { files: [".github/workflows/ci.yml", "wrangler.jsonc"] }),
      advice("analytics", "Analytics", true, { removed_files: 1 }),
      advice("marketing", "Marketing", false, { downgraded: true, reason: "The README says so." }),
      advice("finance", "Finance", false),
    ],
    not_reviewed: ["legal"],
    suggested_providers: [{ name: "Plausible", files: ["src/analytics.ts"] }],
    notes: ["1 cited file(s) do not exist in the repository and were removed."],
  },
};

function agent(role_id: string, title: string): AgentOut {
  return {
    id: `11111111-2222-4333-8444-${String(ROLES.findIndex((r) => r.id === role_id)).padStart(12, "0")}`,
    role_id,
    title,
    enabled: false,
    model: "local-gemma",
    cap_minor: null,
    spent_minor: 0,
    newer_version: false,
  };
}
const EMPTY: WorkspaceAgents = { enabled: false, cap_minor: null, spent_minor: 0, agents: [], switches: true };
const withAgents = (...agents: AgentOut[]): WorkspaceAgents => ({ ...EMPTY, agents });

type Route = [status: number, body: unknown];

// A pretend server: each GET or POST answers the next reply queued for it (the last one repeats).
function serve(routes: Record<string, Route[]>) {
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`;
    const queue = routes[key] ?? (path === "/api/roles" ? [[200, ROLES] as Route] : undefined);
    const [status, body] = (queue && queue.length > 1 ? queue.shift() : queue?.[0]) ?? [404, { detail: "nope" }];
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function calls(fetch: ReturnType<typeof serve>, method: string) {
  return (fetch.mock.calls as unknown as [string, RequestInit | undefined][])
    .filter(([, init]) => (init?.method ?? "GET") === method)
    .map(([path, init]) => ({ path, body: init?.body === undefined ? null : (JSON.parse(init.body as string) as unknown) }));
}

const scoutBox = async () => screen.findByRole("region", { name: "The scout" });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the scout on the Agents tab", () => {
  it("says what it needs when it is not connected, and offers no Ask button", async () => {
    serve({ [`GET ${SCOUT}`]: [[200, { ...NONE, connected: false, repo: null }]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    expect(within(box).getByText(/needs the laptop database, the GitHub token and the local model/)).toBeInTheDocument();
    expect(within(box).queryByRole("button")).not.toBeInTheDocument();
    expect(within(box).getByText("Folder: not connected yet")).toBeInTheDocument();
  });

  it("asks the scout, shows how far it is, and reads again until it is done", async () => {
    const fetch = serve({
      [`GET ${SCOUT}`]: [[200, NONE], [200, RUNNING], [200, DONE]],
      [`POST ${SCOUT}`]: [[202, { ...RUNNING, stage: "reading" }]],
      [`GET ${AGENTS}`]: [[200, EMPTY]],
    });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    await userEvent.click(within(box).getByRole("button", { name: "Ask the scout" }));
    expect(await within(box).findByText(/Reading the repository/)).toBeInTheDocument();
    expect(await within(box).findByText(/The local model is thinking/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(await within(box).findByText(DONE.report?.summary ?? "", {}, { timeout: 8000 })).toBeInTheDocument();
    expect(calls(fetch, "POST")).toEqual([{ path: SCOUT, body: {} }]);
    expect(within(box).getByRole("listitem", { current: "step" })).toHaveTextContent("Choose agents");
  }, 20_000);

  it("reads again while the run is going, and stops once it has ended", async () => {
    const fetch = serve({ [`GET ${SCOUT}`]: [[200, RUNNING], [200, RUNNING], [200, DONE]] });
    render(<ScoutPanel slug="sage" name="Sage" roles={ROLES} given={new Set()} busy={false} onCreate={vi.fn()} pollMs={20} />);
    await screen.findByText(DONE.report?.summary ?? "");
    await new Promise((done) => setTimeout(done, 300));
    expect(calls(fetch, "GET").filter((c) => c.path === SCOUT)).toHaveLength(3);
  });

  it("ticks what it recommends, with the reason and the files code checked", async () => {
    serve({ [`GET ${SCOUT}`]: [[200, DONE]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    const monitoring = within(box).getByRole("checkbox", { name: "Monitoring" });
    expect(monitoring).toBeChecked();
    expect(monitoring).toHaveAccessibleDescription("Monitoring is needed.");
    expect(within(box).getByText("Found: .github/workflows/ci.yml, wrangler.jsonc")).toBeInTheDocument();
    expect(within(box).getByText("Code removed 1 file name that does not exist.")).toBeInTheDocument();
    expect(within(box).getByText(/Checked by code: 1 cited file/)).toBeInTheDocument();
    expect(within(box).getByText("src/analytics.ts").closest("li")).toHaveTextContent("Plausible: src/analytics.ts");
    expect(within(box).getByRole("button", { name: "Create 2 agents" })).toBeEnabled();
  });

  it("lists every other role in the library, saying why it is not recommended", async () => {
    serve({ [`GET ${SCOUT}`]: [[200, DONE]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const library = within(await scoutBox()).getByRole("complementary", { name: "Add more from the library" });
    expect(within(library).getByText("Not recommended: no evidence in the code, only in the docs.")).toBeInTheDocument();
    expect(within(library).getByText("No sign that Finance is needed yet.")).toBeInTheDocument();
    expect(within(library).getByText("Not reviewed by the scout.")).toBeInTheDocument();
    expect(within(library).queryByText("Monitoring")).not.toBeInTheDocument();
    await userEvent.type(within(library).getByLabelText("Search 3 roles"), "fin");
    expect(within(library).getAllByRole("listitem")).toHaveLength(1);
    expect(within(library).getByText("Finance")).toBeInTheDocument();
  });

  it("+ adds a role the scout did not recommend, and the count follows", async () => {
    serve({ [`GET ${SCOUT}`]: [[200, DONE]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    await userEvent.click(within(box).getByRole("button", { name: "Add Finance" }));
    expect(within(box).getByRole("button", { name: "Remove Finance" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(box).getByRole("checkbox", { name: "Analytics" }));
    expect(within(box).getByRole("button", { name: "Create 2 agents" })).toBeInTheDocument();
  });

  it("Create makes each ticked agent, switched off with no spending, and says so", async () => {
    const monitoring = agent("monitoring", "Monitoring");
    const analytics = agent("analytics", "Analytics");
    const fetch = serve({
      [`GET ${SCOUT}`]: [[200, DONE]],
      [`GET ${AGENTS}`]: [[200, EMPTY]],
      [`POST ${AGENTS}`]: [
        [200, withAgents(monitoring)],
        [200, withAgents(monitoring, analytics)],
      ],
    });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    await userEvent.click(within(box).getByRole("button", { name: "Create 2 agents" }));
    expect(await within(box).findByText(/2 agents were made, switched off with no spending/)).toBeInTheDocument();
    expect(calls(fetch, "POST")).toEqual([
      { path: AGENTS, body: { role_id: "monitoring" } },
      { path: AGENTS, body: { role_id: "analytics" } },
    ]);
    expect(within(box).getByRole("listitem", { current: "step" })).toHaveTextContent("Done");
    // The agents list below now shows them, and the scout shows them as already there.
    expect(screen.getByRole("button", { name: "Switch Analytics on" })).toBeInTheDocument();
    expect(within(box).getByRole("checkbox", { name: /Monitoring/ })).toBeDisabled();
    expect(within(box).getByRole("button", { name: "Create 0 agents" })).toBeDisabled();
  });

  it("a role the workspace already has is shown as there, and not made again", async () => {
    const fetch = serve({
      [`GET ${SCOUT}`]: [[200, DONE]],
      [`GET ${AGENTS}`]: [[200, withAgents(agent("monitoring", "Monitoring"))]],
      [`POST ${AGENTS}`]: [[200, withAgents(agent("monitoring", "Monitoring"), agent("analytics", "Analytics"))]],
    });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    expect(within(box).getByText("Already in this workspace")).toBeInTheDocument();
    await userEvent.click(within(box).getByRole("button", { name: "Create 1 agent" }));
    await within(box).findByText(/1 agent was made/);
    expect(calls(fetch, "POST")).toEqual([{ path: AGENTS, body: { role_id: "analytics" } }]);
  });

  it("a typed cap is set after the agents are made, through the Windows Hello path", async () => {
    const made = withAgents(agent("monitoring", "Monitoring"));
    const fetch = serve({
      [`GET ${SCOUT}`]: [[200, DONE]],
      [`GET ${AGENTS}`]: [[200, EMPTY]],
      [`POST ${AGENTS}`]: [[200, made]],
      [`POST ${SETTINGS}`]: [[200, { ...made, cap_minor: 2000 }]],
    });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    await userEvent.click(within(box).getByRole("checkbox", { name: "Analytics" }));
    await userEvent.type(within(box).getByLabelText("Monthly spend cap for this workspace (USD)"), "20");
    await userEvent.click(within(box).getByRole("button", { name: "Create 1 agent" }));
    await within(box).findByText(/1 agent was made/);
    expect(calls(fetch, "POST")).toEqual([
      { path: AGENTS, body: { role_id: "monitoring" } },
      { path: SETTINGS, body: { enabled: false, cap_minor: 2000 } },
    ]);
  });

  it("a cap that is not a number stops Create and says how to write it", async () => {
    serve({ [`GET ${SCOUT}`]: [[200, DONE]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    await userEvent.type(within(box).getByLabelText("Monthly spend cap for this workspace (USD)"), "lots");
    expect(within(box).getByText("Type a number of dollars, like 20 or 12.50.")).toBeInTheDocument();
    expect(within(box).getByRole("button", { name: "Create 2 agents" })).toBeDisabled();
  });

  it("a failure half way says how many were made", async () => {
    serve({
      [`GET ${SCOUT}`]: [[200, DONE]],
      [`GET ${AGENTS}`]: [[200, EMPTY]],
      [`POST ${AGENTS}`]: [
        [200, withAgents(agent("monitoring", "Monitoring"))],
        [503, { detail: "agents.store_unavailable" }],
      ],
    });
    render(<AgentsPanel slug="sage" name="Sage" />);
    await userEvent.click(within(await scoutBox()).getByRole("button", { name: "Create 2 agents" }));
    expect(await screen.findByText(/1 of 2 agents were made. The laptop database did not answer/)).toBeInTheDocument();
  });

  it("a failed run says why in words, and can be asked again", async () => {
    const fetch = serve({
      [`GET ${SCOUT}`]: [[200, { ...RUNNING, state: "failed", stage: "finished", reason: "scout.not_json" }]],
      [`POST ${SCOUT}`]: [[202, RUNNING]],
      [`GET ${AGENTS}`]: [[200, EMPTY]],
    });
    render(<AgentsPanel slug="sage" name="Sage" />);
    const box = await scoutBox();
    expect(
      within(box).getByText(/not in the required shape, so code refused it, told the model why and asked twice more/),
    ).toBeInTheDocument();
    await userEvent.click(within(box).getByRole("button", { name: "Ask the scout again" }));
    expect(await within(box).findByText(/The local model is thinking/)).toBeInTheDocument();
    expect(calls(fetch, "POST")).toEqual([{ path: SCOUT, body: {} }]);
  });

  it("a refused ask says why", async () => {
    serve({
      [`GET ${SCOUT}`]: [[200, NONE]],
      [`POST ${SCOUT}`]: [[409, { detail: "scout.no_repo" }]],
      [`GET ${AGENTS}`]: [[200, EMPTY]],
    });
    render(<AgentsPanel slug="sage" name="Sage" />);
    await userEvent.click(within(await scoutBox()).getByRole("button", { name: "Ask the scout" }));
    expect(await screen.findByText(/has no GitHub repository, so the scout has nothing to read/)).toBeInTheDocument();
  });

  it("a project just added is asked about once, straight away", async () => {
    const fetch = serve({
      [`GET ${SCOUT}`]: [[200, NONE]],
      [`POST ${SCOUT}`]: [[202, RUNNING]],
      [`GET ${AGENTS}`]: [[200, EMPTY]],
    });
    render(<AgentsPanel slug="sage" name="Sage" askScout />);
    expect(await screen.findByText(/The local model is thinking/)).toBeInTheDocument();
    expect(calls(fetch, "POST")).toEqual([{ path: SCOUT, body: {} }]);
  });

  it("a project the scout has already looked at is not asked again on its own", async () => {
    const fetch = serve({ [`GET ${SCOUT}`]: [[200, DONE]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    render(<AgentsPanel slug="sage" name="Sage" askScout />);
    await screen.findByText(DONE.report?.summary ?? "");
    expect(calls(fetch, "POST")).toEqual([]);
  });

  it("shows the model's words as plain text, never as markup", async () => {
    const report = DONE.report;
    if (report === null) throw new Error("DONE has a report");
    const sneaky = { ...report, summary: '<img src=x onerror="alert(1)"> Sage' };
    serve({ [`GET ${SCOUT}`]: [[200, { ...DONE, report: sneaky }]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    const { container } = render(<AgentsPanel slug="sage" name="Sage" />);
    expect(await screen.findByText('<img src=x onerror="alert(1)"> Sage')).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("refuses a scout answer that breaks the contract", async () => {
    serve({ [`GET ${SCOUT}`]: [[200, { ...RUNNING, report: DONE.report }]], [`GET ${AGENTS}`]: [[200, EMPTY]] });
    render(<AgentsPanel slug="sage" name="Sage" />);
    expect(await screen.findByText("Could not load the scout.")).toBeInTheDocument();
  });
});
