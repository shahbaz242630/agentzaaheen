import { useEffect, useState } from "react";

import { api, type ProjectSummary, type RegistryPage, type WorkspaceAgents } from "../api";
import { capWords, money } from "./AgentsPanel";
import { HealthPill } from "./health";
import { ArchivedProjects } from "./RemoveProject";

// One line about a workspace's agents: honest when there are none, or when they can't be read.
export function agentLine(found: WorkspaceAgents | "failed" | undefined): string {
  if (found === undefined) return "Agents: …";
  if (found === "failed") return "Agents: could not be read";
  if (found.agents.length === 0) return "Agents: none yet";
  const on = found.agents.filter((a) => a.enabled).length;
  const counts = `${String(found.agents.length)} (${String(on)} on)`;
  return `Agents: ${counts} · spent ${money(found.spent_minor)} · cap ${capWords(found.cap_minor)}`;
}

interface Props {
  workspaces: readonly ProjectSummary[];
  // The Customize page's waiting requests; ``null`` while unknown (not loaded, or it failed).
  registry: RegistryPage | null;
  onOpen: (slug: string) => void;
  onCustomize: () => void;
  // What just happened to a project that was removed, shown once at the top.
  notice: string | null;
  // A project was brought back: the workspace list must be read again.
  onChanged: () => void;
}

const KIND_WORDS = { connector: "connector", skill: "skill", plugin: "plugin bundle" } as const;

// The Board (ADR-039): every workspace's summary line and what needs the Chairman, never a workspace's raw data.
export function BoardView({ workspaces, registry, onOpen, onCustomize, notice, onChanged }: Props) {
  const [agents, setAgents] = useState<Record<string, WorkspaceAgents | "failed">>({});
  useEffect(() => {
    let live = true;
    for (const w of workspaces) {
      api.agents(w.id).then(
        (data) => {
          if (live) setAgents((seen) => ({ ...seen, [w.id]: data }));
        },
        () => {
          if (live) setAgents((seen) => ({ ...seen, [w.id]: "failed" }));
        },
      );
    }
    return () => {
      live = false;
    };
  }, [workspaces]);
  const waiting = workspaces.filter((w) => w.escalations > 0);
  const requests = registry?.requests ?? [];
  const nothing = waiting.length === 0 && requests.length === 0;
  return (
    <section aria-labelledby="board-title" className="flex flex-col gap-6">
      <header>
        <h1 id="board-title" className="text-2xl font-semibold tracking-tight text-ink">
          The Board
        </h1>
        <p className="mt-1 text-muted">
          You and the CEO. You see each workspace's summary line here, never its raw data.
        </p>
      </header>

      {notice !== null && (
        <p role="status" className="rounded-xl border border-line bg-raised px-4 py-2.5 text-sm text-ink">
          {notice}
        </p>
      )}

      <section aria-labelledby="needs-you" className="rounded-2xl border border-line bg-panel p-5">
        <h2 id="needs-you" className="font-semibold text-ink">
          Needs you
        </h2>
        {nothing ? (
          <p className="mt-2 text-sm text-muted">Nothing needs you right now.</p>
        ) : (
          <ul className="mt-2 divide-y divide-line">
            {waiting.map((w) => (
              <li key={w.id} className="flex flex-wrap items-center gap-3 py-3">
                <span className="rounded-full bg-raised px-2.5 py-0.5 text-xs font-semibold text-ink">{w.name}</span>
                <span className="min-w-0 flex-1 text-sm text-ink">
                  {w.escalations === 1 ? "1 thing is waiting for you." : `${w.escalations} things are waiting for you.`}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    onOpen(w.id);
                  }}
                  className="min-h-11 rounded-lg border border-line bg-panel px-4 text-sm font-medium text-ink hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                >
                  Open {w.name}
                </button>
              </li>
            ))}
            {requests.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-3 py-3">
                <span className="rounded-full bg-raised px-2.5 py-0.5 text-xs font-semibold text-ink">Customize</span>
                <span className="min-w-0 flex-1 text-sm text-ink">
                  A request to switch on the {KIND_WORDS[r.kind]} “{r.item_id}” is waiting for your approval.
                </span>
                <button
                  type="button"
                  onClick={onCustomize}
                  className="min-h-11 rounded-lg border border-line bg-panel px-4 text-sm font-medium text-ink hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                >
                  Review
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="board-workspaces" className="flex flex-col gap-3">
        <h2 id="board-workspaces" className="font-semibold text-ink">
          Workspaces
        </h2>
        {workspaces.length === 0 ? (
          <p className="text-sm text-muted">No workspaces yet.</p>
        ) : (
          <ul className="grid grid-cols-[repeat(auto-fill,minmax(17rem,1fr))] gap-4">
            {workspaces.map((w) => (
              <li key={w.id}>
                <button
                  type="button"
                  onClick={() => {
                    onOpen(w.id);
                  }}
                  className="flex h-full w-full flex-col gap-3 rounded-2xl border border-line bg-panel p-5 text-left hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                >
                  <span className="flex items-center justify-between gap-3">
                    <span className="text-lg font-semibold text-ink">{w.name}</span>
                    <HealthPill health={w.health} />
                  </span>
                  <span className="text-sm text-muted">{w.summary}</span>
                  <span className="text-sm text-muted">{agentLine(agents[w.id])}</span>
                  <span className="text-sm text-muted">Daily summary: not set up yet</span>
                  <span className="border-t border-line pt-3 text-sm text-muted">
                    {w.escalations === 0 ? "Nothing waiting for you" : `${w.escalations} waiting for you`}
                    {w.sample ? " · sample data" : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <ArchivedProjects onChanged={onChanged} />
    </section>
  );
}
