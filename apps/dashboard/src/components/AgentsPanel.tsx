import { Fingerprint, Plus, Power } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { ApiError, api, type AgentOut, type RoleChoice, type SwitchChange, type WorkspaceAgents } from "../api";
import { PasskeyProblem, withPasskeyIfNeeded } from "../passkey";

type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "failed" };

const REASON: Record<string, string> = {
  "agents.read_only": "Agents need the laptop database. Start the dashboard with --switches.",
  "agents.store_unavailable": "The laptop database did not answer. Is it running?",
  "agents.already_given": "This workspace already has that agent.",
  "agents.unknown_role": "That role is not in the library any more.",
  "agents.unknown_agent": "That agent is not in this workspace.",
  "agents.not_allowed": "That is not allowed from here.",
  "agents.passkey_failed": "Windows Hello did not confirm it, so nothing changed. Please try again.",
  "agents.bad_cap": "A cap must be zero or more.",
  "registry.no_passkey": "Set up your passkey first (start the dashboard with --enrol-passkey).",
};
const PROBLEM: Record<PasskeyProblem["reason"], string> = {
  unsupported: "This browser can't use Windows Hello here. Use Edge or Chrome.",
  wrong_address: "Open the dashboard at http://localhost to use Windows Hello.",
  cancelled: "Windows Hello was closed, so nothing changed.",
};

export function money(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

// A cap in words: "not set" means nothing may be spent.
export function capWords(cents: number | null): string {
  return cents === null ? "not set" : money(cents);
}

// Dollars typed by a person, as cents; empty means no cap (then nothing may be spent).
export function centsFrom(text: string): number | null | "bad" {
  const trimmed = text.trim().replace(/^\$/, "");
  if (trimmed === "") return null;
  if (!/^\d{1,7}(\.\d{1,2})?$/.test(trimmed)) return "bad";
  return Math.round(Number(trimmed) * 100);
}

function said(failure: unknown): string {
  if (failure instanceof PasskeyProblem) return PROBLEM[failure.reason];
  const code = failure instanceof ApiError ? failure.code : null;
  return (code && REASON[code]) ?? "That did not work. Please try again.";
}

const BUTTON = [
  "inline-flex min-h-11 items-center gap-2 rounded-lg border border-line bg-panel px-4 text-sm font-medium text-ink",
  "hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50",
].join(" ");

interface Props {
  slug: string;
  name: string;
}

// The Agents tab (ADR-039): this workspace's agents, its switch and its monthly cap.
// Going riskier (on, a higher cap) asks Windows Hello to sign that exact change; going safer never does.
export function AgentsPanel({ slug, name }: Props) {
  const [loaded, setLoaded] = useState<Load<WorkspaceAgents>>({ state: "loading" });
  const [roles, setRoles] = useState<RoleChoice[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.agents(slug).then(
      (data) => {
        if (live) setLoaded({ state: "ready", data });
      },
      () => {
        if (live) setLoaded({ state: "failed" });
      },
    );
    api.roles().then(
      (data) => {
        if (live) setRoles(data);
      },
      () => {
        if (live) setRoles([]);
      },
    );
    return () => {
      live = false;
    };
  }, [slug]);

  async function act(change: () => Promise<WorkspaceAgents>) {
    setBusy(true);
    setProblem(null);
    try {
      setLoaded({ state: "ready", data: await change() });
    } catch (failure) {
      setProblem(said(failure));
    } finally {
      setBusy(false);
    }
  }

  const setWorkspace = (change: SwitchChange) =>
    act(() =>
      withPasskeyIfNeeded(
        () => api.setWorkspace(slug, change),
        () => api.workspaceOptions(slug, change),
        (credential) => api.setWorkspace(slug, change, credential),
      ),
    );
  const setAgent = (agent: AgentOut, change: SwitchChange) =>
    act(() =>
      withPasskeyIfNeeded(
        () => api.setAgent(slug, agent.id, change),
        () => api.agentOptions(slug, agent.id, change),
        (credential) => api.setAgent(slug, agent.id, change, credential),
      ),
    );

  if (loaded.state === "loading") return <p className="text-sm text-muted">Loading…</p>;
  if (loaded.state === "failed") {
    return (
      <p role="alert" className="text-sm text-alert">
        Could not load the agents.
      </p>
    );
  }
  const data = loaded.data;
  if (!data.switches) {
    return (
      <div className="rounded-2xl border border-dashed border-line bg-panel p-5">
        <h2 className="font-semibold text-ink">No agents yet</h2>
        <p className="mt-1 text-sm text-muted">{REASON["agents.read_only"]}</p>
      </div>
    );
  }
  const given = new Set(data.agents.map((a) => a.role_id));
  const free = roles.filter((r) => !given.has(r.id));

  return (
    <div className="flex flex-col gap-5">
      {problem !== null && (
        <p role="alert" className="rounded-xl border border-alert bg-alert-soft px-4 py-2.5 text-sm text-ink">
          {problem}
        </p>
      )}

      <section aria-labelledby="workspace-switch" className="rounded-2xl border border-line bg-panel p-5">
        <h2 id="workspace-switch" className="font-semibold text-ink">
          {name}'s agents are {data.enabled ? "on" : "paused"}
        </h2>
        <p className="mt-1 text-sm text-muted">
          Monthly cap {capWords(data.cap_minor)} · spent {money(data.spent_minor)} this month.
          {data.cap_minor === null && " Nothing can spend until you set a cap."}
        </p>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <button
            type="button"
            className={BUTTON}
            disabled={busy}
            onClick={() => void setWorkspace({ enabled: !data.enabled, cap_minor: data.cap_minor })}
          >
            {data.enabled ? <Power className="size-4" aria-hidden="true" /> : <Fingerprint className="size-4" aria-hidden="true" />}
            {data.enabled ? `Pause ${name}` : `Switch ${name} on`}
          </button>
          <CapField
            label={`Monthly cap for ${name} (USD)`}
            cap={data.cap_minor}
            disabled={busy}
            onSave={(cap) => void setWorkspace({ enabled: data.enabled, cap_minor: cap })}
          />
        </div>
      </section>

      <section aria-labelledby="agent-list" className="flex flex-col gap-3">
        <h2 id="agent-list" className="font-semibold text-ink">
          Agents
        </h2>
        {data.agents.length === 0 ? (
          <p className="text-sm text-muted">
            No agents yet. Nothing runs until you add one, switch it on and give it a cap.
          </p>
        ) : (
          <ul className="flex flex-col divide-y divide-line rounded-2xl border border-line bg-panel">
            {data.agents.map((agent) => (
              <li key={agent.id} className="flex flex-col gap-3 px-5 py-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="font-medium text-ink">{agent.title}</p>
                    <p className="text-sm text-muted">
                      {agent.enabled ? "On" : "Off"} · Model: Gemma (local) · cap {capWords(agent.cap_minor)} · spent{" "}
                      {money(agent.spent_minor)} this month
                    </p>
                    {agent.newer_version && (
                      <p className="text-sm text-muted">Its role pack changed; it keeps running the copy it was given.</p>
                    )}
                  </div>
                  <button
                    type="button"
                    className={BUTTON}
                    disabled={busy}
                    onClick={() => void setAgent(agent, { enabled: !agent.enabled, cap_minor: agent.cap_minor })}
                  >
                    {agent.enabled ? (
                      <Power className="size-4" aria-hidden="true" />
                    ) : (
                      <Fingerprint className="size-4" aria-hidden="true" />
                    )}
                    {agent.enabled ? `Switch ${agent.title} off` : `Switch ${agent.title} on`}
                  </button>
                </div>
                <CapField
                  label={`Monthly cap for ${agent.title} (USD)`}
                  cap={agent.cap_minor}
                  disabled={busy}
                  onSave={(cap) => void setAgent(agent, { enabled: agent.enabled, cap_minor: cap })}
                />
              </li>
            ))}
          </ul>
        )}
        <AddAgent roles={free} disabled={busy} onAdd={(id) => void act(() => api.giveAgent(slug, id))} />
      </section>
    </div>
  );
}

interface CapProps {
  label: string;
  cap: number | null;
  disabled: boolean;
  onSave: (cap: number | null) => void;
}

function CapField({ label, cap, disabled, onSave }: CapProps) {
  const id = useId();
  const [text, setText] = useState(cap === null ? "" : (cap / 100).toFixed(2));
  const parsed = centsFrom(text);
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (parsed !== "bad") onSave(parsed);
      }}
    >
      <label htmlFor={id} className="flex flex-col gap-1 text-sm text-muted">
        {label}
        <input
          id={id}
          inputMode="decimal"
          value={text}
          placeholder="not set"
          aria-invalid={parsed === "bad"}
          onChange={(event) => {
            setText(event.target.value);
          }}
          className="min-h-11 w-36 rounded-lg border border-line bg-panel px-3 text-ink"
        />
      </label>
      <button type="submit" className={BUTTON} disabled={disabled || parsed === "bad" || parsed === cap}>
        Save cap
      </button>
    </form>
  );
}

interface AddProps {
  roles: RoleChoice[];
  disabled: boolean;
  onAdd: (id: string) => void;
}

function AddAgent({ roles, disabled, onAdd }: AddProps) {
  const id = useId();
  const [chosen, setChosen] = useState("");
  if (roles.length === 0) return null;
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (chosen !== "") onAdd(chosen);
      }}
    >
      <label htmlFor={id} className="flex flex-col gap-1 text-sm text-muted">
        Add an agent from the role library
        <select
          id={id}
          value={chosen}
          onChange={(event) => {
            setChosen(event.target.value);
          }}
          className="min-h-11 rounded-lg border border-line bg-panel px-3 text-ink"
        >
          <option value="">Choose a role…</option>
          {roles.map((role) => (
            <option key={role.id} value={role.id}>
              {role.title} ({role.permission})
            </option>
          ))}
        </select>
      </label>
      <button
        type="submit"
        disabled={disabled || chosen === ""}
        className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-brand px-4 text-sm font-medium text-on-brand hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50"
      >
        <Plus className="size-4" aria-hidden="true" />
        Add agent
      </button>
    </form>
  );
}
