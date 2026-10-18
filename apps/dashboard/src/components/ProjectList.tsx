import type { Health, ProjectSummary } from "../api";

const HEALTH: Record<Health, { dot: string; label: string }> = {
  ok: { dot: "bg-ok", label: "All good" },
  warn: { dot: "bg-warn", label: "Needs a look" },
  alert: { dot: "bg-alert", label: "Urgent" },
  unknown: { dot: "bg-unknown", label: "Unknown" },
};

interface Props {
  projects: readonly ProjectSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
}

export function ProjectList({ projects, selected, onSelect }: Props) {
  return (
    <nav aria-label="Projects" className="flex flex-col gap-1">
      <h2 className="px-3 pb-2 text-xs font-semibold tracking-wider text-faint uppercase">Projects</h2>
      <ul className="flex flex-col gap-1">
        {projects.map((project) => {
          const health = HEALTH[project.health];
          const active = project.id === selected;
          return (
            <li key={project.id}>
              <button
                type="button"
                onClick={() => {
                  onSelect(project.id);
                }}
                aria-current={active ? "page" : undefined}
                className={[
                  "group flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition-colors",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
                  active ? "bg-brand-soft" : "hover:bg-raised",
                ].join(" ")}
              >
                <span
                  className={`mt-1.5 size-2.5 shrink-0 rounded-full ${health.dot}`}
                  title={health.label}
                  aria-label={health.label}
                  role="img"
                />
                <span className="min-w-0 flex-1">
                  <span className={`block font-medium ${active ? "text-brand-ink" : "text-ink"}`}>
                    {project.name}
                  </span>
                  <span className="line-clamp-2 block text-sm text-muted">{project.summary}</span>
                </span>
                {project.escalations > 0 && (
                  <span
                    className="mt-0.5 rounded-full bg-alert-soft px-2 py-0.5 text-xs font-semibold text-alert tabular"
                    title={`${project.escalations} escalated to you`}
                  >
                    {project.escalations}
                    <span className="sr-only"> escalated to you</span>
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
