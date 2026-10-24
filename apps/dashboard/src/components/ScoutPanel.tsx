import { Check, Plus, Search, Telescope } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { ApiError, api, type RoleAdvice, type RoleChoice, type ScoutReport, type ScoutView } from "../api";
import { ago, centsFrom } from "../format";

// How often the page asks how a running scout is doing. A run takes about 5 minutes on the server's CPU.
export const POLL_MS = 5000;
const LIBRARY_SHOWN = 8;

const STAGE: Record<string, string> = {
  reading: "Reading the repository (read-only)…",
  thinking: "The local model is thinking. This takes about 5 minutes on the server.",
  checking: "Code is checking the model's answer…",
};

// Why a run failed, or why asking was refused, in words.
const REASON: Record<string, string> = {
  "scout.busy": "The scout is already looking at this project.",
  "scout.no_repo": "This project has no GitHub repository, so the scout has nothing to read.",
  "scout.not_allowed": "That is not allowed from here.",
  "scout.not_connected": "The scout needs the laptop database, the GitHub token and the local model on this run.",
  "scout.store_unavailable": "The laptop database did not answer. Is it running?",
  "scout.not_json": "The model's answer was not in the required shape, so code refused it. Nothing was guessed.",
  "scout.bad_answer": "The model's answer did not pass the checks, so code refused it. Nothing was guessed.",
  "scout.crashed": "The scout stopped on an error it did not expect. The dashboard log says where.",
  "scout.interrupted": "The dashboard stopped while the scout was working, so that run was closed.",
  "scout.unreadable": "The saved advice no longer reads as advice (it was saved by an older version).",
  "source.unavailable": "GitHub or the model did not answer in time. Please try again in a few minutes.",
  "source.auth": "GitHub refused the read-only token. It may have expired.",
  "source.refused": "GitHub refused to show that repository to the token.",
  "source.rate_limited": "GitHub asked us to slow down. Please try again in an hour.",
  "source.bad_response": "GitHub or the model answered with something we could not read.",
  "source.too_much_data": "The repository is too big to read in one go.",
};

function reasonWords(code: string | null): string {
  return (code && REASON[code]) ?? "The scout could not finish. Please try again.";
}

type Step = "connect" | "review" | "choose" | "done";
const STEPS: readonly [Step, string][] = [
  ["connect", "Connect"],
  ["review", "Scout review"],
  ["choose", "Choose agents"],
  ["done", "Done"],
];

const BUTTON = [
  "inline-flex min-h-11 items-center gap-2 rounded-lg border border-line bg-panel px-4 text-sm font-medium text-ink",
  "hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50",
].join(" ");
const PRIMARY = [
  "inline-flex min-h-11 items-center gap-2 rounded-lg bg-brand px-5 text-sm font-semibold text-on-brand",
  "hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50",
].join(" ");

type Load = { state: "loading" } | { state: "ready"; view: ScoutView } | { state: "failed" };

interface Props {
  slug: string;
  name: string;
  // Every role in the library, and the roles this workspace already has.
  roles: readonly RoleChoice[];
  given: ReadonlySet<string>;
  busy: boolean;
  // Make the ticked agents (switched off, no spending) and, if typed, set the workspace's cap.
  // Says how many were made; any problem is shown by the Agents tab.
  onCreate: (roleIds: string[], cap: number | null) => Promise<number>;
  // A project just added: ask the scout straight away if it has never been asked.
  askNow?: boolean;
  pollMs?: number;
}

// The scout on the Agents tab (ADR-039, F17 slice 5c): ask it, watch it work, then tick agents and Create.
// It only reads and advises; nothing runs or spends until an agent is switched on with a cap (Windows Hello).
export function ScoutPanel({ slug, name, roles, given, busy, onCreate, askNow = false, pollMs = POLL_MS }: Props) {
  const [loaded, setLoaded] = useState<Load>({ state: "loading" });
  const [problem, setProblem] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [made, setMade] = useState<number | null>(null);
  // A project just added is asked about once, and only if the scout has never looked at it.
  const autoAsked = useRef(false);

  const [reads, setReads] = useState(0);

  const ask = useCallback(() => {
    setAsking(true);
    setProblem(null);
    setMade(null);
    api.askScout(slug).then(
      (view) => {
        setLoaded({ state: "ready", view });
        setAsking(false);
      },
      (failure: unknown) => {
        setProblem(reasonWords(failure instanceof ApiError ? failure.code : null));
        setAsking(false);
      },
    );
  }, [slug]);

  // Read the newest run when the page opens, and each time the timer below says so.
  useEffect(() => {
    let live = true;
    api.scout(slug).then(
      (view) => {
        if (!live) return;
        setLoaded({ state: "ready", view });
        if (askNow && !autoAsked.current && view.state === "none" && view.connected) {
          autoAsked.current = true;
          ask();
        }
      },
      () => {
        if (live) setLoaded({ state: "failed" });
      },
    );
    return () => {
      live = false;
    };
  }, [slug, reads, askNow, ask]);

  // While a run is going, read again a few seconds after each answer.
  useEffect(() => {
    if (loaded.state !== "ready" || loaded.view.state !== "running") return;
    const timer = setTimeout(() => {
      setReads((n) => n + 1);
    }, pollMs);
    return () => {
      clearTimeout(timer);
    };
  }, [loaded, pollMs]);

  if (loaded.state === "loading") return <p className="text-sm text-muted">Loading the scout…</p>;
  if (loaded.state === "failed") {
    return (
      <p role="alert" className="text-sm text-alert">
        Could not load the scout.
      </p>
    );
  }
  const scout = loaded.view;
  const step: Step =
    made !== null ? "done" : scout.state === "done" ? "choose" : scout.state === "none" ? "connect" : "review";

  return (
    <section aria-labelledby={`scout-${slug}`} className="flex flex-col gap-4 rounded-2xl border border-line bg-panel p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id={`scout-${slug}`} className="flex items-center gap-2 font-semibold text-ink">
          <Telescope className="size-4 text-brand" aria-hidden="true" />
          The scout
        </h2>
        <Steps current={step} />
      </div>

      <p className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted">
        <span>
          GitHub: {scout.repo ?? "the project's repository"}
          {scout.branch !== null && ` (${scout.branch})`}
        </span>
        <span>Folder: not connected yet</span>
        <span>The scout reads only. It changes nothing.</span>
      </p>

      {problem !== null && (
        <p role="alert" className="rounded-xl border border-alert bg-alert-soft px-4 py-2.5 text-sm text-ink">
          {problem}
        </p>
      )}

      {!scout.connected && scout.state !== "done" && (
        <p className="text-sm text-muted">{REASON["scout.not_connected"]}</p>
      )}

      {scout.state === "none" && scout.connected && (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-muted">
            The scout reads {name}'s repository on our own server's model and recommends agents, each with its reason
            and the files it found.
          </p>
          <button type="button" className={PRIMARY} disabled={asking} onClick={ask}>
            {asking ? "Asking…" : "Ask the scout"}
          </button>
        </div>
      )}

      {scout.state === "running" && (
        <p role="status" className="rounded-xl bg-brand-soft px-4 py-3 text-sm text-ink">
          {STAGE[scout.stage ?? "reading"] ?? STAGE.reading}
          {scout.started_at !== null && ` Started ${ago(scout.started_at)}.`} You can leave this page; the scout keeps
          going.
        </p>
      )}

      {scout.state === "failed" && (
        <div className="flex flex-wrap items-center gap-3">
          <p role="alert" className="text-sm text-ink">
            {reasonWords(scout.reason)}
          </p>
          {scout.connected && (
            <button type="button" className={BUTTON} disabled={asking} onClick={ask}>
              {asking ? "Asking…" : "Ask the scout again"}
            </button>
          )}
        </div>
      )}

      {scout.state === "done" && scout.report !== null && (
        <Advice
          // A new run starts the ticks afresh.
          key={scout.finished_at ?? "done"}
          report={scout.report}
          finishedAt={scout.finished_at}
          roles={roles}
          given={given}
          busy={busy || asking}
          made={made}
          canAsk={scout.connected}
          onAsk={ask}
          onCreate={async (ids, cap) => {
            setMade(await onCreate(ids, cap));
          }}
        />
      )}
    </section>
  );
}

function Steps({ current }: { current: Step }) {
  const at = STEPS.findIndex(([s]) => s === current);
  return (
    <ol aria-label="Steps" className="flex flex-wrap gap-2">
      {STEPS.map(([step, label], n) => (
        <li
          key={step}
          aria-current={n === at ? "step" : undefined}
          className={[
            "flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-semibold",
            n === at ? "bg-brand text-on-brand" : n < at ? "bg-brand-soft text-brand-ink" : "bg-raised text-muted",
          ].join(" ")}
        >
          {n < at ? <Check className="size-3.5" aria-hidden="true" /> : <span aria-hidden="true">{n + 1}</span>}
          {label}
          {n < at && <span className="sr-only">(done)</span>}
        </li>
      ))}
    </ol>
  );
}

interface AdviceProps {
  report: ScoutReport;
  finishedAt: string | null;
  roles: readonly RoleChoice[];
  given: ReadonlySet<string>;
  busy: boolean;
  made: number | null;
  canAsk: boolean;
  onAsk: () => void;
  onCreate: (roleIds: string[], cap: number | null) => Promise<void>;
}

interface LibraryRole {
  id: string;
  title: string;
  why: string;
}

function Advice({ report, finishedAt, roles, given, busy, made, canAsk, onAsk, onCreate }: AdviceProps) {
  const ids = useId();
  const known = new Set(roles.map((r) => r.id));
  // Only roles still in the library can be made; ones this workspace has are shown as already there.
  const recommended = report.roles.filter((r) => r.recommend && known.has(r.role_id));
  const advised = new Map(report.roles.map((r) => [r.role_id, r]));
  const library: LibraryRole[] = roles
    .filter((r) => !advised.get(r.id)?.recommend)
    .map((r) => ({ id: r.id, title: r.title, why: notRecommendedWhy(advised.get(r.id)) }));

  const [picked, setPicked] = useState<ReadonlySet<string>>(
    () => new Set(recommended.map((r) => r.role_id).filter((id) => !given.has(id))),
  );
  const [find, setFind] = useState("");
  const [showAll, setShowAll] = useState(false);
  const [capText, setCapText] = useState("");
  const [creating, setCreating] = useState(false);
  const cap = centsFrom(capText);

  const flip = (id: string) => {
    setPicked((now) => {
      const next = new Set(now);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const wanted = [...picked].filter((id) => !given.has(id) && known.has(id));
  const query = find.trim().toLowerCase();
  const matching = library.filter((r) => query === "" || r.title.toLowerCase().includes(query));
  const shown = showAll || query !== "" ? matching : matching.slice(0, LIBRARY_SHOWN);

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-ink">{report.summary}</p>
      <p className="text-xs text-faint">
        Scout finished{finishedAt !== null && ` ${ago(finishedAt)}`}. Every file it names was checked against the
        repository by code.
      </p>
      {report.notes.length > 0 && (
        <ul aria-label="What code changed in the scout's answer" className="flex flex-col gap-1 text-sm text-muted">
          {report.notes.map((note) => (
            <li key={note}>Checked by code: {note}</li>
          ))}
        </ul>
      )}

      {made !== null && (
        <p role="status" className="rounded-xl bg-ok-soft px-4 py-3 text-sm text-ink">
          {made === 1 ? "1 agent was made" : `${String(made)} agents were made`}, switched off with no spending. Switch
          each on below when you are ready (Windows Hello).
        </p>
      )}

      <div className="flex flex-wrap items-start gap-4">
        <section aria-labelledby={`${ids}-rec`} className="flex min-w-0 flex-[3_1_28rem] flex-col">
          <h3 id={`${ids}-rec`} className="font-semibold text-ink">
            The scout recommends
          </h3>
          <p className="mb-2 text-sm text-muted">Tick the agents you want. Nothing runs until you press Create.</p>
          {recommended.length === 0 && (
            <p className="text-sm text-muted">Nothing yet: the scout found no evidence in the code for any role.</p>
          )}
          <ul className="flex flex-col divide-y divide-line border-t border-line">
            {recommended.map((advice) => (
              <Recommended
                key={advice.role_id}
                advice={advice}
                has={given.has(advice.role_id)}
                ticked={picked.has(advice.role_id)}
                onFlip={() => {
                  flip(advice.role_id);
                }}
              />
            ))}
          </ul>
          {report.suggested_providers.length > 0 && (
            <div className="mt-4 text-sm text-muted">
              <p className="font-medium text-ink">The scout thinks it may also use (not checked by code):</p>
              <ul className="mt-1 flex flex-col gap-0.5">
                {report.suggested_providers.map((p) => (
                  <li key={p.name}>
                    {p.name}: <span className="break-all">{p.files.join(", ")}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        <aside aria-labelledby={`${ids}-lib`} className="flex min-w-0 flex-[2_1_20rem] flex-col gap-2">
          <h3 id={`${ids}-lib`} className="font-semibold text-ink">
            Add more from the library
          </h3>
          <label htmlFor={`${ids}-find`} className="text-sm text-muted">
            Search {library.length} roles
          </label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-3.5 left-3 size-4 text-faint" aria-hidden="true" />
            <input
              id={`${ids}-find`}
              type="search"
              value={find}
              placeholder="e.g. Finance, Legal…"
              onChange={(event) => {
                setFind(event.target.value);
              }}
              className="min-h-11 w-full rounded-lg border border-line bg-panel pr-3 pl-9 text-sm text-ink"
            />
          </div>
          <ul className="flex flex-col divide-y divide-line border-t border-line">
            {shown.map((role) => {
              const has = given.has(role.id);
              const on = picked.has(role.id);
              return (
                <li key={role.id} className="flex items-center gap-3 py-2">
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="text-sm font-medium text-ink">{role.title}</span>
                    <span className="text-xs text-muted">{has ? "Already in this workspace." : role.why}</span>
                  </span>
                  {!has && (
                    <button
                      type="button"
                      aria-pressed={on}
                      aria-label={`${on ? "Remove" : "Add"} ${role.title}`}
                      disabled={busy || creating}
                      onClick={() => {
                        flip(role.id);
                      }}
                      className={[
                        "inline-flex min-h-11 min-w-11 items-center justify-center gap-1 rounded-lg border px-3 text-sm font-medium",
                        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50",
                        on ? "border-brand bg-brand-soft text-brand-ink" : "border-line bg-panel text-ink hover:bg-raised",
                      ].join(" ")}
                    >
                      {on ? "Added" : <Plus className="size-4" aria-hidden="true" />}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
          {matching.length === 0 && <p className="text-sm text-muted">No role matches "{find.trim()}".</p>}
          {!showAll && query === "" && matching.length > LIBRARY_SHOWN && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => {
                setShowAll(true);
              }}
            >
              Show all {matching.length} roles
            </button>
          )}
        </aside>
      </div>

      <form
        className="flex flex-wrap items-end gap-3 border-t border-line pt-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (cap === "bad" || wanted.length === 0) return;
          setCreating(true);
          void onCreate(wanted, cap).finally(() => {
            setCreating(false);
          });
        }}
      >
        <label htmlFor={`${ids}-cap`} className="flex flex-col gap-1 text-sm font-medium text-ink">
          Monthly spend cap for this workspace (USD)
          <input
            id={`${ids}-cap`}
            inputMode="decimal"
            value={capText}
            placeholder="leave as it is"
            aria-invalid={cap === "bad"}
            aria-describedby={`${ids}-cap-help`}
            onChange={(event) => {
              setCapText(event.target.value);
            }}
            className="min-h-11 w-40 rounded-lg border border-line bg-panel px-3 font-normal text-ink"
          />
        </label>
        <p id={`${ids}-cap-help`} className="min-w-60 flex-1 text-sm text-muted">
          {cap === "bad"
            ? "Type a number of dollars, like 20 or 12.50."
            : "Agents are made switched off and cannot spend until a cap is set. Setting a cap uses Windows Hello."}
        </p>
        {canAsk && (
          <button type="button" className={BUTTON} disabled={busy || creating} onClick={onAsk}>
            Ask the scout again
          </button>
        )}
        <button type="submit" className={PRIMARY} disabled={busy || creating || wanted.length === 0 || cap === "bad"}>
          {creating ? "Creating…" : `Create ${String(wanted.length)} ${wanted.length === 1 ? "agent" : "agents"}`}
        </button>
      </form>
    </div>
  );
}

function notRecommendedWhy(advice: RoleAdvice | undefined): string {
  if (advice === undefined) return "Not reviewed by the scout.";
  if (advice.downgraded) return "Not recommended: no evidence in the code, only in the docs.";
  return advice.reason;
}

interface RecommendedProps {
  advice: RoleAdvice;
  has: boolean;
  ticked: boolean;
  onFlip: () => void;
}

function Recommended({ advice, has, ticked, onFlip }: RecommendedProps) {
  const id = useId();
  return (
    <li className="flex gap-3 py-3">
      <input
        id={id}
        type="checkbox"
        checked={has || ticked}
        disabled={has}
        onChange={onFlip}
        aria-describedby={`${id}-why`}
        className="mt-0.5 size-5 shrink-0 accent-brand"
      />
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor={id} className="font-semibold text-ink">
          {advice.title}
          {has && <span className="ml-2 text-sm font-normal text-muted">Already in this workspace</span>}
        </label>
        <p id={`${id}-why`} className="text-sm text-ink">
          {advice.reason}
        </p>
        {advice.files.length > 0 && (
          <p className="text-xs break-all text-muted">Found: {advice.files.join(", ")}</p>
        )}
        {advice.removed_files > 0 && (
          <p className="text-xs text-muted">
            Code removed {advice.removed_files === 1 ? "1 file name that does" : `${String(advice.removed_files)} file names that do`}{" "}
            not exist.
          </p>
        )}
      </div>
    </li>
  );
}
