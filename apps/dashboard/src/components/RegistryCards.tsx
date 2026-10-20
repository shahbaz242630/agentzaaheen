import { KeyRound, ShieldCheck, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import type { ConnectorItem, ItemState, Level, PluginItem, SkillItem, Use } from "../api";
import { StatePill } from "./RegistrySwitch";

const USE: Record<Use, { dot: string; label: string }> = {
  live: { dot: "bg-ok", label: "Live" },
  failed: { dot: "bg-alert", label: "Read failed" },
  needs_key: { dot: "bg-unknown", label: "Needs key" },
};

const LEVEL: Record<Level, string> = {
  read: "Read-only",
  draft: "Drafts: needs approval",
  act: "Can act: needs approval",
  forbidden: "Blocked",
};

const LAYOUT = { claude: "Claude format", codex: "Codex format", cursor: "Cursor format" } as const;

// "triage-failing-build" → "Triage failing build"
export function skillTitle(id: string): string {
  const words = id.replaceAll("-", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function plural(count: number, one: string): string {
  return `${count} ${one}${count === 1 ? "" : "s"}`;
}

function Pill({ tone = "plain", children }: { tone?: "plain" | "ok" | "warn"; children: ReactNode }) {
  const look = {
    plain: "bg-raised text-muted",
    ok: "bg-ok-soft text-ink",
    warn: "bg-warn-soft text-ink",
  }[tone];
  return <span className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ${look}`}>{children}</span>;
}

interface ShellProps {
  id: string;
  title: string;
  tag: string;
  description: string;
  fingerprint: string;
  version?: string | null;
  state: ItemState;
  control?: ReactNode;
  children: ReactNode;
}

function Shell({ id, title, tag, description, fingerprint, version, state, control, children }: ShellProps) {
  return (
    <article
      aria-labelledby={`item-${id}`}
      className="flex flex-col gap-3 rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]"
    >
      <header className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className="grid size-10 shrink-0 place-items-center rounded-xl bg-brand-soft text-lg font-semibold text-brand-ink"
        >
          {title.charAt(0).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <h3 id={`item-${id}`} className="font-semibold text-ink">
            {title}
          </h3>
          <p className="text-xs text-muted">{tag}</p>
        </div>
        <StatePill state={state} />
      </header>
      <p className="line-clamp-4 text-sm text-muted" title={description}>
        {description}
      </p>
      {children}
      {control}
      <p className="mt-auto pt-1 font-mono text-[0.7rem] text-faint" title={`Fingerprint ${fingerprint}`}>
        {version ? `v${version} · ` : ""}
        {fingerprint.slice(0, 12)}
      </p>
    </article>
  );
}

interface CardProps<T> {
  item: T;
  control?: ReactNode;
}

export function ConnectorCard({
  item,
  control,
  projectName,
}: CardProps<ConnectorItem> & { projectName: (id: string) => string }) {
  return (
    <Shell
      id={item.id}
      title={item.name}
      tag={item.category}
      description={item.description}
      fingerprint={item.fingerprint}
      version={item.version}
      state={item.state}
      control={control}
    >
      <div className="flex flex-wrap gap-1.5">
        <Pill tone={item.level === "read" ? "ok" : "warn"}>
          {item.level === "read" ? <ShieldCheck className="size-3.5" aria-hidden="true" /> : <TriangleAlert className="size-3.5" aria-hidden="true" />}
          {LEVEL[item.level]}
        </Pill>
        {item.key_slots.length > 0 && (
          <Pill>
            <KeyRound className="size-3.5" aria-hidden="true" />
            Key: {item.key_slots.join(", ")}
          </Pill>
        )}
      </div>
      {item.uses.length > 0 ? (
        <ul aria-label={`Where ${item.name} is used`} className="flex flex-col gap-1 text-sm">
          {item.uses.map((use) => (
            <li key={use.project_id} className="flex items-center justify-between gap-3">
              <span className="truncate text-ink">{projectName(use.project_id)}</span>
              <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted">
                <span className={`size-1.5 rounded-full ${USE[use.status].dot}`} aria-hidden="true" />
                {USE[use.status].label}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-faint">Not used by any project yet.</p>
      )}
      <p className="text-xs text-faint">Reads from {item.hosts.join(", ")}</p>
    </Shell>
  );
}

export function SkillCard({
  item,
  control,
  connectorName,
}: CardProps<SkillItem> & { connectorName: (id: string) => string }) {
  return (
    <Shell
      id={item.id}
      title={skillTitle(item.id)}
      tag="Skill"
      description={item.description}
      fingerprint={item.fingerprint}
      state={item.state}
      control={control}
    >
      <div className="flex flex-wrap gap-1.5">
        <Pill>{item.invocation === "auto" ? "Used when a task needs it" : "Only when you ask for it"}</Pill>
        {item.requires.map((id) =>
          item.missing.includes(id) ? (
            <Pill key={id} tone="warn">
              <TriangleAlert className="size-3.5" aria-hidden="true" />
              Needs {id} (not added yet)
            </Pill>
          ) : (
            <Pill key={id}>Needs {connectorName(id)}</Pill>
          ),
        )}
      </div>
    </Shell>
  );
}

export function PluginCard({ item }: CardProps<PluginItem>) {
  const parts = [
    plural(item.skills, "skill"),
    plural(item.connectors, "connector"),
    plural(item.commands, "command"),
    plural(item.agents, "agent"),
  ];
  if (item.hooks) parts.push("hooks");
  return (
    <Shell
      id={item.id}
      title={item.id}
      tag={LAYOUT[item.layout]}
      description={item.description || "No description."}
      fingerprint={item.fingerprint}
      version={item.version}
      state={item.state}
    >
      <p className="text-sm text-ink">{parts.join(" · ")}</p>
      <p className="text-xs text-faint">Shown only. Nothing in a plugin runs until you approve it.</p>
    </Shell>
  );
}
