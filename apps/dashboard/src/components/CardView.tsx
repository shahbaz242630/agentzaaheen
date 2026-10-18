import type { Card, Tone } from "../api";
import { ago, explainUnknown, formatValue, freshnessLabel } from "../format";

const TONE_DOT: Record<Tone, string> = {
  ok: "bg-ok",
  info: "bg-info",
  warn: "bg-warn",
  alert: "bg-alert",
};

interface Props {
  card: Card;
  now?: Date;
}

export function CardView({ card, now }: Props) {
  const fresh = card.freshness === "fresh";
  return (
    <article
      aria-labelledby={`card-${card.id}`}
      className="flex flex-col gap-4 rounded-2xl border border-line bg-panel p-5 shadow-[0_1px_2px_oklch(0_0_0/0.04)]"
    >
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id={`card-${card.id}`} className="font-semibold text-ink">
            {card.title}
          </h3>
          <p className={`mt-0.5 text-xs ${fresh ? "text-muted" : "text-faint"}`}>
            <span className={`mr-1.5 inline-block size-1.5 rounded-full align-middle ${fresh ? "bg-ok" : "bg-unknown"}`} />
            {freshnessLabel(card.freshness, card.last_success_at, now)}
          </p>
        </div>
        <span className="flex shrink-0 flex-col items-end gap-1">
          <span className="rounded-md bg-raised px-2 py-1 text-xs font-medium text-muted">{card.source}</span>
          {card.sample && (
            <span className="rounded-md border border-dashed border-warn px-1.5 py-0.5 text-[0.7rem] font-medium text-warn">
              Sample
            </span>
          )}
        </span>
      </header>

      {card.metrics.length > 0 && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
          {card.metrics.map((metric, index) => {
            const unknown = metric.value.kind === "unknown";
            return (
              <div key={`${index}-${metric.label}`} className="min-w-0">
                <dt className="truncate text-xs text-muted">{metric.label}</dt>
                <dd
                  className={`mt-0.5 text-2xl font-semibold tracking-tight tabular ${unknown ? "text-unknown" : "text-ink"}`}
                  title={metric.value.kind === "unknown" ? explainUnknown(metric.value.reason) : undefined}
                >
                  {formatValue(metric.value)}
                </dd>
                {metric.detail !== null && <dd className="truncate text-xs text-faint">{metric.detail}</dd>}
              </div>
            );
          })}
        </dl>
      )}

      {card.entries.length > 0 && (
        <ul className="flex flex-col gap-2 border-t border-line pt-3">
          {card.entries.map((entry, index) => (
            <li key={index} className="flex items-start gap-2.5 text-sm">
              <span className={`mt-1.5 size-1.5 shrink-0 rounded-full ${TONE_DOT[entry.tone]}`} aria-hidden="true" />
              <span className="min-w-0 flex-1 text-ink">{entry.text}</span>
              {entry.at !== null && <span className="shrink-0 text-xs text-faint tabular">{ago(entry.at, now)}</span>}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
