import { Plus, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { ApiError, api, type RepoList } from "../api";

type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "failed" };

const REASON: Record<string, string> = {
  "workspaces.read_only": "Adding a project needs the laptop database. Start the dashboard with --switches.",
  "workspaces.store_unavailable": "The laptop database did not answer. Is it running?",
  "workspaces.not_allowed": "That is not allowed from here.",
  "workspaces.bad_name": "Give the project a name (up to 80 characters).",
  "workspaces.bad_repo": "That repository name is not written owner/name.",
  "workspaces.name_taken": "A project with that name exists (or did once). Choose another name.",
  "workspaces.repo_taken": "Another project already reads that repository.",
  "workspaces.repo_not_covered":
    "Agent Zaaheen's read-only GitHub token can't read that repository. Add it to the token on GitHub first.",
  "source.unavailable": "GitHub did not answer. Please try again in a minute.",
};

function said(failure: unknown): string {
  const code = failure instanceof ApiError ? failure.code : null;
  return (code && REASON[code]) ?? "That did not work. Please try again.";
}

const BUTTON =
  "min-h-11 rounded-lg px-4 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";
const FIELD =
  "min-h-11 w-full rounded-lg border border-line bg-panel px-3 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";

interface Props {
  // The project was added: read the workspace list again and open it.
  onAdded: (slug: string) => void;
}

// "+ Add a project" on the Board (F17 slice 5a): a name and, if wanted, one repository the token covers.
// Nothing in a new project runs or spends, so no Windows Hello here; the scout and agents come next.
export function AddProject({ onAdded }: Props) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => {
          setOpen(true);
        }}
        className={`${BUTTON} inline-flex items-center gap-2 self-start border border-line bg-panel text-ink hover:bg-raised`}
      >
        <Plus aria-hidden="true" className="size-4" />
        Add a project
      </button>
    );
  }
  return (
    <AddProjectForm
      onClose={() => {
        setOpen(false);
      }}
      onAdded={onAdded}
    />
  );
}

function AddProjectForm({ onClose, onAdded }: Props & { onClose: () => void }) {
  const id = useId();
  const [repos, setRepos] = useState<Load<RepoList>>({ state: "loading" });
  const [name, setName] = useState("");
  const [repo, setRepo] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.repos().then(
      (data) => {
        if (live) setRepos({ state: "ready", data });
      },
      () => {
        if (live) setRepos({ state: "failed" });
      },
    );
    return () => {
      live = false;
    };
  }, []);

  function submit() {
    setBusy(true);
    setProblem(null);
    api.addProject(name, repo === "" ? null : repo).then(
      (added) => {
        onAdded(added.slug);
      },
      (failure: unknown) => {
        setProblem(said(failure));
        setBusy(false);
      },
    );
  }

  return (
    <form
      aria-labelledby={`${id}-title`}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      className="flex flex-col gap-4 rounded-2xl border border-line bg-panel p-5"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 id={`${id}-title`} className="font-semibold text-ink">
            Add a project
          </h3>
          <p className="mt-1 text-sm text-muted">
            It gets its own workspace. Nothing in it runs or spends until you choose agents and set a cap.
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close Add a project"
          className={`${BUTTON} inline-flex items-center border border-line bg-panel px-3 text-ink hover:bg-raised`}
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${id}-name`} className="text-sm font-medium text-ink">
          Project name
        </label>
        <input
          id={`${id}-name`}
          value={name}
          maxLength={80}
          required
          onChange={(event) => {
            setName(event.target.value);
          }}
          className={FIELD}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${id}-repo`} className="text-sm font-medium text-ink">
          GitHub repository (read-only)
        </label>
        {repos.state === "loading" && <p className="text-sm text-muted">Reading the repositories Agent Zaaheen can see…</p>}
        {repos.state === "failed" && (
          <p className="text-sm text-muted">
            The list of repositories could not be read. You can still add the project without one.
          </p>
        )}
        {repos.state === "ready" && !repos.data.connected && (
          <p className="text-sm text-muted">
            GitHub is not connected on this run, so the project is added without a repository.
          </p>
        )}
        {repos.state === "ready" && repos.data.connected && (
          <>
            <select
              id={`${id}-repo`}
              value={repo}
              onChange={(event) => {
                setRepo(event.target.value);
              }}
              className={FIELD}
            >
              <option value="">No repository for now</option>
              {repos.data.repos.map((r) => (
                <option key={r.repo} value={r.repo} disabled={r.used_by !== null}>
                  {r.used_by === null ? r.repo : `${r.repo} (used by ${r.used_by})`}
                </option>
              ))}
            </select>
            <p className="text-sm text-muted">
              Only repositories Agent Zaaheen's read-only token covers are listed. For another one, add it to the token on
              GitHub first.
            </p>
          </>
        )}
      </div>

      {problem !== null && (
        <p role="alert" className="rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink">
          {problem}
        </p>
      )}

      <div className="flex flex-wrap gap-3">
        <button
          type="submit"
          disabled={busy || name.trim() === ""}
          className={`${BUTTON} bg-brand text-on-brand hover:opacity-90 disabled:opacity-50`}
        >
          {busy ? "Adding…" : "Add project"}
        </button>
        <button type="button" onClick={onClose} className={`${BUTTON} border border-line bg-panel text-ink hover:bg-raised`}>
          Cancel
        </button>
      </div>
    </form>
  );
}
