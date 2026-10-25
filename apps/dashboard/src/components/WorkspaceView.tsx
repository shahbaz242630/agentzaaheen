import { FlaskConical } from "lucide-react";
import { useId } from "react";

import type { Card, ProjectDetail, RegistryPage, Tone, Use } from "../api";
import { AgentsPanel } from "./AgentsPanel";
import { BuiltWith } from "./BuiltWith";
import { CardView } from "./CardView";
import { HealthPill } from "./health";
import { RemoveButton } from "./RemoveProject";
import { DailySummaryPanel } from "./Summaries";

export const TABS = ["overview", "agents", "alerts", "sources"] as const;
export type Tab = (typeof TABS)[number];

const TAB_LABEL: Record<Tab, string> = { overview: "Overview", agents: "Agents", alerts: "Alerts", sources: "Sources" };
const USE_WORDS: Record<Use, string> = { live: "connected", failed: "could not be read", needs_key: "needs a key" };
const ALERT_TONES: readonly Tone[] = ["alert", "warn"];
const TONE_DOT: Record<Tone, string> = { ok: "bg-ok", info: "bg-info", warn: "bg-warn", alert: "bg-alert" };

interface Props {
  detail: ProjectDetail;
  // The Customize page's connectors, for the Sources tab; ``null`` while unknown, ``"failed"`` if it could not load.
  registry: RegistryPage | null | "failed";
  tab: Tab;
  onTab: (tab: Tab) => void;
  // The project was archived or deleted for good: back to the Board, with what happened.
  onRemoved: (message: string) => void;
  // A project just added: the Agents tab asks the scout straight away.
  askScout?: boolean;
}

// One workspace (ADR-039): everything on this page belongs to it only.
export function WorkspaceView({ detail, registry, tab, onTab, onRemoved, askScout = false }: Props) {
  const { project } = detail;
  const ids = useId();
  return (
    <section aria-labelledby="workspace-title" className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-3">
          <h1 id="workspace-title" className="text-2xl font-semibold tracking-tight text-ink">
            {project.name}
          </h1>
          <HealthPill health={project.health} />
          <span className="ml-auto">
            <RemoveButton slug={project.id} name={project.name} onRemoved={onRemoved} />
          </span>
        </div>
        <p className="text-muted">{project.summary}</p>
        <p className="text-sm text-faint">Everything on this page belongs to {project.name} only.</p>
      </header>

      <div role="tablist" aria-label={`${project.name} sections`} className="flex flex-wrap gap-1 border-b border-line">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            id={`${ids}-${t}`}
            aria-selected={t === tab}
            aria-controls={`${ids}-panel`}
            onClick={() => {
              onTab(t);
            }}
            className={[
              "-mb-px min-h-11 border-b-2 px-4 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
              t === tab ? "border-brand text-brand-ink" : "border-transparent text-muted hover:text-ink",
            ].join(" ")}
          >
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`${ids}-panel`} aria-labelledby={`${ids}-${tab}`}>
        {tab === "overview" && <Overview detail={detail} registry={registry} />}
        {tab === "agents" && <AgentsPanel slug={project.id} name={project.name} askScout={askScout} />}
        {tab === "alerts" && <Alerts cards={detail.cards} />}
        {tab === "sources" && <Sources slug={project.id} registry={registry} />}
      </div>
    </section>
  );
}

function Overview({ detail, registry }: { detail: ProjectDetail; registry: RegistryPage | null | "failed" }) {
  const { project, cards } = detail;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted">
        {project.escalations === 0 ? "Nothing waiting for you" : `${project.escalations} waiting for you`}
      </p>
      {project.sample && (
        <p className="flex items-center gap-2 rounded-xl border border-dashed border-warn bg-warn-soft px-4 py-2.5 text-sm text-ink">
          <FlaskConical className="size-4 shrink-0 text-warn" aria-hidden="true" />
          {cards.every((card) => card.sample)
            ? "Sample data: these numbers are made up to show the layout. Real numbers come next."
            : "Cards marked Sample use made-up numbers. The others are real."}
        </p>
      )}
      {/* Keyed by workspace, so another workspace never shows this one's summary or profile while loading. */}
      <DailySummaryPanel key={`summary-${project.id}`} slug={project.id} />
      <BuiltWith key={project.id} slug={project.id} registry={registry} />
      <div className="grid grid-cols-[repeat(auto-fill,minmax(17rem,1fr))] items-start gap-4">
        {cards.map((card) => (
          <CardView key={card.id} card={card} />
        ))}
      </div>
    </div>
  );
}

function Alerts({ cards }: { cards: readonly Card[] }) {
  const open = cards.flatMap((card) =>
    card.entries.filter((e) => ALERT_TONES.includes(e.tone)).map((e) => ({ card: card.title, ...e })),
  );
  if (open.length === 0) return <p className="text-sm text-muted">No open alerts.</p>;
  return (
    <ul className="flex flex-col divide-y divide-line rounded-2xl border border-line bg-panel">
      {open.map((alert, n) => (
        <li key={`${alert.card}-${String(n)}`} className="flex items-start gap-3 px-5 py-3">
          <span className={`mt-1.5 size-2.5 shrink-0 rounded-full ${TONE_DOT[alert.tone]}`} aria-hidden="true" />
          <span className="text-sm text-ink">
            <span className="font-medium">{alert.card}:</span> {alert.text}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Sources({ slug, registry }: { slug: string; registry: RegistryPage | null | "failed" }) {
  if (registry === "failed") {
    return (
      <p role="alert" className="text-sm text-alert">
        Could not load the sources.
      </p>
    );
  }
  if (registry === null) return <p className="text-sm text-muted">Loading…</p>;
  const used = registry.connectors.flatMap((c) =>
    c.uses.filter((u) => u.project_id === slug).map((u) => ({ id: c.id, name: c.name, status: u.status })),
  );
  if (used.length === 0) return <p className="text-sm text-muted">No sources connected yet.</p>;
  return (
    <ul className="flex flex-col divide-y divide-line rounded-2xl border border-line bg-panel">
      {used.map((source) => (
        <li key={source.id} className="flex items-center justify-between gap-3 px-5 py-3 text-sm">
          <span className="font-medium text-ink">{source.name}</span>
          <span className={source.status === "live" ? "text-muted" : "text-alert"}>{USE_WORDS[source.status]}</span>
        </li>
      ))}
    </ul>
  );
}
