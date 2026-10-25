import { Compass, Search, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";

import {
  ApiError,
  api,
  type ClaudeKeyStatus,
  type ItemState,
  type Kind,
  type PasskeyStatus,
  type RegistryPage,
} from "../api";
import { PasskeyProblem, approveWithPasskey, onPasskeyAddress, setUpPasskey, withPasskeyIfNeeded } from "../passkey";
import { ClaudeKeyPanel } from "./ClaudeKeyPanel";
import { PasskeyPanel } from "./PasskeyPanel";
import { ConnectorCard, PluginCard, SkillCard, skillTitle } from "./RegistryCards";
import { RequestList, SwitchControl, reason } from "./RegistrySwitch";

const PASSKEY_PROBLEM: Record<PasskeyProblem["reason"], string> = {
  unsupported: "This browser can't use passkeys. Use Chrome or Edge (version 129 or newer).",
  wrong_address: "Passkeys only work when the dashboard is opened at localhost.",
  cancelled:
    "Windows Hello was closed before confirming, so nothing changed. If Bitwarden or another password manager opened instead, choose its option to use another device.",
};

function explain(failure: unknown): string {
  if (failure instanceof PasskeyProblem) return PASSKEY_PROBLEM[failure.reason];
  return reason(failure instanceof ApiError ? failure.code : null);
}

type Shelf = "yours" | "discover";

const TABS: { kind: Kind; label: string }[] = [
  { kind: "connector", label: "Connectors" },
  { kind: "skill", label: "Skills" },
  { kind: "plugin", label: "Plugins" },
];

const EMPTY: Record<Shelf, Record<Kind, string>> = {
  yours: {
    connector: "No connectors are on.",
    skill: "No skills are on.",
    plugin: "Plugins from outside are view only for now; see Discover.",
  },
  discover: {
    connector: "Every connector Agent Zaaheen has is on.",
    skill: "Every skill Agent Zaaheen has is on.",
    plugin: "No plugins found. Start the dashboard with --bundles DIR to show plugin bundles from a folder.",
  },
};

// Yours: what Agent Zaaheen may use now. Discover: everything else it has found.
const ON_SHELF: Record<Shelf, (state: ItemState) => boolean> = {
  yours: (state) => state === "on",
  discover: (state) => state !== "on",
};

interface Props {
  page: RegistryPage;
  projectName: (id: string) => string;
  onChange: (page: RegistryPage) => void;
}

function matches(query: string, ...texts: string[]): boolean {
  const wanted = query.trim().toLowerCase();
  return wanted === "" || texts.some((text) => text.toLowerCase().includes(wanted));
}

const SEGMENT = [
  "rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
].join(" ");

export function CustomizeView({ page, projectName, onChange }: Props) {
  const [tab, setTab] = useState<Kind>("connector");
  const [shelf, setShelf] = useState<Shelf>("yours");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [passkey, setPasskey] = useState<PasskeyStatus | null>(null);
  const [claudeKey, setClaudeKey] = useState<ClaudeKeyStatus | null>(null);

  useEffect(() => {
    if (!page.switches) return;
    let live = true;
    api.passkey().then(
      (status) => {
        if (live) setPasskey(status);
      },
      () => {
        if (live) setPasskey(null);
      },
    );
    api.claudeKey().then(
      (status) => {
        if (live) setClaudeKey(status);
      },
      () => {
        if (live) setClaudeKey(null);
      },
    );
    return () => {
      live = false;
    };
  }, [page.switches]);

  const connectorName = (id: string) => page.connectors.find((c) => c.id === id)?.name ?? id;
  const title = (kind: Kind, id: string) =>
    kind === "connector" ? connectorName(id) : kind === "skill" ? skillTitle(id) : id;
  const keep = ON_SHELF[shelf];
  const connectors = page.connectors.filter(
    (c) => keep(c.state) && matches(query, c.name, c.category, c.description),
  );
  const skills = page.skills.filter((s) => keep(s.state) && matches(query, s.id, skillTitle(s.id), s.description));
  const plugins = page.plugins.filter((p) => keep(p.state) && matches(query, p.id, p.description));
  const counts: Record<Kind, number> = {
    connector: page.connectors.filter((c) => keep(c.state)).length,
    skill: page.skills.filter((s) => keep(s.state)).length,
    plugin: page.plugins.filter((p) => keep(p.state)).length,
  };
  const shown = { connector: connectors.length, skill: skills.length, plugin: plugins.length }[tab];
  const problems = page.problems.filter((p) => p.kind === tab);

  function act(change: Promise<RegistryPage>) {
    setBusy(true);
    setError(null);
    change.then(
      (next) => {
        onChange(next);
        setBusy(false);
      },
      (failure: unknown) => {
        setError(explain(failure));
        setBusy(false);
      },
    );
  }

  function setUp(code: string) {
    setBusy(true);
    setError(null);
    setUpPasskey(code).then(
      (status) => {
        setPasskey(status);
        setBusy(false);
      },
      (failure: unknown) => {
        setError(explain(failure));
        setBusy(false);
      },
    );
  }

  function saveClaudeKey(key: string) {
    setBusy(true);
    setError(null);
    withPasskeyIfNeeded(
      () => api.saveClaudeKey(key),
      () => api.claudeKeyOptions(key),
      (credential) => api.saveClaudeKey(key, credential),
    ).then(
      (status) => {
        setClaudeKey(status);
        setBusy(false);
      },
      (failure: unknown) => {
        setError(explain(failure));
        setBusy(false);
      },
    );
  }

  function control(kind: Kind, id: string, state: ItemState) {
    if (!page.switches) return undefined;
    return (
      <SwitchControl
        name={title(kind, id)}
        state={state}
        waiting={page.requests.some((r) => r.kind === kind && r.item_id === id)}
        busy={busy}
        onSwitchOff={() => {
          act(api.switchOff(kind, id));
        }}
        onRequest={() => {
          act(api.requestOn(kind, id));
        }}
      />
    );
  }

  return (
    <section aria-labelledby="customize-title" className="flex flex-col gap-6">
      <header>
        <h1 id="customize-title" className="text-2xl font-semibold tracking-tight text-ink">
          Customize
        </h1>
        <p className="mt-1 text-muted">
          {page.switches
            ? "The connectors, skills and plugins Agent Zaaheen can use. Switch off anything you don't want used; switching something on always comes to you for approval."
            : "The connectors, skills and plugins Agent Zaaheen can use. Read-only for now: start the dashboard with --switches to change what is on."}
        </p>
      </header>

      {page.switches && passkey !== null && (
        <PasskeyPanel status={passkey} onAddress={onPasskeyAddress()} busy={busy} onSetUp={setUp} />
      )}

      {page.switches && claudeKey?.switches === true && (
        <ClaudeKeyPanel
          status={claudeKey}
          canSign={passkey?.enrolled === true && onPasskeyAddress()}
          busy={busy}
          onSave={saveClaudeKey}
        />
      )}

      {error !== null && (
        <p role="alert" className="rounded-xl border border-alert bg-alert-soft px-4 py-3 text-sm text-ink">
          {error}
        </p>
      )}

      <RequestList
        requests={page.requests}
        title={title}
        busy={busy}
        canApprove={passkey?.enrolled === true && onPasskeyAddress()}
        onWithdraw={(requestId) => {
          act(api.withdraw(requestId));
        }}
        onRefuse={(requestId) => {
          act(api.refuse(requestId));
        }}
        onApprove={(requestId) => {
          act(approveWithPasskey(requestId));
        }}
      />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="tablist" aria-label="Kind" className="flex gap-1 rounded-xl bg-raised p-1">
          {TABS.map(({ kind, label }) => (
            <button
              key={kind}
              type="button"
              role="tab"
              aria-selected={tab === kind}
              onClick={() => {
                setTab(kind);
              }}
              className={`${SEGMENT} ${tab === kind ? "bg-panel text-ink shadow-sm" : "text-muted hover:text-ink"}`}
            >
              {label} <span className="text-faint tabular">{counts[kind]}</span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <div role="group" aria-label="Show" className="flex gap-1 rounded-xl bg-raised p-1">
            {(["yours", "discover"] as const).map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={shelf === value}
                onClick={() => {
                  setShelf(value);
                }}
                className={`${SEGMENT} ${shelf === value ? "bg-panel text-ink shadow-sm" : "text-muted hover:text-ink"}`}
              >
                {value === "yours" ? "Yours" : "Discover"}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 rounded-xl border border-line bg-panel px-3 py-1.5 focus-within:outline-2 focus-within:outline-brand">
            <Search className="size-4 text-faint" aria-hidden="true" />
            <span className="sr-only">Search</span>
            <input
              type="search"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
              }}
              placeholder="Search"
              className="w-36 bg-transparent text-sm text-ink outline-none placeholder:text-faint"
            />
          </label>
        </div>
      </div>

      {shelf === "discover" && (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Compass className="size-4 text-faint" aria-hidden="true" />
          Everything Agent Zaaheen has found that is not on. Asking to switch one on comes to you for approval.
        </p>
      )}
      {shelf === "yours" && problems.length > 0 && (
        <div className="rounded-xl border border-alert bg-alert-soft px-4 py-3 text-sm text-ink">
          <p className="flex items-center gap-2 font-medium">
            <TriangleAlert className="size-4 text-alert" aria-hidden="true" />
            {problems.length === 1 ? "1 item could not be loaded" : `${problems.length} items could not be loaded`}
          </p>
          <ul className="mt-1.5 flex flex-col gap-1 text-muted">
            {problems.map((problem) => (
              <li key={problem.path}>
                <span className="font-mono text-xs text-ink">{problem.path}</span>: {problem.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {shown === 0 ? (
        <p className="text-muted">{query.trim() ? `Nothing matches "${query.trim()}".` : EMPTY[shelf][tab]}</p>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(18rem,1fr))] items-start gap-4">
          {tab === "connector" &&
            connectors.map((item) => (
              <ConnectorCard
                key={item.id}
                item={item}
                projectName={projectName}
                control={control("connector", item.id, item.state)}
              />
            ))}
          {tab === "skill" &&
            skills.map((item) => (
              <SkillCard
                key={item.id}
                item={item}
                connectorName={connectorName}
                control={control("skill", item.id, item.state)}
              />
            ))}
          {tab === "plugin" && plugins.map((item) => <PluginCard key={item.id} item={item} />)}
        </div>
      )}
    </section>
  );
}
