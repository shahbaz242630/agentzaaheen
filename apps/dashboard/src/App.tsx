import { SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type ModelChoice, type ProjectDetail, type ProjectSummary, type RegistryPage } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { CustomizeView } from "./components/CustomizeView";
import { ProjectList } from "./components/ProjectList";
import { ProjectView } from "./components/ProjectView";

type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "failed" };

export function App() {
  const [projects, setProjects] = useState<Load<ProjectSummary[]>>({ state: "loading" });
  const [models, setModels] = useState<ModelChoice[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Load<ProjectDetail>>({ state: "loading" });
  const [customizing, setCustomizing] = useState(false);
  const [registry, setRegistry] = useState<Load<RegistryPage> | null>(null);

  useEffect(() => {
    let live = true;
    api.projects().then(
      (data) => {
        if (!live) return;
        setProjects({ state: "ready", data });
        setSelected((current) => current ?? data[0]?.id ?? null);
      },
      () => {
        if (live) setProjects({ state: "failed" });
      },
    );
    api.models().then(
      (data) => {
        if (live) setModels(data);
      },
      () => {
        if (live) setModels([]);
      },
    );
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (selected === null) return;
    let live = true;
    api.project(selected).then(
      (data) => {
        if (live) setDetail({ state: "ready", data });
      },
      () => {
        if (live) setDetail({ state: "failed" });
      },
    );
    return () => {
      live = false;
    };
  }, [selected]);

  // Read once when first opened, retried after a failure; each switch answers with the page as it now stands.
  function customize() {
    setCustomizing(true);
    if (registry?.state === "ready" || registry?.state === "loading") return;
    setRegistry({ state: "loading" });
    api.registry().then(
      (data) => {
        setRegistry({ state: "ready", data });
      },
      () => {
        setRegistry({ state: "failed" });
      },
    );
  }

  function select(id: string) {
    setCustomizing(false);
    if (id === selected) return;
    setDetail({ state: "loading" });
    setSelected(id);
  }

  const projectNameOf = (id: string) =>
    projects.state === "ready" ? (projects.data.find((p) => p.id === id)?.name ?? id) : id;

  const projectName =
    projects.state === "ready" ? (projects.data.find((p) => p.id === selected)?.name ?? null) : null;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-line bg-panel px-5 py-3">
        <div className="flex items-center gap-2.5">
          <img src="/favicon.svg" alt="" className="size-7" />
          <span className="text-lg font-semibold tracking-tight text-ink">Yaqzan</span>
          <span lang="ar" dir="rtl" className="text-lg text-muted">
            يقظان
          </span>
        </div>
        <span className="hidden text-xs text-faint sm:inline">Read-only · runs on this laptop</span>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[16rem_minmax(0,1fr)_24rem]">
        <aside className="border-b border-line bg-panel p-3 lg:overflow-y-auto lg:border-r lg:border-b-0">
          <button
            type="button"
            onClick={customize}
            aria-current={customizing ? "page" : undefined}
            className={[
              "mb-3 flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left font-medium transition-colors",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
              customizing ? "bg-brand-soft text-brand-ink" : "text-ink hover:bg-raised",
            ].join(" ")}
          >
            <SlidersHorizontal className="size-4" aria-hidden="true" />
            Customize
          </button>
          {projects.state === "loading" && <p className="p-3 text-sm text-muted">Loading projects…</p>}
          {projects.state === "failed" && (
            <p role="alert" className="p-3 text-sm text-alert">
              Could not load the projects. Is the dashboard server running?
            </p>
          )}
          {projects.state === "ready" && projects.data.length === 0 && (
            <p className="p-3 text-sm text-muted">No projects yet.</p>
          )}
          {projects.state === "ready" && projects.data.length > 0 && (
            <ProjectList projects={projects.data} selected={customizing ? null : selected} onSelect={select} />
          )}
        </aside>

        <main className="min-w-0 p-5 lg:overflow-y-auto lg:p-8">
          {customizing ? (
            <>
              {registry?.state === "loading" && <p className="text-muted">Loading…</p>}
              {registry?.state === "failed" && (
                <p role="alert" className="text-alert">
                  Could not load the connectors and skills.
                </p>
              )}
              {registry?.state === "ready" && (
                <CustomizeView
                  page={registry.data}
                  projectName={projectNameOf}
                  onChange={(data) => {
                    setRegistry({ state: "ready", data });
                  }}
                />
              )}
            </>
          ) : (
            <>
              {selected !== null && detail.state === "loading" && <p className="text-muted">Loading…</p>}
              {detail.state === "failed" && (
                <p role="alert" className="text-alert">
                  Could not load this project.
                </p>
              )}
              {detail.state === "ready" && <ProjectView detail={detail.data} />}
            </>
          )}
        </main>

        <aside className="min-h-[28rem] border-t border-line bg-panel lg:min-h-0 lg:border-t-0 lg:border-l">
          <ChatPanel models={models} projectId={selected} projectName={projectName} />
        </aside>
      </div>
    </div>
  );
}
