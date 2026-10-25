import { useEffect, useState } from "react";

import {
  api,
  type LogFilters,
  type LogLevel,
  type LogLine,
  type LogsPage,
  type ProjectSummary,
} from "../api";

// The Logs page (ADR-034 slice 3): every kept log line says what happened, why, and what to do next.
// Every sentence was written by Yaqzan's own templates; this page only lays them out and filters them.

const FIELD =
  "min-h-11 w-full rounded-lg border border-line bg-panel px-3 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";

const LEVELS: { value: LogLevel; label: string }[] = [
  { value: "info", label: "Everything" },
  { value: "warning", label: "Warnings and errors" },
  { value: "error", label: "Errors only" },
];
const DAYS: { value: number; label: string }[] = [
  { value: 1, label: "Today" },
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
];
const BADGE: Record<LogLevel, string> = {
  info: "bg-raised text-muted",
  warning: "bg-warn-soft text-ink",
  error: "bg-alert-soft text-alert",
};
const BADGE_WORD: Record<LogLevel, string> = {
  info: "Info",
  warning: "Warning",
  error: "Error",
};
const TRACE = /^[0-9a-f]{32}$/;

type Load =
  | { state: "loading" }
  | { state: "ready"; data: LogsPage }
  | { state: "failed" };

// "25 Oct 13:05:09", in the viewer's own time zone.
export function logTime(iso: string | null): string {
  if (iso === null) return "time unknown";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "time unknown";
  const day = at.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
  });
  const clock = at.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  return `${day} ${clock}`;
}

interface Props {
  workspaces: ProjectSummary[];
}

export function LogsView({ workspaces }: Props) {
  const [filters, setFilters] = useState<LogFilters>({
    level: "warning",
    days: 1,
  });
  const [traceText, setTraceText] = useState("");
  const [load, setLoad] = useState<Load>({ state: "loading" });

  useEffect(() => {
    let live = true;
    api.logs(filters).then(
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
  }, [filters]);

  function set(change: Partial<LogFilters>) {
    setLoad({ state: "loading" });
    setFilters((now) => ({ ...now, ...change }));
  }

  function followTrace(trace: string) {
    setTraceText(trace);
    set({ trace, days: 30, level: "info" });
  }

  const traceOk = traceText === "" || TRACE.test(traceText);

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Logs</h1>
        <p className="mt-1 text-sm text-muted">
          Everything Yaqzan did, newest first, in plain English: what happened,
          why, and what to do next. Kept on this laptop for 30 days, with
          secrets and personal details removed.
        </p>
      </div>

      <form
        className="grid grid-cols-1 gap-3 rounded-2xl border border-line bg-panel p-4 sm:grid-cols-2 xl:grid-cols-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (traceOk) set({ trace: traceText === "" ? undefined : traceText });
        }}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-level" className="text-sm font-medium text-ink">
            Show
          </label>
          <select
            id="logs-level"
            value={filters.level}
            onChange={(event) => {
              set({ level: event.target.value as LogLevel });
            }}
            className={FIELD}
          >
            {LEVELS.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-days" className="text-sm font-medium text-ink">
            When
          </label>
          <select
            id="logs-days"
            value={filters.days}
            onChange={(event) => {
              set({ days: Number(event.target.value) });
            }}
            className={FIELD}
          >
            {DAYS.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label
            htmlFor="logs-project"
            className="text-sm font-medium text-ink"
          >
            Project
          </label>
          <select
            id="logs-project"
            value={filters.project ?? ""}
            onChange={(event) => {
              set({
                project:
                  event.target.value === "" ? undefined : event.target.value,
              });
            }}
            className={FIELD}
          >
            <option value="">All projects</option>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-trace" className="text-sm font-medium text-ink">
            Trace ID (from an error)
          </label>
          <div className="flex gap-2">
            <input
              id="logs-trace"
              value={traceText}
              onChange={(event) => {
                setTraceText(event.target.value.trim().toLowerCase());
              }}
              placeholder="32 letters and digits"
              aria-invalid={!traceOk}
              aria-describedby={traceOk ? undefined : "logs-trace-error"}
              className={FIELD}
            />
            <button
              type="submit"
              disabled={!traceOk}
              className="min-h-11 rounded-lg bg-brand px-4 text-sm font-medium text-on-brand disabled:opacity-50"
            >
              Find
            </button>
          </div>
          {!traceOk && (
            <p id="logs-trace-error" className="text-xs text-alert">
              A trace ID is 32 letters (a–f) and digits.
            </p>
          )}
          {filters.trace !== undefined && (
            <button
              type="button"
              onClick={() => {
                setTraceText("");
                set({ trace: undefined });
              }}
              className="self-start text-xs text-brand-ink underline"
            >
              Clear the trace
            </button>
          )}
        </div>
      </form>

      <LogsBody load={load} onTrace={followTrace} />
    </div>
  );
}

function LogsBody({
  load,
  onTrace,
}: {
  load: Load;
  onTrace: (trace: string) => void;
}) {
  if (load.state === "loading")
    return <p className="text-sm text-muted">Loading…</p>;
  if (load.state === "failed") {
    return (
      <p role="alert" className="text-sm text-alert">
        The logs could not be read. Is the dashboard server running?
      </p>
    );
  }
  const { kept, lines, more, cut } = load.data;
  if (!kept) {
    return (
      <p className="text-sm text-muted">
        Logs are not kept yet. Start the dashboard with{" "}
        <code>--log-dir ~/AppData/Local/Yaqzan/logs</code> to keep them.
      </p>
    );
  }
  if (lines.length === 0)
    return <p className="text-sm text-muted">Nothing matches these filters.</p>;
  return (
    <div className="flex flex-col gap-3">
      {(more || cut) && (
        <p className="text-sm text-muted">
          {more &&
            `Showing the newest ${String(lines.length)} lines; narrow the filters to see older ones.`}
          {more && cut && " "}
          {cut &&
            "One day had so many lines that only its newest part was searched."}
        </p>
      )}
      <ol className="flex flex-col gap-2">
        {lines.map((line, index) => (
          <LogItem
            key={`${line.timestamp ?? ""}-${String(index)}`}
            line={line}
            onTrace={onTrace}
          />
        ))}
      </ol>
    </div>
  );
}

function LogItem({
  line,
  onTrace,
}: {
  line: LogLine;
  onTrace: (trace: string) => void;
}) {
  const values = Object.entries(line.fields);
  return (
    <li className="rounded-xl border border-line bg-panel p-4">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span
          className={`rounded-full px-2 py-0.5 font-medium ${BADGE[line.level]}`}
        >
          {BADGE_WORD[line.level]}
        </span>
        <time dateTime={line.timestamp ?? undefined} className="text-faint">
          {logTime(line.timestamp)}
        </time>
        <code className="text-faint">{line.event}</code>
        {line.component !== null && (
          <span className="text-faint">· {line.component}</span>
        )}
      </div>
      <p className="mt-2 text-sm font-medium text-ink">
        {line.what ?? line.event}
      </p>
      {line.why !== null && (
        <p className="mt-1 text-sm text-muted">Why: {line.why}</p>
      )}
      {line.next !== null && (
        <p className="mt-1 text-sm text-muted">Next: {line.next}</p>
      )}
      {(values.length > 0 || line.trace_id !== null) && (
        <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
          {values.map(([key, value]) => (
            <span
              key={key}
              className="rounded-md bg-raised px-1.5 py-0.5 text-muted"
            >
              {key}: {String(value)}
            </span>
          ))}
          {line.trace_id !== null && (
            <button
              type="button"
              onClick={() => {
                if (line.trace_id !== null) onTrace(line.trace_id);
              }}
              className="rounded-md bg-brand-soft px-1.5 py-0.5 text-brand-ink underline"
              aria-label={`Show every line of trace ${line.trace_id}`}
            >
              trace {line.trace_id.slice(0, 8)}…
            </button>
          )}
        </div>
      )}
    </li>
  );
}
