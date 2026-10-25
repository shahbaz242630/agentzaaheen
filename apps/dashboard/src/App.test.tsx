import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { failureReply, notConnectedReply, SAMPLE_NOTE } from "./components/ChatPanel";
import sample from "./contract/sample.json";

const NO_AGENTS = { enabled: false, cap_minor: null, spent_minor: 0, agents: [], switches: false };

function answer(path: string): unknown {
  if (path === "/api/workspaces") return sample.projects;
  if (path === "/api/models") return sample.models;
  if (path === "/api/registry") return sample.registry;
  if (path === "/api/roles") return [];
  if (path.endsWith("/agents")) return NO_AGENTS;
  // Sample mode has no database: summaries are "not set up yet" everywhere.
  if (path === "/api/summaries") return { connected: false, workspaces: {} };
  if (path === "/api/briefings") return { connected: false, briefings: [] };
  if (path.endsWith("/summaries")) return { connected: false, days: [] };
  const id = decodeURIComponent(path.replace("/api/workspaces/", ""));
  return (sample.details as Record<string, unknown>)[id];
}

function serve(override?: (path: string) => Response | undefined) {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) => {
      const custom = override?.(path);
      if (custom) return Promise.resolve(custom);
      return Promise.resolve(new Response(JSON.stringify(answer(path)), { status: 200 }));
    }),
  );
}

beforeEach(() => {
  serve();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// Open a workspace from the left column (the Board's cards carry the same names).
async function openWorkspace(name: string) {
  const nav = await screen.findByRole("navigation", { name: "Workspaces" });
  await userEvent.click(within(nav).getByRole("button", { name: new RegExp(name) }));
  return screen.findByRole("heading", { level: 1, name });
}

async function startOn(name: string) {
  render(<App />);
  await openWorkspace(name);
}

describe("the dashboard", () => {
  it("opens on the Board and lists every workspace on the left", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "The Board" })).toBeInTheDocument();
    const nav = await screen.findByRole("navigation", { name: "Workspaces" });
    const names = within(nav)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(names).toEqual([
      expect.stringContaining("Zaaheen"),
      expect.stringContaining("Sandoq Kin"),
      expect.stringContaining("Agent X"),
      expect.stringContaining("Eva"),
      expect.stringContaining("Family Check-in"),
    ]);
    expect(await openWorkspace("Zaaheen")).toBeInTheDocument();
    expect(screen.getByText(/Sample data/)).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Support email" })).toHaveTextContent("Responded23");
  });

  it("switches project when one is clicked", async () => {
    render(<App />);
    await openWorkspace("Zaaheen");
    expect(await openWorkspace("Agent X")).toBeInTheDocument();
    expect(screen.getByText("99.95%")).toBeInTheDocument();
  });

  it("shows unknown, never 0, for a source that is not connected", async () => {
    await startOn("Eva");
    const revenue = await screen.findByRole("article", { name: "Revenue" });
    expect(revenue).toHaveTextContent("Not connected yet");
    expect(within(revenue).getAllByText("unknown")).toHaveLength(2);
    expect(revenue).not.toHaveTextContent(/\b0\b/);
  });

  it("says so when the server cannot be reached", async () => {
    serve(() => new Response("{}", { status: 500 }));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the workspaces");
  });

  it("refuses an answer that breaks the contract", async () => {
    serve((path) =>
      path === "/api/workspaces"
        ? new Response(JSON.stringify([{ ...sample.projects[0], email: "a@b.c" }]), { status: 200 })
        : undefined,
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the workspaces");
  });
});

describe("adding a project", () => {
  it("opens the new project on its Agents tab and asks the scout straight away", async () => {
    const sage = { id: "sage", name: "Sage", summary: "", health: "unknown", escalations: 0, sample: false };
    let added = false;
    const posts: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string, init?: RequestInit) => {
        const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));
        if (init?.method === "POST") {
          posts.push(path);
          if (path === "/api/workspaces") {
            added = true;
            return reply({ slug: "sage", name: "Sage" }, 201);
          }
          return reply({ ...scoutView, state: "running", stage: "reading", started_at: "2026-10-24T20:00:00+00:00" }, 202);
        }
        if (path === "/api/workspaces") return reply(added ? [...sample.projects, sage] : sample.projects);
        if (path === "/api/workspaces/sage") return reply({ project: sage, cards: [] });
        if (path === "/api/workspaces/sage/agents") return reply({ ...NO_AGENTS, switches: true });
        if (path === "/api/workspaces/sage/scout") return reply(scoutView);
        if (path === "/api/github/repos") return reply({ repos: [{ repo: "acme/sage", used_by: null }], connected: true });
        return reply(answer(path));
      }),
    );
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Add a project" }));
    await userEvent.type(screen.getByLabelText("Project name"), "Sage");
    await userEvent.selectOptions(await screen.findByLabelText("GitHub repository (read-only)"), "acme/sage");
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Sage" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Agents" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText(/Reading the repository/)).toBeInTheDocument();
    expect(posts).toEqual(["/api/workspaces", "/api/workspaces/sage/scout"]);
  });
});

const scoutView = {
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

describe("real and sample cards side by side", () => {
  it("tags sample cards and says the others are real", async () => {
    const zaaheen = sample.details.zaaheen;
    const mixed = {
      ...zaaheen,
      cards: zaaheen.cards.map((card) => (card.id === "paddle" ? { ...card, sample: false } : card)),
    };
    serve((path) =>
      path === "/api/workspaces/zaaheen" ? new Response(JSON.stringify(mixed), { status: 200 }) : undefined,
    );
    await startOn("Zaaheen");
    expect(await screen.findByText(/The others are real/)).toBeInTheDocument();
    const revenue = screen.getAllByRole("article", { name: "Revenue" });
    expect(revenue[0]).not.toHaveTextContent("Sample");
    expect(revenue[1]).toHaveTextContent("Sample");
  });
});

describe("the UI enforces the API's rules too", () => {
  const zaaheen = sample.details.zaaheen;

  it("refuses a stale card that carries a number", async () => {
    const stale = {
      ...zaaheen,
      cards: zaaheen.cards.map((card) => (card.id === "paddle" ? { ...card, freshness: "stale" } : card)),
    };
    serve((path) =>
      path === "/api/workspaces/zaaheen" ? new Response(JSON.stringify(stale), { status: 200 }) : undefined,
    );
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Workspaces" });
    await userEvent.click(within(nav).getByRole("button", { name: /Zaaheen/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this workspace");
    expect(screen.queryByText("$1,240")).not.toBeInTheDocument();
  });

  it("refuses a card that never synced but claims a last success", async () => {
    const eva = sample.details.eva;
    const odd = {
      ...eva,
      cards: eva.cards.map((card) => ({ ...card, last_success_at: "2026-10-18T11:00:00Z" })),
    };
    serve((path) => (path === "/api/workspaces/eva" ? new Response(JSON.stringify(odd), { status: 200 }) : undefined));
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Workspaces" });
    await userEvent.click(within(nav).getByRole("button", { name: /Eva/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this workspace");
  });

  it.each([
    ["a cloud default", (m: { id: string }) => ({ default: m.id === "cloud-claude" })],
    ["no default", () => ({ default: false })],
  ])("refuses a model list with %s and never pre-selects a cloud model", async (_name, change) => {
    const models = { models: sample.models.models.map((m) => ({ ...m, ...change(m) })) };
    serve((path) => (path === "/api/models" ? new Response(JSON.stringify(models), { status: 200 }) : undefined));
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "The Board" });
    expect(screen.queryByRole("option", { name: /Claude/ })).not.toBeInTheDocument();
    expect(screen.getByText(/No model list yet/)).toBeInTheDocument();
  });

  it("says so when there are no workspaces", async () => {
    serve((path) => (path === "/api/workspaces" ? new Response("[]", { status: 200 }) : undefined));
    render(<App />);
    expect(await screen.findAllByText("No workspaces yet.")).not.toHaveLength(0);
  });
});

describe("the chat", () => {
  it("defaults to the local model and lists cloud ones as not connected", async () => {
    render(<App />);
    await screen.findByRole("option", { name: /Gemma/ });
    expect(screen.getByLabelText("Model")).toHaveDisplayValue(/Gemma \(local\) · default/);
    expect(screen.getByRole("option", { name: /Claude \(cloud\) · not connected/ })).toBeInTheDocument();
  });

  it("answers honestly that no model is connected yet", async () => {
    await startOn("Zaaheen");
    await userEvent.type(screen.getByLabelText("Message"), "Summarise yesterday's actions{Enter}");
    expect(screen.getByText("Summarise yesterday's actions")).toBeInTheDocument();
    expect(screen.getByText(/so I can.t answer for real/)).toHaveTextContent("Zaaheen");
    expect(screen.getByLabelText("Message")).toHaveValue("");
  });

  it("follows the model picked in the menu", async () => {
    render(<App />);
    await screen.findByRole("option", { name: /Claude/ });
    await userEvent.selectOptions(screen.getByLabelText("Model"), "cloud-claude");
    await userEvent.type(screen.getByLabelText("Message"), "hello{Enter}");
    expect(screen.getByText(/^Claude \(cloud\) is not connected yet/)).toBeInTheDocument();
  });

  it("sends a suggestion with one click and keeps voice switched off", async () => {
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "What is escalated to me?" }));
    expect(screen.getByText("What is escalated to me?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Voice (coming later)" })).toBeDisabled();
  });

  it("ignores an empty message", async () => {
    render(<App />);
    const input = await screen.findByLabelText("Message");
    await userEvent.type(input, "   {Enter}");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.queryByText(/so I can.t answer for real/)).not.toBeInTheDocument();
  });

  it("shows text exactly as written, never as HTML", async () => {
    await startOn("Zaaheen");
    await userEvent.type(screen.getByLabelText("Message"), "<b>bold</b>{Enter}");
    expect(screen.getByText("<b>bold</b>")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("words the reply without a model or a project", () => {
    expect(notConnectedReply(undefined, null)).toMatch(/^The model is not connected yet.*all your workspaces/);
  });
});

describe("the chat with the local model connected", () => {
  const connected = { models: sample.models.models.map((m) => ({ ...m, available: m.id === "local-gemma" })) };

  function reply(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status });
  }

  function serveChat(chat: () => Promise<Response>): RequestInit[] {
    const calls: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string, init?: RequestInit) => {
        if (path === "/api/workspaces/zaaheen/chat") {
          calls.push(init ?? {});
          return chat();
        }
        return Promise.resolve(reply(path === "/api/models" ? connected : answer(path)));
      }),
    );
    return calls;
  }

  async function ask(question: string) {
    await startOn("Zaaheen");
    await screen.findByRole("option", { name: /Gemma \(local\) · default$/ });
    await userEvent.type(screen.getByLabelText("Message"), `${question}{Enter}`);
  }

  it("asks the model about the open project and shows its answer", async () => {
    const calls = serveChat(() =>
      Promise.resolve(reply({ model_id: "local-gemma", answer: "MRR is $1,240.", from_sample: false, used: [] })),
    );
    await ask("How is revenue?");
    expect(await screen.findByText("MRR is $1,240.")).toBeInTheDocument();
    expect(calls[0]?.method).toBe("POST");
    expect(calls[0]?.headers).toMatchObject({ "Content-Type": "application/json", "X-Yaqzan-Request": "1" });
    expect(JSON.parse(calls[0]?.body as string)).toEqual({
      model_id: "local-gemma",
      question: "How is revenue?",
    });
  });

  it("says when an answer leans on sample data", async () => {
    serveChat(() =>
      Promise.resolve(reply({ model_id: "local-gemma", answer: "There are 2 escalations.", from_sample: true, used: [] })),
    );
    await ask("What is escalated to me?");
    expect(await screen.findByText(/There are 2 escalations\./)).toHaveTextContent(SAMPLE_NOTE);
  });

  it("says which skill and reads an answer used", async () => {
    const used = ["Skill: triage-failing-build", "Build log: eva"];
    serveChat(() =>
      Promise.resolve(reply({ model_id: "local-gemma", answer: "A test broke.", from_sample: false, used })),
    );
    await ask("Why is Eva red?");
    expect(await screen.findByText(/A test broke\./)).toHaveTextContent(
      "Used: Skill: triage-failing-build · Build log: eva",
    );
  });

  it("shows a placeholder and blocks a second question while the model thinks", async () => {
    let finish: (response: Response) => void = () => undefined;
    serveChat(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    await ask("How is revenue?");
    expect(screen.getByText("Thinking…")).toHaveAttribute("aria-busy", "true");
    await userEvent.type(screen.getByLabelText("Message"), "again");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    finish(reply({ model_id: "local-gemma", answer: "Done.", from_sample: false, used: [] }));
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it.each([
    ["net.unavailable", 502, /model server isn't running/],
    ["net.timeout", 502, /took too long/],
    ["chat.busy", 429, /still answering the last question/],
    ["chat.not_connected", 503, /so I can.t answer for real/],
    ["net.bad_response", 502, /went wrong asking the model \(net\.bad_response\)\. Nothing was changed\./],
  ])("explains %s in words", async (code, status, words) => {
    serveChat(() => Promise.resolve(reply({ detail: code }, status)));
    await ask("How is revenue?");
    expect(await screen.findByText(words)).toBeInTheDocument();
  });

  it("refuses an answer that does not match the contract", async () => {
    serveChat(() => Promise.resolve(reply({ answer: "<script>" })));
    await ask("How is revenue?");
    expect(await screen.findByText(/^Something went wrong asking the model\. Nothing was changed\.$/)).toBeInTheDocument();
  });

  it("explains an error with no code", () => {
    expect(failureReply(new Error("boom"), undefined, null)).toBe(
      "Something went wrong asking the model. Nothing was changed.",
    );
  });
});

describe("the Board and the workspace pages (ADR-039)", () => {
  it("lists what needs you from every workspace, and opens one from its card", async () => {
    render(<App />);
    const needs = await screen.findByRole("region", { name: "Needs you" });
    const escalated = sample.projects.filter((p) => p.escalations > 0);
    for (const project of escalated) {
      expect(within(needs).getByRole("button", { name: `Open ${project.name}` })).toBeInTheDocument();
    }
    const cards = screen.getByRole("region", { name: "Workspaces" });
    await userEvent.click(within(cards).getByRole("button", { name: /Sandoq Kin/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Sandoq Kin" })).toBeInTheDocument();
  });

  it("shows a waiting Customize request under Needs you and opens Customize to review it", async () => {
    const asked = {
      ...sample.registry,
      requests: [
        {
          id: "8f2d7c1e-2b3a-4c5d-8e9f-0a1b2c3d4e5f",
          kind: "skill",
          item_id: "triage-failing-build",
          requested_at: "2026-10-18T11:00:00Z",
          stale: false,
        },
      ],
    };
    serve((path) => (path === "/api/registry" ? new Response(JSON.stringify(asked), { status: 200 }) : undefined));
    render(<App />);
    const needs = await screen.findByRole("region", { name: "Needs you" });
    expect(await within(needs).findByText(/switch on the skill “triage-failing-build”/)).toBeInTheDocument();
    await userEvent.click(within(needs).getByRole("button", { name: "Review" }));
    expect(screen.getByRole("button", { name: "Customize" })).toHaveAttribute("aria-current", "page");
  });

  it("says plainly what is not set up yet, never made-up agents or summaries", async () => {
    render(<App />);
    const cards = await screen.findByRole("region", { name: "Workspaces" });
    expect(await within(cards).findAllByText("Agents: none yet")).toHaveLength(sample.projects.length);
    expect(within(cards).getAllByText("Daily summary: not set up yet")).toHaveLength(sample.projects.length);
  });

  it("shows each workspace's summary line on its Board card, and the summary on its Overview", async () => {
    const line = { day: "2026-10-25", made_at: "2026-10-25T01:00:00Z", health: "warn", line: "Needs a look." };
    const lines = { connected: true, workspaces: { zaaheen: line } };
    const days = { connected: true, days: [{ day: "2026-10-25", made_at: line.made_at, line: "Needs a look.", summary: null }] };
    serve((path) => {
      if (path === "/api/summaries") return new Response(JSON.stringify(lines), { status: 200 });
      if (path === "/api/workspaces/zaaheen/summaries") return new Response(JSON.stringify(days), { status: 200 });
      return undefined;
    });
    render(<App />);
    const cards = await screen.findByRole("region", { name: "Workspaces" });
    expect(await within(cards).findByText("Sun 25 Oct: Needs a look.")).toBeInTheDocument();
    expect(within(cards).getAllByText("Daily summary: none yet (made at 05:00)")).toHaveLength(
      sample.projects.length - 1,
    );
    await openWorkspace("Zaaheen");
    const summary = await screen.findByRole("region", { name: "Daily summary" });
    expect(await within(summary).findByRole("alert")).toHaveTextContent("could not be read");
  });

  it("has tabs: the cards, no agents yet, the open alerts, and the connected sources", async () => {
    await startOn("Zaaheen");
    expect(screen.getByText("Everything on this page belongs to Zaaheen only.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");

    await userEvent.click(screen.getByRole("tab", { name: "Agents" }));
    expect(screen.getByRole("heading", { name: "No agents yet" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Alerts" }));
    const alerts = sample.details.zaaheen.cards.flatMap((c) =>
      c.entries.filter((e) => e.tone === "alert" || e.tone === "warn"),
    );
    const panel = screen.getByRole("tabpanel");
    if (alerts.length === 0) expect(panel).toHaveTextContent("No open alerts.");
    for (const alert of alerts) expect(panel).toHaveTextContent(alert.text);

    await userEvent.click(screen.getByRole("tab", { name: "Sources" }));
    const used = sample.registry.connectors.filter((c) => c.uses.some((u) => u.project_id === "zaaheen"));
    const sources = screen.getByRole("tabpanel");
    if (used.length === 0) expect(sources).toHaveTextContent("No sources connected yet.");
    for (const connector of used) expect(sources).toHaveTextContent(connector.name);
  });

  it("lists only this workspace's sources, never another's", async () => {
    await startOn("Agent X");
    await userEvent.click(screen.getByRole("tab", { name: "Sources" }));
    const sources = screen.getByRole("tabpanel");
    expect(sources).toHaveTextContent("GitHub");
    expect(sources).not.toHaveTextContent("Paddle"); // Zaaheen's, not Agent X's
  });

  it("starts a fresh chat in each workspace, so one workspace's talk never shows in another", async () => {
    await startOn("Zaaheen");
    await userEvent.type(screen.getByLabelText("Message"), "Only about Zaaheen{Enter}");
    expect(screen.getByText("Only about Zaaheen")).toBeInTheDocument();
    await openWorkspace("Eva");
    expect(screen.queryByText("Only about Zaaheen")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /The Board/ }));
    expect(screen.queryByText("Only about Zaaheen")).not.toBeInTheDocument();
  });
});

describe("the chat asks at the open workspace's own address", () => {
  const connected = { models: sample.models.models.map((m) => ({ ...m, available: m.id === "local-gemma" })) };

  function serveAsked(): string[] {
    const asked: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          asked.push(path);
          const body = { model_id: "local-gemma", answer: "Done.", from_sample: false, used: [] };
          return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
        }
        const body = path === "/api/models" ? connected : answer(path);
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
      }),
    );
    return asked;
  }

  it("asks Eva's chat at Eva's address and the Board's at the Board's", async () => {
    const asked = serveAsked();
    await startOn("Eva");
    await screen.findByRole("option", { name: /Gemma \(local\) · default$/ });
    await userEvent.type(screen.getByLabelText("Message"), "Why red?{Enter}");
    await screen.findByText("Done.");
    await userEvent.click(screen.getByRole("button", { name: /The Board/ }));
    await screen.findByRole("option", { name: /Gemma \(local\) · default$/ });
    await userEvent.type(screen.getByLabelText("Message"), "Any news?{Enter}");
    await screen.findByText("Done.");
    expect(asked).toEqual(["/api/workspaces/eva/chat", "/api/chat"]);
  });
});
