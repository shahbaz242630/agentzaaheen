import { Archive, ArchiveRestore, Trash2, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { ApiError, api, type ArchivedList } from "../api";
import { PasskeyProblem, withPasskeyIfNeeded } from "../passkey";

type Choice = "archive" | "erase";
type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "failed" };

const REASON: Record<string, string> = {
  "workspaces.read_only": "Removing a project needs the laptop database. Start the dashboard with --switches.",
  "workspaces.store_unavailable": "The laptop database did not answer. Is it running?",
  "workspaces.not_allowed": "That is not allowed from here.",
  "workspaces.passkey_failed": "Windows Hello did not confirm it, so nothing changed. Please try again.",
  "workspaces.not_active": "This project is already archived.",
  "workspaces.not_archived": "This project is not archived any more.",
  "workspace.not_archived": "This project is not archived any more.",
  "registry.no_passkey": "Set up your passkey first (start the dashboard with --enrol-passkey).",
};
const PROBLEM: Record<PasskeyProblem["reason"], string> = {
  unsupported: "This browser can't use Windows Hello here. Use Edge or Chrome.",
  wrong_address: "Open the dashboard at http://localhost to use Windows Hello.",
  cancelled: "Windows Hello was closed, so nothing changed.",
};

function said(failure: unknown): string {
  if (failure instanceof PasskeyProblem) return PROBLEM[failure.reason];
  const code = failure instanceof ApiError ? failure.code : null;
  return (code && REASON[code]) ?? "That did not work. Please try again.";
}

// What each choice does, in a line or two: the popup shows these before anything happens.
export function explainer(choice: Choice, name: string): string {
  return choice === "archive"
    ? `Stops everything for ${name}: agents off, spending stopped, saved keys deleted. Its history is kept, so you can bring it back from the Board; it comes back switched off.`
    : `Everything Archive does, then all of ${name}'s data is erased for good. This cannot be undone. The audit log keeps a record that it existed.`;
}

const signed = {
  archive: (slug: string) =>
    withPasskeyIfNeeded(
      () => api.archive(slug),
      () => api.archiveOptions(slug),
      (credential) => api.archive(slug, credential),
    ),
  erase: (slug: string) =>
    withPasskeyIfNeeded(
      () => api.erase(slug),
      () => api.eraseOptions(slug),
      (credential) => api.erase(slug, credential),
    ),
  restore: (slug: string) =>
    withPasskeyIfNeeded(
      () => api.restore(slug),
      () => api.restoreOptions(slug),
      (credential) => api.restore(slug, credential),
    ),
};

const QUIET = [
  "inline-flex min-h-11 items-center gap-2 rounded-lg border border-line bg-panel px-4 text-sm font-medium text-ink",
  "hover:bg-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50",
].join(" ");
const DANGER = [
  "inline-flex min-h-11 items-center gap-2 rounded-lg border border-alert bg-alert-soft px-4 text-sm font-medium text-alert",
  "hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50",
].join(" ");

interface DialogProps {
  slug: string;
  name: string;
  // Only "erase" when the project is already archived (from the Board's archived list).
  choices: readonly Choice[];
  onClose: () => void;
  // The project is no longer active (archived, or deleted for good).
  onDone: (message: string) => void;
}

// The popup: pick Archive or Delete for good, read what each does, then Windows Hello signs it.
export function RemoveDialog({ slug, name, choices, onClose, onDone }: DialogProps) {
  const ids = useId();
  const [choice, setChoice] = useState<Choice>(choices[0] ?? "archive");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const first = useRef<HTMLInputElement>(null);

  useEffect(() => {
    first.current?.focus();
  }, []);

  async function go() {
    setBusy(true);
    setProblem(null);
    let archived = !choices.includes("archive");
    try {
      if (!archived) {
        await signed.archive(slug);
        archived = true;
      }
      if (choice === "erase") {
        await signed.erase(slug);
        onDone(`${name} was deleted for good. The audit log keeps a record that it existed.`);
      } else {
        onDone(`${name} is archived. You can bring it back from the Board.`);
      }
    } catch (failure) {
      if (archived && choices.includes("archive")) {
        // The first signature went through; only deleting it for good did not.
        onDone(`${name} is archived, but it was not deleted for good: ${said(failure)} You can delete it from the Board.`);
      } else {
        setProblem(said(failure));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${ids}-title`}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !busy) onClose();
        }}
        className="flex w-full max-w-lg flex-col gap-4 rounded-2xl border border-line bg-panel p-6 shadow-lg"
      >
        <div className="flex items-start justify-between gap-3">
          <h2 id={`${ids}-title`} className="text-lg font-semibold text-ink">
            Remove {name}
          </h2>
          <button type="button" aria-label="Close" disabled={busy} onClick={onClose} className={QUIET}>
            <X className="size-4" aria-hidden="true" />
          </button>
        </div>

        <fieldset className="flex flex-col gap-3">
          <legend className="sr-only">How to remove {name}</legend>
          {choices.map((c, n) => (
            <label
              key={c}
              className={[
                "flex cursor-pointer gap-3 rounded-xl border p-4",
                choice === c ? (c === "erase" ? "border-alert bg-alert-soft" : "border-brand bg-brand-soft") : "border-line",
              ].join(" ")}
            >
              <input
                ref={n === 0 ? first : undefined}
                type="radio"
                name={`${ids}-choice`}
                value={c}
                checked={choice === c}
                disabled={busy}
                onChange={() => {
                  setChoice(c);
                }}
                className="mt-1"
              />
              <span className="flex flex-col gap-1">
                <span className="font-medium text-ink">{c === "archive" ? "Archive" : "Delete for good"}</span>
                <span className="text-sm text-muted">{explainer(c, name)}</span>
              </span>
            </label>
          ))}
        </fieldset>

        {choice === "erase" && choices.includes("archive") && (
          <p className="text-sm text-muted">Windows Hello asks twice: once to archive, once to delete.</p>
        )}
        {problem !== null && (
          <p role="alert" className="rounded-xl border border-alert bg-alert-soft px-4 py-2.5 text-sm text-ink">
            {problem}
          </p>
        )}

        <div className="flex flex-wrap justify-end gap-3">
          <button type="button" disabled={busy} onClick={onClose} className={QUIET}>
            Cancel
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void go()}
            className={choice === "erase" ? DANGER : QUIET}
          >
            {choice === "erase" ? <Trash2 className="size-4" aria-hidden="true" /> : <Archive className="size-4" aria-hidden="true" />}
            {choice === "erase" ? `Delete ${name} for good` : `Archive ${name}`}
          </button>
        </div>
      </div>
    </div>
  );
}

// The quiet button on a workspace's page that opens the popup.
export function RemoveButton({ slug, name, onRemoved }: { slug: string; name: string; onRemoved: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
        }}
        className={QUIET}
      >
        <Archive className="size-4" aria-hidden="true" />
        Remove project
      </button>
      {open && (
        <RemoveDialog
          slug={slug}
          name={name}
          choices={["archive", "erase"]}
          onClose={() => {
            setOpen(false);
          }}
          onDone={(message) => {
            setOpen(false);
            onRemoved(message);
          }}
        />
      )}
    </>
  );
}

// The Board's archived projects: bring one back (it comes back switched off) or delete it for good.
export function ArchivedProjects({ onChanged }: { onChanged: () => void }) {
  const [loaded, setLoaded] = useState<Load<ArchivedList>>({ state: "loading" });
  const [deleting, setDeleting] = useState<{ slug: string; name: string } | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.archived().then(
      (data) => {
        if (live) setLoaded({ state: "ready", data });
      },
      () => {
        if (live) setLoaded({ state: "failed" });
      },
    );
    return () => {
      live = false;
    };
  }, []);

  async function bringBack(slug: string, name: string) {
    setBusy(true);
    setNote(null);
    try {
      setLoaded({ state: "ready", data: await signed.restore(slug) });
      setNote(`${name} is back, switched off. Reconnect its keys and switch its agents on when you are ready.`);
      onChanged();
    } catch (failure) {
      setNote(said(failure));
    } finally {
      setBusy(false);
    }
  }

  function refresh(message: string) {
    setDeleting(null);
    setNote(message);
    api.archived().then(
      (data) => {
        setLoaded({ state: "ready", data });
      },
      () => {
        setLoaded({ state: "failed" });
      },
    );
  }

  if (loaded.state !== "ready" || !loaded.data.switches) return null;
  const archived = loaded.data.workspaces;
  if (archived.length === 0 && note === null) return null;

  return (
    <section aria-labelledby="board-archived" className="flex flex-col gap-3">
      <h2 id="board-archived" className="font-semibold text-ink">
        Archived projects
      </h2>
      {note !== null && (
        <p role="status" className="rounded-xl border border-line bg-raised px-4 py-2.5 text-sm text-ink">
          {note}
        </p>
      )}
      {archived.length > 0 && (
        <ul className="flex flex-col divide-y divide-line rounded-2xl border border-line bg-panel">
          {archived.map((w) => (
            <li key={w.slug} className="flex flex-wrap items-center gap-3 px-5 py-3">
              <span className="min-w-0 flex-1 font-medium text-ink">{w.name}</span>
              <button type="button" disabled={busy} onClick={() => void bringBack(w.slug, w.name)} className={QUIET}>
                <ArchiveRestore className="size-4" aria-hidden="true" />
                Bring {w.name} back
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setDeleting(w);
                }}
                className={DANGER}
              >
                <Trash2 className="size-4" aria-hidden="true" />
                Delete {w.name} for good
              </button>
            </li>
          ))}
        </ul>
      )}
      {deleting !== null && (
        <RemoveDialog
          slug={deleting.slug}
          name={deleting.name}
          choices={["erase"]}
          onClose={() => {
            setDeleting(null);
          }}
          onDone={refresh}
        />
      )}
    </section>
  );
}
