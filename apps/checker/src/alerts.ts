// Which projects to report to Healthchecks.io this round, in the fixed words of core/notify (ADR-031).
//
// Each project has its own Healthchecks check (its slug is the project id). A "fail" report makes
// Healthchecks alert the Chairman (phone via ntfy, and email) when the project goes from up to down;
// a success report alerts once when it is all up again. Healthchecks itself never repeats an alert,
// so reporting the same state again is harmless: that is how the hourly report keeps each check fresh.
// Messages hold counts, our own reason words and the parts' own short names: never an address.

import type { Checked } from "./probe";
import { REASON_WORDS } from "./probe";

export interface Change {
  readonly id: string;
  readonly project: string;
  readonly up: boolean;
}

export interface Report {
  readonly project: string;
  readonly down: boolean;
  readonly message: string;
}

export interface Seen extends Checked {
  readonly id: string;
}

// ``stored`` is each address's last saved state (true = up), by id.
export function changes(seen: readonly Seen[], stored: ReadonlyMap<string, boolean>): Change[] {
  const out: Change[] = [];
  for (const s of seen) {
    const before = stored.get(s.id);
    if (before !== s.up) out.push({ id: s.id, project: s.target.project, up: s.up });
  }
  return out;
}

// One report per project at most: when something changed, or every project when ``hourly``.
export function reports(
  seen: readonly Seen[],
  changed: readonly Change[],
  stored: ReadonlyMap<string, boolean>,
  hourly: boolean,
): Report[] {
  const byProject = new Map<string, Seen[]>();
  for (const s of seen) byProject.set(s.target.project, [...(byProject.get(s.target.project) ?? []), s]);
  const changedIds = new Set(changed.map((c) => c.id));
  const out: Report[] = [];
  for (const [project, sites] of [...byProject.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const up = sites.filter((s) => s.up).length;
    const down = sites.length - up;
    const newlyDown = sites.filter((s) => !s.up && changedIds.has(s.id));
    // Back up only if it had been saved as down: a first sighting that is up is not news.
    const backUp = sites.filter((s) => s.up && changedIds.has(s.id) && stored.get(s.id) === false);
    if (newlyDown.length > 0) {
      const why = [...new Set(newlyDown.map((s) => REASON_WORDS[s.reason]))].sort().join(", ");
      const counted = `${down} of ${plural(sites.length, "address")} down`;
      const parts = partNames(newlyDown);
      const what = parts ? `${parts} down (${why}); ${counted}` : `${counted} (${why})`;
      out.push({ project, down: true, message: `${what}. Details on the dashboard.` });
    } else if (backUp.length > 0) {
      const which = partNames(backUp) || plural(backUp.length, "address");
      const message = `${which} answering again; ${up} of ${sites.length} up now.`;
      out.push({ project, down: down > 0, message });
    } else if (hourly) {
      out.push({ project, down: down > 0, message: `${up} of ${plural(sites.length, "address")} up.` });
    }
  }
  return out;
}

// The parts' names joined, or "" when any address has no part (then counts are used instead).
function partNames(sites: readonly Seen[]): string {
  if (sites.length === 0 || sites.some((s) => s.target.part === null)) return "";
  return [...new Set(sites.map((s) => s.target.part as string))].sort().join(", ");
}

function plural(count: number, word: string): string {
  return count === 1 ? `${count} ${word}` : `${count} ${word}es`;
}
