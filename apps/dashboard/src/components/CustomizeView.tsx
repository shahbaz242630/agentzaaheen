import { Compass, Search, TriangleAlert } from "lucide-react";
import { useState } from "react";

import type { Kind, RegistryPage } from "../api";
import { ConnectorCard, PluginCard, SkillCard, skillTitle } from "./RegistryCards";

type Shelf = "yours" | "discover";

const TABS: { kind: Kind; label: string }[] = [
  { kind: "connector", label: "Connectors" },
  { kind: "skill", label: "Skills" },
  { kind: "plugin", label: "Plugins" },
];

const EMPTY: Record<Kind, string> = {
  connector: "No connectors yet.",
  skill: "No skills yet.",
  plugin: "No plugins yet. Start the dashboard with --bundles DIR to show plugin bundles from a folder.",
};

interface Props {
  page: RegistryPage;
  projectName: (id: string) => string;
}

function matches(query: string, ...texts: string[]): boolean {
  const wanted = query.trim().toLowerCase();
  return wanted === "" || texts.some((text) => text.toLowerCase().includes(wanted));
}

export function CustomizeView({ page, projectName }: Props) {
  const [tab, setTab] = useState<Kind>("connector");
  const [shelf, setShelf] = useState<Shelf>("yours");
  const [query, setQuery] = useState("");

  const connectorName = (id: string) => page.connectors.find((c) => c.id === id)?.name ?? id;
  const connectors = page.connectors.filter((c) => matches(query, c.name, c.category, c.description));
  const skills = page.skills.filter((s) => matches(query, s.id, skillTitle(s.id), s.description));
  const plugins = page.plugins.filter((p) => matches(query, p.id, p.description));
  const counts: Record<Kind, number> = {
    connector: page.connectors.length,
    skill: page.skills.length,
    plugin: page.plugins.length,
  };
  const shown = { connector: connectors.length, skill: skills.length, plugin: plugins.length }[tab];
  const problems = page.problems.filter((p) => p.kind === tab);

  return (
    <section aria-labelledby="customize-title" className="flex flex-col gap-6">
      <header>
        <h1 id="customize-title" className="text-2xl font-semibold tracking-tight text-ink">
          Customize
        </h1>
        <p className="mt-1 text-muted">
          The connectors, skills and plugins Yaqzan can use. Read-only for now: adding or switching one on will
          always come to you for approval first.
        </p>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="tablist" aria-label="Kind" className="flex gap-1 rounded-xl bg-raised p-1">
          {TABS.map(({ kind, label }) => (
            <button
              key={kind}
              type="button"
              role="tab"
              aria-selected={tab === kind}
              onClick={() => {
                setTab(kind);
              }}
              className={[
                "rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
                tab === kind ? "bg-panel text-ink shadow-sm" : "text-muted hover:text-ink",
              ].join(" ")}
            >
              {label} <span className="text-faint tabular">{counts[kind]}</span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <div role="group" aria-label="Show" className="flex gap-1 rounded-xl bg-raised p-1">
            {(["yours", "discover"] as const).map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={shelf === value}
                onClick={() => {
                  setShelf(value);
                }}
                className={[
                  "rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
                  shelf === value ? "bg-panel text-ink shadow-sm" : "text-muted hover:text-ink",
                ].join(" ")}
              >
                {value === "yours" ? "Yours" : "Discover"}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 rounded-xl border border-line bg-panel px-3 py-1.5 focus-within:outline-2 focus-within:outline-brand">
            <Search className="size-4 text-faint" aria-hidden="true" />
            <span className="sr-only">Search</span>
            <input
              type="search"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
              }}
              placeholder="Search"
              className="w-36 bg-transparent text-sm text-ink outline-none placeholder:text-faint"
            />
          </label>
        </div>
      </div>

      {shelf === "discover" ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-dashed border-line px-6 py-12 text-center">
          <Compass className="size-8 text-faint" aria-hidden="true" />
          <p className="font-medium text-ink">Discover is coming soon</p>
          <p className="max-w-md text-sm text-muted">
            A list of connectors, skills and plugins you can add will appear here. Adding one will come to you as a
            proposal to approve; nothing switches on by itself.
          </p>
        </div>
      ) : (
        <>
          {problems.length > 0 && (
            <div className="rounded-xl border border-alert bg-alert-soft px-4 py-3 text-sm text-ink">
              <p className="flex items-center gap-2 font-medium">
                <TriangleAlert className="size-4 text-alert" aria-hidden="true" />
                {problems.length === 1 ? "1 item could not be loaded" : `${problems.length} items could not be loaded`}
              </p>
              <ul className="mt-1.5 flex flex-col gap-1 text-muted">
                {problems.map((problem) => (
                  <li key={problem.path}>
                    <span className="font-mono text-xs text-ink">{problem.path}</span>: {problem.message}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {shown === 0 ? (
            <p className="text-muted">{query.trim() ? `Nothing matches "${query.trim()}".` : EMPTY[tab]}</p>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(18rem,1fr))] items-start gap-4">
              {tab === "connector" &&
                connectors.map((item) => <ConnectorCard key={item.id} item={item} projectName={projectName} />)}
              {tab === "skill" &&
                skills.map((item) => <SkillCard key={item.id} item={item} connectorName={connectorName} />)}
              {tab === "plugin" && plugins.map((item) => <PluginCard key={item.id} item={item} />)}
            </div>
          )}
        </>
      )}
    </section>
  );
}
