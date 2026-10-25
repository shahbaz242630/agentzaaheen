import { useCallback, useEffect, useState } from "react";

import {
  api,
  type BoardSummaries,
  type Briefing,
  type DailySummary,
  type SummaryLine,
  type WorkspaceSummaries,
} from "../api";
import { ago, capWords, explainUnknown, formatValue, money } from "../format";

// Daily summaries and the Board's briefings (F17 slice 6). Every word of a summary was written by code
// from what Yaqzan holds; this page only lays it out. What could not be read always comes first.

const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const REASON: Record<string, string> = {
  "store.unavailable": "The laptop database did not answer",
  "store.failed": "The laptop database refused it",
  "source.auth": "The service refused our key; it may have expired",
  "source.refused": "The service refused the request",
  "source.unavailable": "The service did not answer",
  "source.rate_limited": "The service asked us to slow down",
  "scout.unreadable": "The scout's saved advice could not be read",
  "net.timeout": "ntfy did not answer in time",
  "net.unavailable": "ntfy could not be reached",
  "net.client_error": "ntfy refused the message",
};

export function reasonWords(code: string): string {
  return REASON[code] ?? explainUnknown(code);
}

// "Sun 25 Oct", from the API's ISO date (the Chairman's own date, made by the server).
export function dayWords(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  const at = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 1, date ?? 1));
  return `${WEEKDAY[at.getUTCDay()] ?? ""} ${String(at.getUTCDate())} ${MONTH[at.getUTCMonth()] ?? ""}`;
}

// The Board card's line: honest when there is no database, none yet, or it could not be read.
export function boardLine(found: BoardSummaries | "failed" | undefined, slug: string): string {
  if (found === undefined) return "Daily summary: …";
  if (found === "failed") return "Daily summary: could not be read";
  if (!found.connected) return "Daily summary: not set up yet";
  const line: SummaryLine | undefined = found.workspaces[slug];
  if (line === undefined) return "Daily summary: none yet (made at 05:00)";
  return `${dayWords(line.day)}: ${line.line ?? "the saved summary could not be read"}`;
}

type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "failed" };

// One workspace's newest summary in full, and the days before it in one line each.
export function DailySummaryPanel({ slug }: { slug: string }) {
  const [load, setLoad] = useState<Load<WorkspaceSummaries>>({ state: "loading" });
  useEffect(() => {
    let live = true;
    api.workspaceSummaries(slug).then(
      (data) => {
        if (live) setLoad({ state: "ready", data });
      },
      () => {
        if (live) setLoad({ state: "failed" });
      },
    );
    return () => {
      live = false;
    };
  }, [slug]);
  return (
    <section aria-labelledby={`summary-${slug}`} className="rounded-2xl border border-line bg-panel p-5">
      <h2 id={`summary-${slug}`} className="font-semibold text-ink">
        Daily summary
      </h2>
      <SummaryBody load={load} />
    </section>
  );
}

function SummaryBody({ load }: { load: Load<WorkspaceSummaries> }) {
  if (load.state === "loading") return <p className="mt-2 text-sm text-muted">Loading…</p>;
  if (load.state === "failed") {
    return (
      <p role="alert" className="mt-2 text-sm text-alert">
        The daily summary could not be loaded.
      </p>
    );
  }
  const { connected, days } = load.data;
  if (!connected) {
    return <p className="mt-2 text-sm text-muted">Not set up yet: daily summaries need the laptop database.</p>;
  }
  const [newest, ...older] = days;
  if (newest === undefined) {
    return <p className="mt-2 text-sm text-muted">None yet. The first one is made at 05:00.</p>;
  }
  return (
    <div className="mt-2 flex flex-col gap-4">
      <p className="text-sm text-faint">
        {dayWords(newest.day)} · made {ago(newest.made_at)} · written by code from what Yaqzan holds
      </p>
      {newest.summary === null ? (
        <p role="alert" className="text-sm text-alert">
          This day's saved summary could not be read, so nothing from it is shown.
        </p>
      ) : (
        <SummaryFacts summary={newest.summary} line={newest.line} />
      )}
      {older.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-ink">Earlier days</h3>
          <ul className="mt-1 flex flex-col gap-1 text-sm text-muted">
            {older.map((d) => (
              <li key={d.day}>
                <span className="font-medium text-ink">{dayWords(d.day)}:</span>{" "}
                {d.line ?? "the saved summary could not be read"}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SummaryFacts({ summary, line }: { summary: DailySummary; line: string | null }) {
  const { unread, sources, changes, alerts, agents, scout } = summary;
  return (
    <div className="flex flex-col gap-3 text-sm">
      {line !== null && <p className="text-base text-ink">{line}</p>}
      {unread.length > 0 && (
        <div className="rounded-xl border border-warn bg-warn-soft px-4 py-3">
          <h3 className="font-semibold text-ink">Could not be read</h3>
          <ul className="mt-1 flex flex-col gap-0.5 text-ink">
            {unread.map((u) => (
              <li key={u.title}>
                {u.title}: {reasonWords(u.reason)}.
              </li>
            ))}
          </ul>
        </div>
      )}
      {sources.length > 0 && (
        <ul className="flex flex-col gap-2">
          {sources.map((s) => (
            <li key={s.card}>
              <span className="font-medium text-ink">{s.title}</span>
              {s.metrics.length > 0 && (
                <span className="text-muted">
                  {" "}
                  · {s.metrics.map((m) => `${m.label} ${formatValue(m.value)}`).join(" · ")}
                </span>
              )}
              {s.flagged.map((e, n) => (
                <span key={`${s.card}-${String(n)}`} className={`block ${e.tone === "alert" ? "text-alert" : "text-ink"}`}>
                  {e.text}
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}
      {changes.length > 0 && (
        <div>
          <h3 className="font-semibold text-ink">Changed since the day before</h3>
          <ul className="mt-1 flex flex-col gap-0.5 text-muted">
            {changes.map((c) => (
              <li key={`${c.title}-${c.label}`}>
                {c.title} {c.label}: {formatValue(c.before)} → {formatValue(c.after)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {alerts !== null && (
        <p className="text-muted">
          Phone alerts: {String(alerts.opened)} went down, {String(alerts.closed)} came back up
          {Date.parse(alerts.counted_since) > Date.parse(summary.window_start)
            ? " (counted only since the dashboard started)"
            : ""}.
        </p>
      )}
      {agents?.agents === 0 && <p className="text-muted">Agents: none yet</p>}
      {agents !== null && agents.agents > 0 && (
        <p className="text-muted">
          Agents: {String(agents.on)} of {String(agents.agents)} on · spent {money(agents.spent_minor)} · cap{" "}
          {capWords(agents.cap_minor)}
        </p>
      )}
      {scout !== null && <p className="text-muted">{scoutWords(scout)}</p>}
      {summary.sample_left_out.length > 0 && (
        <p className="text-faint">Left out (sample data, not real): {summary.sample_left_out.join(", ")}.</p>
      )}
    </div>
  );
}

function scoutWords(scout: NonNullable<DailySummary["scout"]>): string {
  if (scout.state === "running") return "The scout is still reading this project.";
  if (scout.state === "failed") return `The scout's last run did not finish: ${reasonWords(scout.reason ?? "")}.`;
  const count = scout.recommended.length;
  return count === 0
    ? "The scout recommends no agents yet."
    : `The scout recommends ${String(count)} ${count === 1 ? "role" : "roles"}: ${scout.recommended.join(", ")}.`;
}

function briefingState(b: Briefing): { words: string; tone: string } {
  if (b.state === "sent") return { words: `Sent to your phone ${ago(b.sent_at ?? b.made_at)}`, tone: "text-muted" };
  if (b.state === "waiting") return { words: "Not sent yet", tone: "text-muted" };
  if (b.reason === "briefing.no_topic") {
    return { words: "Kept here only: no phone (ntfy) topic is set up", tone: "text-muted" };
  }
  return {
    words: `Not sent: ${reasonWords(b.reason ?? "")}. Yaqzan tries again every 15 minutes today.`,
    tone: "text-alert",
  };
}

// The Board's morning briefings (last 7 days), the exact words sent, and "make today's summaries now".
export function BriefingsPanel({ onMade }: { onMade: () => void }) {
  const [load, setLoad] = useState<Load<Briefing[]> | { state: "off" }>({ state: "loading" });
  const [making, setMaking] = useState("idle") // "idle", "busy", or what happened;
  const read = useCallback(() => {
    let live = true;
    api.briefings().then(
      (data) => {
        if (live) setLoad(data.connected ? { state: "ready", data: data.briefings } : { state: "off" });
      },
      () => {
        if (live) setLoad({ state: "failed" });
      },
    );
    return () => {
      live = false;
    };
  }, []);
  useEffect(read, [read]);
  const makeNow = () => {
    setMaking("busy");
    api.makeSummaries().then(
      ({ made, failed }) => {
        setMaking(
          failed === 0
            ? `Made ${String(made)} summaries for today.`
            : `Made ${String(made)} summaries; ${String(failed)} could not be made.`,
        );
        onMade();
      },
      () => {
        setMaking("Today's summaries could not be made. Is the laptop database running?");
      },
    );
  };
  if (load.state === "off") return null; // no database: the cards already say "not set up yet"
  return (
    <section aria-labelledby="briefings" className="rounded-2xl border border-line bg-panel p-5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="briefings" className="font-semibold text-ink">
          Morning briefings
        </h2>
        <button
          type="button"
          onClick={makeNow}
          disabled={making === "busy"}
          className="ml-auto min-h-11 rounded-lg border border-line bg-panel px-4 text-sm font-medium text-ink hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
        >
          {making === "busy" ? "Making…" : "Make today's summaries now"}
        </button>
      </div>
      <p className="mt-1 text-sm text-muted">
        Summaries are made at 05:00 and the briefing goes to your phone at 07:00. Making them now does not send
        anything.
      </p>
      {making !== "idle" && making !== "busy" && (
        <p role="status" className="mt-2 text-sm text-ink">
          {making}
        </p>
      )}
      {load.state === "loading" && <p className="mt-2 text-sm text-muted">Loading…</p>}
      {load.state === "failed" && (
        <p role="alert" className="mt-2 text-sm text-alert">
          The briefings could not be loaded.
        </p>
      )}
      {load.state === "ready" &&
        (load.data.length === 0 ? (
          <p className="mt-2 text-sm text-muted">No briefing yet. The first one goes at 07:00.</p>
        ) : (
          <ul className="mt-3 flex flex-col divide-y divide-line">
            {load.data.map((b) => {
              const state = briefingState(b);
              return (
                <li key={b.day} className="flex flex-col gap-1 py-3">
                  <span className="flex flex-wrap items-baseline gap-x-3">
                    <span className="font-medium text-ink">{dayWords(b.day)}</span>
                    <span className={`text-sm ${state.tone}`}>{state.words}</span>
                  </span>
                  <p className="whitespace-pre-line text-sm text-muted">{b.message}</p>
                </li>
              );
            })}
          </ul>
        ))}
    </section>
  );
}
