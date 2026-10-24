import { LayoutGrid, SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type ModelChoice, type ProjectDetail, type ProjectSummary, type RegistryPage } from "./api";
import { BoardView } from "./components/BoardView";
import { ChatPanel } from "./components/ChatPanel";
import { CustomizeView } from "./components/CustomizeView";
import { ProjectList } from "./components/ProjectList";
import { type Tab, WorkspaceView } from "./components/WorkspaceView";

type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "failed" };

// What the centre shows: the Board (home), one workspace and its tab, or the Customize page.
type View = { kind: "board" } | { kind: "workspace"; slug: string; tab: Tab } | { kind: "customize" };

const NAV_BUTTON = [
  "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left font-medium transition-colors",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
].join(" ");

export function App() {
  const [workspaces, setWorkspaces] = useState<Load<ProjectSummary[]>>({ state: "loading" });
  const [models, setModels] = useState<ModelChoice[]>([]);
  const [view, setView] = useState<View>({ kind: "board" });
  const [detail, setDetail] = useState<Load<ProjectDetail>>({ state: "loading" });
  const [registry, setRegistry] = useState<Load<RegistryPage>>({ state: "loading" });
  const [notice, setNotice] = useState<string | null>(null);

  const slug = view.kind === "workspace" ? view.slug : null;

  useEffect(() => {
    let live = true;
    api.projects().then(
      (data) => {
        if (live) setWorkspaces({ state: "ready", data });
      },
      () => {
        if (live) setWorkspaces({ state: "failed" });
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
    // The Board's "Needs you", a workspace's Sources and the Customize page all read it.
    api.registry().then(
      (data) => {
        if (live) setRegistry({ state: "ready", data });
      },
      () => {
        if (live) setRegistry({ state: "failed" });
      },
    );
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (slug === null) return;
    let live = true;
    api.project(slug).then(
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
  }, [slug]);

  // Tried again when Customize is opened after a failure.
  function retryRegistry() {
    if (registry.state !== "failed") return;
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

  // Read again after a project is archived, brought back or deleted.
  function reloadWorkspaces() {
    api.projects().then(
      (data) => {
        setWorkspaces({ state: "ready", data });
      },
      () => {
        setWorkspaces({ state: "failed" });
      },
    );
  }

  function open(next: View) {
    setNotice(null);
    if (next.kind === "workspace" && next.slug !== slug) setDetail({ state: "loading" });
    if (next.kind === "customize") retryRegistry();
    setView(next);
  }

  const nameOf = (id: string) =>
    workspaces.state === "ready" ? (workspaces.data.find((w) => w.id === id)?.name ?? id) : id;
  const registryData = registry.state === "ready" ? registry.data : null;

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
        <aside className="flex flex-col gap-3 border-b border-line bg-panel p-3 lg:overflow-y-auto lg:border-r lg:border-b-0">
          <button
            type="button"
            onClick={() => {
              open({ kind: "board" });
            }}
            aria-current={view.kind === "board" ? "page" : undefined}
            className={`${NAV_BUTTON} ${view.kind === "board" ? "bg-brand-soft text-brand-ink" : "text-ink hover:bg-raised"}`}
          >
            <LayoutGrid className="size-4" aria-hidden="true" />
            The Board
          </button>
          {workspaces.state === "loading" && <p className="p-3 text-sm text-muted">Loading workspaces…</p>}
          {workspaces.state === "failed" && (
            <p role="alert" className="p-3 text-sm text-alert">
              Could not load the workspaces. Is the dashboard server running?
            </p>
          )}
          {workspaces.state === "ready" && workspaces.data.length === 0 && (
            <p className="p-3 text-sm text-muted">No workspaces yet.</p>
          )}
          {workspaces.state === "ready" && workspaces.data.length > 0 && (
            <ProjectList
              projects={workspaces.data}
              selected={slug}
              onSelect={(id) => {
                open({ kind: "workspace", slug: id, tab: "overview" });
              }}
            />
          )}
          <button
            type="button"
            onClick={() => {
              open({ kind: "customize" });
            }}
            aria-current={view.kind === "customize" ? "page" : undefined}
            className={`${NAV_BUTTON} mt-auto ${view.kind === "customize" ? "bg-brand-soft text-brand-ink" : "text-ink hover:bg-raised"}`}
          >
            <SlidersHorizontal className="size-4" aria-hidden="true" />
            Customize
          </button>
        </aside>

        <main className="min-w-0 p-5 lg:overflow-y-auto lg:p-8">
          {view.kind === "board" && workspaces.state === "ready" && (
            <BoardView
              workspaces={workspaces.data}
              registry={registryData}
              onOpen={(id) => {
                open({ kind: "workspace", slug: id, tab: "overview" });
              }}
              onCustomize={() => {
                open({ kind: "customize" });
              }}
              notice={notice}
              onChanged={reloadWorkspaces}
              onAdded={(added) => {
                api.projects().then(
                  (data) => {
                    setWorkspaces({ state: "ready", data });
                    open({ kind: "workspace", slug: added, tab: "overview" });
                  },
                  () => {
                    setWorkspaces({ state: "failed" });
                  },
                );
              }}
            />
          )}
          {view.kind === "customize" && (
            <>
              {registry.state === "loading" && <p className="text-muted">Loading…</p>}
              {registry.state === "failed" && (
                <p role="alert" className="text-alert">
                  Could not load the connectors and skills.
                </p>
              )}
              {registry.state === "ready" && (
                <CustomizeView
                  page={registry.data}
                  projectName={nameOf}
                  onChange={(data) => {
                    setRegistry({ state: "ready", data });
                  }}
                />
              )}
            </>
          )}
          {view.kind === "workspace" && (
            <>
              {detail.state === "loading" && <p className="text-muted">Loading…</p>}
              {detail.state === "failed" && (
                <p role="alert" className="text-alert">
                  Could not load this workspace.
                </p>
              )}
              {detail.state === "ready" && (
                <WorkspaceView
                  detail={detail.data}
                  registry={registry.state === "failed" ? "failed" : registryData}
                  tab={view.tab}
                  onTab={(tab) => {
                    open({ ...view, tab });
                  }}
                  onRemoved={(message) => {
                    reloadWorkspaces();
                    open({ kind: "board" });
                    setNotice(message);
                  }}
                />
              )}
            </>
          )}
        </main>

        <aside className="min-h-[28rem] border-t border-line bg-panel lg:min-h-0 lg:border-t-0 lg:border-l">
          {/* A fresh chat per workspace (and one for the Board): one workspace's conversation never shows in another. */}
          <ChatPanel
            key={slug ?? "board"}
            models={models}
            projectId={slug}
            projectName={slug === null ? null : nameOf(slug)}
          />
        </aside>
      </div>
    </div>
  );
}
