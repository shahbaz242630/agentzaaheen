import { FlaskConical } from "lucide-react";

import type { ProjectDetail } from "../api";
import { CardView } from "./CardView";

interface Props {
  detail: ProjectDetail;
}

export function ProjectView({ detail }: Props) {
  const { project, cards } = detail;
  const escalations = project.escalations;
  return (
    <section aria-labelledby="project-title" className="flex flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 id="project-title" className="text-2xl font-semibold tracking-tight text-ink">
            {project.name}
          </h1>
          <p className="mt-1 text-muted">{project.summary}</p>
        </div>
        <p className="text-sm text-muted">
          {escalations === 0 ? "Nothing waiting for you" : `${escalations} waiting for you`}
        </p>
      </header>

      {project.sample && (
        <p className="flex items-center gap-2 rounded-xl border border-dashed border-warn bg-warn-soft px-4 py-2.5 text-sm text-ink">
          <FlaskConical className="size-4 shrink-0 text-warn" aria-hidden="true" />
          Sample data: these numbers are made up to show the layout. Real numbers come next.
        </p>
      )}

      <div className="grid grid-cols-[repeat(auto-fill,minmax(17rem,1fr))] items-start gap-4">
        {cards.map((card) => (
          <CardView key={card.id} card={card} />
        ))}
      </div>
    </section>
  );
}
