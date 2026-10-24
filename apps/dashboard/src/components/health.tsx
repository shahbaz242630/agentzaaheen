import type { Health } from "../api";

// A workspace's overall colour and its words, used on the list, the Board and the workspace page.
// Pills keep their text in ink (the status colours are too light for small text on their soft grounds).
export const HEALTH: Record<Health, { dot: string; soft: string; label: string }> = {
  ok: { dot: "bg-ok", soft: "bg-ok-soft", label: "All good" },
  warn: { dot: "bg-warn", soft: "bg-warn-soft", label: "Needs a look" },
  alert: { dot: "bg-alert", soft: "bg-alert-soft", label: "Urgent" },
  unknown: { dot: "bg-unknown", soft: "bg-unknown-soft", label: "Unknown" },
};

export function HealthPill({ health }: { health: Health }) {
  const look = HEALTH[health];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold text-ink ${look.soft}`}>
      <span className={`size-2 rounded-full ${look.dot}`} aria-hidden="true" />
      {look.label}
    </span>
  );
}
