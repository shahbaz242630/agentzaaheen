import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { McpPage, McpServer, McpTest, RegistryPage } from "../api";
import sample from "../contract/sample.json";
import { CustomizeView } from "./CustomizeView";
import { McpPanel } from "./McpPanel";

const registry = { ...(sample.registry as RegistryPage), switches: true };
const AT = "2026-10-28T12:00:00Z";
const KEY = { switches: true, vault: true, saved: false, saved_at: null };

const server: McpServer = {
  name: "web",
  url: "https://mcp.example.com/mcp",
  status: "requested",
  added_at: AT,
  test: null,
};
const added: McpPage = { available: true, servers: [server] };
const found: McpTest = {
  at: AT,
  outcome: "mcp.listed",
  server_name: "Example",
  tools: [{ name: "search", description: "<img src=x onerror=alert(1)> Search the web." }],
  skipped: 1,
  fingerprint: "c".repeat(64),
};

const listed: McpPage = { available: true, servers: [{ ...server, test: found }] };

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), { status }));
}

// The Customize page's own reads, plus the MCP routes answered from `answers` (by "METHOD path").
function serve(answers: Record<string, () => Promise<Response>>) {
  const fetchMock = vi.fn((path: string, init?: RequestInit) => {
    const asked = `${init?.method ?? "GET"} ${path}`;
    const answer = answers[asked];
    if (answer) return answer();
    if (path === "/api/passkey") return json({ enrolled: true, enrolment_open: false });
    if (["/api/claude-key", "/api/server-token", "/api/model-key"].includes(path)) return json(KEY);
    return json({ detail: "not.found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function view() {
  return <CustomizeView page={registry} projectName={(id) => id} onChange={vi.fn()} />;
}

async function openTab() {
  await userEvent.click(await screen.findByRole("tab", { name: /MCP/ }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the MCP tab", () => {
  it("is there only when switches are on (the laptop database)", () => {
    serve({});
    render(<CustomizeView page={{ ...registry, switches: false }} projectName={(id) => id} onChange={vi.fn()} />);
    expect(screen.queryByRole("tab", { name: /MCP/ })).not.toBeInTheDocument();
  });

  it("adds a server as a request and says nothing can use it", async () => {
    const fetchMock = serve({
      "GET /api/mcp": () => json({ available: true, servers: [] }),
      "POST /api/mcp": () => json(added),
    });
    render(view());
    await openTab();
    expect(screen.getByText("No custom MCP servers added.")).toBeInTheDocument();
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "web");
    await userEvent.type(screen.getByRole("textbox", { name: "Address" }), "https://mcp.example.com/mcp");
    await userEvent.click(add);
    const card = await screen.findByRole("article", { name: "web" });
    expect(card).toHaveTextContent("Requested · not usable");
    expect(card).toHaveTextContent("Not tested yet");
    const sent = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(sent?.[1]?.body as string)).toEqual({ name: "web", url: "https://mcp.example.com/mcp" });
  });

  it("won't send an odd name or a plain http address", async () => {
    serve({ "GET /api/mcp": () => json({ available: true, servers: [] }) });
    render(view());
    await openTab();
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Web Search");
    await userEvent.type(screen.getByRole("textbox", { name: "Address" }), "http://mcp.example.com/");
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(screen.getByText(/Use lowercase letters/)).toBeInTheDocument();
    expect(screen.getByText("The address must start with https://")).toBeInTheDocument();
  });

  it("explains a refusal in plain words", async () => {
    serve({
      "GET /api/mcp": () => json({ available: true, servers: [] }),
      "POST /api/mcp": () =>
        json({ detail: { code: "mcp.bad_address", why: "only the default https port is allowed" } }, 422),
    });
    render(view());
    await openTab();
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "web");
    await userEvent.type(screen.getByRole("textbox", { name: "Address" }), "https://mcp.example.com:8443/");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That address can't be used: only the default https port is allowed.",
    );
  });

  it("tests a server and shows its tools as the server's own, unchecked words", async () => {
    serve({ "GET /api/mcp": () => json(added), "POST /api/mcp/web/test": () => json(listed) });
    render(view());
    await openTab();
    await userEvent.click(await screen.findByRole("button", { name: "Test" }));
    const card = await screen.findByRole("article", { name: "web" });
    expect(card).toHaveTextContent('1 tool from "Example" (1 tool left out: unusable names)');
    await userEvent.click(within(card).getByText(/1 tool from/));
    expect(within(card).getByText(/in its own words\. Not checked\./)).toBeInTheDocument();
    expect(within(card).getByText(/<img src=x onerror=alert\(1\)> Search the web\./)).toBeInTheDocument();
    expect(card.querySelector("img")).toBeNull(); // shown as text, never as markup
  });

  it("says why a test found nothing", () => {
    const failed: McpPage = {
      available: true,
      servers: [{ ...server, test: { ...found, outcome: "mcp.needs_key", tools: null } }],
    };
    render(<McpPanel page={failed} busy={false} onAdd={vi.fn()} onTest={vi.fn()} onRemove={vi.fn()} />);
    expect(screen.getByRole("article", { name: "web" })).toHaveTextContent(/It wants a key/);
  });

  it("removes a server", async () => {
    serve({
      "GET /api/mcp": () => json(added),
      "POST /api/mcp/web/remove": () => json({ available: true, servers: [] }),
    });
    render(view());
    await openTab();
    await userEvent.click(await screen.findByRole("button", { name: "Remove web" }));
    expect(await screen.findByText("No custom MCP servers added.")).toBeInTheDocument();
  });
});
