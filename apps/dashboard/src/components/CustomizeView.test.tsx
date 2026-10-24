import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { RegistryPage } from "../api";
import sample from "../contract/sample.json";
import { CustomizeView } from "./CustomizeView";

const page = sample.registry as RegistryPage;
const names: Record<string, string> = { zaaheen: "Zaaheen", eva: "Eva" };
const projectName = (id: string) => names[id] ?? id;

function view(shown: RegistryPage) {
  return <CustomizeView page={shown} projectName={projectName} onChange={vi.fn()} />;
}

function withUses(uses: RegistryPage["connectors"][number]["uses"]): RegistryPage {
  return { ...page, connectors: page.connectors.map((c) => (c.id === "github" ? { ...c, uses } : c)) };
}

function serve(registry: Response | null) {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) => {
      if (path === "/api/registry" && registry) return Promise.resolve(registry);
      const answers: Record<string, unknown> = {
        "/api/workspaces": sample.projects,
        "/api/models": sample.models,
        "/api/registry": sample.registry,
      };
      const id = decodeURIComponent(path.replace("/api/workspaces/", ""));
      const body = answers[path] ?? (sample.details as Record<string, unknown>)[id];
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the Customize page", () => {
  it("opens from the left column, shows read-only connectors, and goes back to a project", async () => {
    serve(null);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Customize" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Customize" })).toBeInTheDocument();
    const paddle = screen.getByRole("article", { name: "Paddle" });
    expect(paddle).toHaveTextContent("Read-only");
    expect(paddle).toHaveTextContent("Key: api_key");
    expect(within(paddle).getByRole("list", { name: "Where Paddle is used" })).toHaveTextContent(
      "ZaaheenNeeds key",
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Eva/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Eva" })).toBeInTheDocument();
  });

  it("says so when the registry cannot be loaded, and tries again next time", async () => {
    serve(new Response("{}", { status: 500 }));
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Customize" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the connectors and skills");
    serve(null);
    await userEvent.click(screen.getByRole("button", { name: /Zaaheen/ }));
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));
    expect(await screen.findByRole("article", { name: "Paddle" })).toBeInTheDocument();
  });

  it("says which projects are live, failed or need a key", () => {
    const uses = [
      { project_id: "zaaheen", status: "live" as const },
      { project_id: "eva", status: "failed" as const },
    ];
    render(view(withUses(uses)));
    const list = screen.getByRole("list", { name: "Where GitHub is used" });
    expect(list).toHaveTextContent("ZaaheenLive");
    expect(list).toHaveTextContent("EvaRead failed");
  });

  it("shows skills with what they need and when they run", async () => {
    const manual = {
      description: "Count new sign-ups.",
      fingerprint: "c".repeat(64),
      id: "check-sign-ups",
      invocation: "manual" as const,
      requires: ["clerk"],
      missing: ["clerk"],
      state: "on" as const,
    };
    render(view({ ...page, skills: [...page.skills, manual] }));
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    const triage = screen.getByRole("article", { name: "Triage failing build" });
    expect(triage).toHaveTextContent("Used when a task needs it");
    expect(triage).toHaveTextContent("Needs GitHub");
    const signups = screen.getByRole("article", { name: "Check sign ups" });
    expect(signups).toHaveTextContent("Only when you ask for it");
    expect(signups).toHaveTextContent("Needs clerk (not added yet)");
  });

  it("searches within the open tab", async () => {
    render(view(page));
    const search = screen.getByRole("searchbox", { name: "Search" });
    await userEvent.type(search, "revenue");
    expect(screen.getByRole("article", { name: "Paddle" })).toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "GitHub" })).not.toBeInTheDocument();
    await userEvent.clear(search);
    await userEvent.type(search, "nothing like this");
    expect(screen.getByText('Nothing matches "nothing like this".')).toBeInTheDocument();
  });

  it("shows plugin bundles as counts only, and an empty state without any", async () => {
    const plugin = {
      id: "kit",
      description: "",
      version: "1.0.0",
      layout: "claude" as const,
      skills: 1,
      connectors: 2,
      commands: 0,
      agents: 0,
      hooks: true,
      fingerprint: "b".repeat(64),
      state: "view_only" as const,
    };
    const { rerender } = render(view(page));
    await userEvent.click(screen.getByRole("tab", { name: /Plugins/ }));
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    expect(screen.getByText(/No plugins found/)).toBeInTheDocument();
    rerender(view({ ...page, plugins: [plugin] }));
    const card = screen.getByRole("article", { name: "kit" });
    expect(card).toHaveTextContent("View only (unsigned)");
    expect(within(card).queryByRole("button")).not.toBeInTheDocument();
    expect(card).toHaveTextContent("Claude format");
    expect(card).toHaveTextContent("1 skill · 2 connectors · 0 commands · 0 agents · hooks");
    expect(card).toHaveTextContent("Nothing in a plugin runs until you approve it.");
  });

  it("lists what could not be loaded, on its own tab", async () => {
    const problems = [{ kind: "skill" as const, path: "skills/broken", message: "the name is wrong" }];
    render(view({ ...page, problems }));
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    expect(screen.getByText("1 item could not be loaded")).toBeInTheDocument();
    expect(screen.getByText("skills/broken")).toBeInTheDocument();
  });

  it("keeps Discover honest: only what is not on, and empty when everything is", async () => {
    render(view(page));
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    expect(screen.getByText("Every connector Yaqzan has is on.")).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("without a database shows no switches and says how to get them", () => {
    render(view(page));
    expect(screen.getByText(/start the dashboard with --switches/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Switch off/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Heads up/)).not.toBeInTheDocument();
  });
});
