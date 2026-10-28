// The Customize page's MCP tab (ADR-041 slice 1): add a custom MCP server by
// name and https address, test it, remove it. Adding is only a request:
// nothing can use a server until it is approved with Windows Hello (the next
// step). A test connects, lists the server's tools and disconnects; it sends
// none of our keys or text. The tool names and descriptions are the server's
// own words, shown as plain text and marked as not checked.
import { Plug, Search, Trash2, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { ApiError, type McpPage, type McpServer } from "../api";
import { ago } from "../format";

const NAME = /^[a-z][a-z0-9_-]{0,31}$/;

const OUTCOME: Record<string, string> = {
  "mcp.needs_key": "It wants a key. Keys for custom servers come with the next step (approval), so it can't be listed yet.",
  "mcp.no_answer": "It did not answer within 8 seconds, or its name could not be found.",
  "mcp.blocked":
    "Refused before anything was sent: the address points inside a private network, or is not a plain public https address.",
  "mcp.busy": "It said it was busy. Try again later.",
  "mcp.server_error": "It had an error of its own.",
  "mcp.refused": "It refused the request. There may be no MCP server at that address.",
  "mcp.too_big": "Its answer was too big (over 500 tools, or over 2 MB).",
  "mcp.not_mcp": "It answered, but not as an MCP server.",
};

const REFUSED: Record<string, string> = {
  "mcp.bad_name": "Use lowercase letters, digits, - or _, starting with a letter (32 at most).",
  "mcp.reserved_name": "That name belongs to something Agent Zaaheen already has. Pick another.",
  "mcp.name_taken": "A server with that name is already added (or the list of 50 is full).",
  "mcp.bad_address": "That address can't be used.",
  "mcp.unknown_server": "That server is no longer on the list.",
  "mcp.not_available": "Custom servers need the laptop database: start the dashboard with --switches.",
  "mcp.store_unavailable": "The laptop database did not answer. Try again in a moment.",
};

export function mcpProblem(failure: unknown): string {
  if (failure instanceof ApiError && failure.code !== null) {
    const known = REFUSED[failure.code];
    if (failure.code === "mcp.bad_address" && failure.why !== null) return `That address can't be used: ${failure.why}.`;
    if (known !== undefined) return known;
  }
  return "That didn't work. Try again in a moment.";
}

function plural(count: number, one: string): string {
  return `${count} ${one}${count === 1 ? "" : "s"}`;
}

function TestResult({ server }: { server: McpServer }) {
  const { test } = server;
  if (test === null) return <p className="text-sm text-muted">Not tested yet. Test lists its tools without using any.</p>;
  if (test.outcome !== "mcp.listed" || test.tools === null) {
    return (
      <p className="flex items-start gap-1.5 text-sm text-muted">
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
        <span>
          Tested {ago(test.at)}: {OUTCOME[test.outcome] ?? "the test failed."}
        </span>
      </p>
    );
  }
  const skipped = test.skipped ?? 0;
  return (
    <details className="text-sm">
      <summary className="cursor-pointer text-muted hover:text-ink">
        Tested {ago(test.at)}: {plural(test.tools.length, "tool")}
        {test.server_name ? ` from "${test.server_name}"` : ""}
        {skipped > 0 ? ` (${plural(skipped, "tool")} left out: unusable names)` : ""}
      </summary>
      <p className="mt-2 text-xs text-faint">What the server says about its tools, in its own words. Not checked.</p>
      <ul className="mt-1.5 flex max-h-64 flex-col gap-1.5 overflow-y-auto">
        {test.tools.map((tool) => (
          <li key={tool.name} className="rounded-lg bg-raised px-2.5 py-1.5">
            <span className="font-mono text-xs text-ink">{tool.name}</span>
            {tool.description !== "" && (
              <p className="line-clamp-3 text-xs whitespace-pre-line text-muted" title={tool.description}>
                {tool.description}
              </p>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

interface Props {
  page: McpPage;
  busy: boolean;
  onAdd: (name: string, url: string) => void;
  onTest: (name: string) => void;
  onRemove: (name: string) => void;
}

export function McpPanel({ page, busy, onAdd, onTest, onRemove }: Props) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const cleanName = name.trim();
  const cleanUrl = url.trim();
  const nameOk = NAME.test(cleanName);
  const urlOk = cleanUrl.startsWith("https://") && cleanUrl.length <= 512;

  if (!page.available) {
    return <p className="text-muted">{REFUSED["mcp.not_available"]}</p>;
  }
  return (
    <div className="flex flex-col gap-4">
      <section
        aria-labelledby="mcp-add-title"
        className="rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]"
      >
        <h2 id="mcp-add-title" className="flex items-center gap-2 font-semibold text-ink">
          <Plug className="size-4 text-faint" aria-hidden="true" />
          Add custom MCP
        </h2>
        <p className="mt-0.5 text-sm text-muted">
          For an MCP server that isn't in our list. Adding only files a request: nothing can use it until you approve
          it with Windows Hello, and every tool counts as acting until you mark it as only reading. Public https
          addresses only, for now.
        </p>
        <form
          className="mt-3 flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!nameOk || !urlOk) return;
            onAdd(cleanName, cleanUrl);
            setName("");
            setUrl("");
          }}
        >
          <label className="flex items-center gap-2 rounded-xl border border-line bg-panel px-3 py-1.5 focus-within:outline-2 focus-within:outline-brand">
            <span className="text-sm text-muted">Name</span>
            <input
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
              autoComplete="off"
              spellCheck={false}
              maxLength={32}
              placeholder="web-search"
              className="w-32 bg-transparent font-mono text-sm text-ink outline-none placeholder:text-faint"
            />
          </label>
          <label className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-line bg-panel px-3 py-1.5 focus-within:outline-2 focus-within:outline-brand">
            <span className="text-sm text-muted">Address</span>
            <input
              type="url"
              value={url}
              onChange={(event) => {
                setUrl(event.target.value);
              }}
              autoComplete="off"
              spellCheck={false}
              maxLength={512}
              placeholder="https://mcp.example.com/mcp"
              className="min-w-0 flex-1 bg-transparent font-mono text-sm text-ink outline-none placeholder:text-faint"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !nameOk || !urlOk}
            className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-on-brand hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
          >
            Add
          </button>
          {cleanName.length > 0 && !nameOk && <span className="w-full text-sm text-muted">{REFUSED["mcp.bad_name"]}</span>}
          {cleanUrl.length > 0 && !cleanUrl.startsWith("https://") && (
            <span className="w-full text-sm text-muted">The address must start with https://</span>
          )}
        </form>
      </section>

      {page.servers.length === 0 ? (
        <p className="text-muted">No custom MCP servers added.</p>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(20rem,1fr))] items-start gap-4">
          {page.servers.map((server) => (
            <article
              key={server.name}
              aria-labelledby={`mcp-${server.name}`}
              className="flex flex-col gap-3 rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]"
            >
              <header className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <h3 id={`mcp-${server.name}`} className="font-mono font-semibold text-ink">
                    {server.name}
                  </h3>
                  <p className="truncate font-mono text-xs text-muted" title={server.url}>
                    {server.url}
                  </p>
                </div>
                <span className="inline-flex shrink-0 items-center rounded-md bg-warn-soft px-2 py-0.5 text-xs font-medium text-ink">
                  Requested · not usable
                </span>
              </header>
              <TestResult server={server} />
              <p className="text-xs text-faint">Added {ago(server.added_at)}</p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    onTest(server.name);
                  }}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-ink hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Search className="size-4" aria-hidden="true" />
                  {server.test === null ? "Test" : "Test again"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  aria-label={`Remove ${server.name}`}
                  onClick={() => {
                    onRemove(server.name);
                  }}
                  className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-muted hover:bg-raised hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Trash2 className="size-4" aria-hidden="true" />
                  Remove
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
