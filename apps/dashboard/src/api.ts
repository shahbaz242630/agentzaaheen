// The API contract, checked on arrival (Rulebook §4: zod at the API boundary).
// Mirrors src/agentzaaheen/core/api/views.py. Anything that does not match is refused.
import { z } from "zod";

const code = z.string().regex(/^[a-z0-9_]+(\.[a-z0-9_]+)*$/);
const slug = z.string().regex(/^[a-z0-9][a-z0-9-]{0,39}$/);
const time = z.iso.datetime({ offset: true });

export const Value = z.discriminatedUnion("kind", [
  z.strictObject({ kind: z.literal("money"), minor: z.int(), currency: z.string().regex(/^[A-Z]{3}$/) }),
  z.strictObject({ kind: z.literal("count"), value: z.int().nonnegative() }),
  z.strictObject({ kind: z.literal("percent"), basis_points: z.int().min(0).max(10_000) }),
  z.strictObject({ kind: z.literal("unknown"), reason: code }),
]);
export type Value = z.infer<typeof Value>;

export const Freshness = z.enum(["fresh", "stale", "alert", "never"]);
export type Freshness = z.infer<typeof Freshness>;

export const Tone = z.enum(["ok", "info", "warn", "alert"]);
export type Tone = z.infer<typeof Tone>;

export const Metric = z.strictObject({ label: z.string(), value: Value, detail: z.string().nullable() });
export type Metric = z.infer<typeof Metric>;

export const Entry = z.strictObject({ text: z.string(), tone: Tone, at: time.nullable() });
export type Entry = z.infer<typeof Entry>;

// The UI enforces the same two card rules as the API (PRD F1.7), so a stale
// number can never be shown even if a future server forgets to check.
export const Card = z
  .strictObject({
    id: slug,
    title: z.string(),
    source: z.string(),
    freshness: Freshness,
    last_success_at: time.nullable(),
    sample: z.boolean(),
    metrics: z.array(Metric),
    entries: z.array(Entry),
  })
  .superRefine((card, ctx) => {
    if (card.freshness !== "fresh" && card.metrics.some((m) => m.value.kind !== "unknown")) {
      ctx.addIssue({ code: "custom", message: `card ${card.id} is ${card.freshness}: numbers must be unknown` });
    }
    if (card.freshness === "never" && card.last_success_at !== null) {
      ctx.addIssue({ code: "custom", message: `card ${card.id} never synced but has a last success time` });
    }
  });
export type Card = z.infer<typeof Card>;

export const Health = z.enum(["ok", "warn", "alert", "unknown"]);
export type Health = z.infer<typeof Health>;

export const ProjectSummary = z.strictObject({
  id: slug,
  name: z.string(),
  summary: z.string(),
  health: Health,
  escalations: z.int().nonnegative(),
  sample: z.boolean(),
});
export type ProjectSummary = z.infer<typeof ProjectSummary>;

export const ProjectDetail = z.strictObject({ project: ProjectSummary, cards: z.array(Card) });
export type ProjectDetail = z.infer<typeof ProjectDetail>;

export const ModelChoice = z.strictObject({
  id: slug,
  label: z.string(),
  where: z.enum(["local", "cloud"]),
  available: z.boolean(),
  default: z.boolean(),
  note: z.string(),
});
export type ModelChoice = z.infer<typeof ModelChoice>;

// Exactly one default model, and it is local (ADR-023).
export const ModelList = z.strictObject({ models: z.array(ModelChoice) }).refine(
  ({ models }) => {
    const defaults = models.filter((m) => m.default);
    return defaults.length === 1 && defaults[0]?.where === "local";
  },
  { message: "there must be exactly one default model, and it must be local" },
);

export const ChatAnswer = z.strictObject({
  model_id: slug,
  answer: z.string().min(1).max(4000),
  from_sample: z.boolean(),
  used: z.array(z.string().min(1).max(120)).max(8),
});
export type ChatAnswer = z.infer<typeof ChatAnswer>;

// The Customize page (F06). Mirrors src/agentzaaheen/core/api/registry.py.
const item = z.string().regex(/^[a-z0-9]+(-[a-z0-9]+)*$/);
const fingerprint = z.string().regex(/^[0-9a-f]{64}$/);

export const Level = z.enum(["read", "draft", "act", "forbidden"]);
export type Level = z.infer<typeof Level>;

export const Use = z.enum(["live", "failed", "needs_key"]);
export type Use = z.infer<typeof Use>;

// How an item stands (ADR-030): on, switched off, changed since approved (so off), or view only (outside bundle).
export const ItemState = z.enum(["on", "off", "changed", "view_only"]);
export type ItemState = z.infer<typeof ItemState>;

export const ConnectorItem = z.strictObject({
  id: item,
  name: z.string(),
  category: z.string(),
  description: z.string(),
  version: z.string(),
  level: Level,
  hosts: z.array(z.string()),
  endpoints: z.array(z.string()),
  key_slots: z.array(z.string()),
  fingerprint,
  uses: z.array(z.strictObject({ project_id: slug, status: Use })),
  state: ItemState,
});
export type ConnectorItem = z.infer<typeof ConnectorItem>;

export const SkillItem = z.strictObject({
  id: item,
  description: z.string(),
  invocation: z.enum(["auto", "manual"]),
  requires: z.array(item),
  missing: z.array(item),
  fingerprint,
  state: ItemState,
});
export type SkillItem = z.infer<typeof SkillItem>;

export const PluginItem = z.strictObject({
  id: item,
  description: z.string(),
  version: z.string().nullable(),
  layout: z.enum(["claude", "codex", "cursor"]),
  skills: z.int().nonnegative(),
  connectors: z.int().nonnegative(),
  commands: z.int().nonnegative(),
  agents: z.int().nonnegative(),
  hooks: z.boolean(),
  fingerprint,
  state: ItemState,
});
export type PluginItem = z.infer<typeof PluginItem>;

export const Kind = z.enum(["connector", "skill", "plugin"]);
export type Kind = z.infer<typeof Kind>;

export const SwitchRequest = z.strictObject({
  id: z.uuid(),
  kind: Kind,
  item_id: item,
  requested_at: time,
  stale: z.boolean(),
});
export type SwitchRequest = z.infer<typeof SwitchRequest>;

// ``switches``: the server has a database, so on/off can be changed (otherwise the page is read-only).
export const RegistryPage = z.strictObject({
  connectors: z.array(ConnectorItem),
  skills: z.array(SkillItem),
  plugins: z.array(PluginItem),
  problems: z.array(z.strictObject({ kind: Kind, path: z.string(), message: z.string() })),
  switches: z.boolean(),
  requests: z.array(SwitchRequest),
});
export type RegistryPage = z.infer<typeof RegistryPage>;

// The Agents tab (ADR-039, F17 slice 3). Mirrors src/agentzaaheen/core/api/agents.py. Money is in US cents.
export const RoleChoice = z.strictObject({
  id: item,
  title: z.string(),
  mission: z.string(),
  permission: z.enum(["read", "draft", "act"]),
});
export type RoleChoice = z.infer<typeof RoleChoice>;

export const AgentOut = z.strictObject({
  id: z.uuid(),
  role_id: item,
  title: z.string(),
  enabled: z.boolean(),
  model: z.string(),
  cap_minor: z.int().nonnegative().nullable(),
  spent_minor: z.int().nonnegative(),
  newer_version: z.boolean(),
});
export type AgentOut = z.infer<typeof AgentOut>;

export const WorkspaceAgents = z.strictObject({
  enabled: z.boolean(),
  cap_minor: z.int().nonnegative().nullable(),
  spent_minor: z.int().nonnegative(),
  agents: z.array(AgentOut),
  switches: z.boolean(),
});
export type WorkspaceAgents = z.infer<typeof WorkspaceAgents>;

// What a switch should become. No cap (null) means nothing may be spent.
export interface SwitchChange {
  enabled: boolean;
  cap_minor: number | null;
}

// Removing a project (ADR-039): what archiving switched off, and the archived projects.
export const ArchiveResult = z.strictObject({
  agents_stopped: z.int().nonnegative(),
  keys_deleted: z.int().nonnegative(),
});
export type ArchiveResult = z.infer<typeof ArchiveResult>;

export const ArchivedList = z.strictObject({
  workspaces: z.array(z.strictObject({ slug: item, name: z.string() })),
  switches: z.boolean(),
});
export type ArchivedList = z.infer<typeof ArchivedList>;

// Adding a project (F17 slice 5a): the repositories the read-only GitHub token covers, and who reads each.
const repoName = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/);
export const RepoList = z.strictObject({
  repos: z.array(z.strictObject({ repo: repoName, used_by: item.nullable() })),
  connected: z.boolean(),
});
export type RepoList = z.infer<typeof RepoList>;
export const AddedProject = z.strictObject({ slug: item, name: z.string() });

// What a workspace is built with (F17 slice 5b-1). Mirrors src/agentzaaheen/core/api/profile.py.
export const ProviderCategory = z.enum([
  "code",
  "hosting",
  "stores",
  "payments",
  "data",
  "monitoring",
  "messages",
  "ai",
  "sign_in",
]);
export type ProviderCategory = z.infer<typeof ProviderCategory>;
export const Profile = z
  .strictObject({
    state: z.enum(["ready", "no_repo", "not_connected", "failed"]),
    repo: repoName.nullable(),
    branch: z.string().nullable(),
    languages: z.array(
      z.strictObject({
        name: z.string().max(60),
        bytes: z.int().nonnegative(),
        share_bp: z.int().min(0).max(10_000),
      }),
    ),
    providers: z.array(
      z.strictObject({
        id: item,
        name: z.string().max(60),
        category: ProviderCategory,
        evidence: z.array(z.string()).min(1).max(3),
      }),
    ),
    changed_at: time.nullable(),
    checked_at: time.nullable(),
    reason: z.string().nullable(),
  })
  .refine(
    ({ languages }) => languages.length === 0 || languages.reduce((sum, l) => sum + l.share_bp, 0) === 10_000,
    { message: "language shares must add up to 100%" },
  );
export type Profile = z.infer<typeof Profile>;

// The scout's newest run and its advice, checked by code (F17 slice 5b-2). Mirrors src/agentzaaheen/core/api/scout.py.
const repoPath = z.string().min(1).max(400);
export const RoleAdvice = z.strictObject({
  role_id: item,
  title: z.string(),
  recommend: z.boolean(),
  reason: z.string().max(400),
  files: z.array(repoPath).max(5),
  removed_files: z.int().nonnegative(),
  downgraded: z.boolean(),
});
export type RoleAdvice = z.infer<typeof RoleAdvice>;
export const ScoutReport = z.strictObject({
  summary: z.string().max(600),
  roles: z.array(RoleAdvice),
  not_reviewed: z.array(item),
  suggested_providers: z
    .array(z.strictObject({ name: z.string().max(60), files: z.array(repoPath).min(1).max(5) }))
    .max(5),
  notes: z.array(z.string()),
});
export type ScoutReport = z.infer<typeof ScoutReport>;
export const ScoutView = z
  .strictObject({
    connected: z.boolean(),
    state: z.enum(["none", "running", "done", "failed"]),
    stage: z.enum(["reading", "thinking", "checking", "finished"]).nullable(),
    repo: repoName.nullable(),
    branch: z.string().nullable(),
    started_at: time.nullable(),
    finished_at: time.nullable(),
    reason: code.nullable(),
    report: ScoutReport.nullable(),
  })
  // Advice is shown only from a finished run; a run that is not done never carries any.
  .refine(({ state, report }) => (state === "done") === (report !== null), {
    message: "only a finished run carries advice",
  });
export type ScoutView = z.infer<typeof ScoutView>;

// The daily summaries and the Board's briefings, written by code (F17 slice 6). Mirrors
// src/agentzaaheen/core/api/summary.py and summaries.py.
const day = z.iso.date();
export const Unread = z.strictObject({ title: z.string(), reason: code });
export const SourceFact = z.strictObject({
  card: z.string(),
  title: z.string(),
  metrics: z.array(Metric),
  flagged: z.array(Entry),
});
export const Change = z.strictObject({ title: z.string(), label: z.string(), before: Value, after: Value });
export const DailySummary = z.strictObject({
  version: z.literal(1),
  workspace: z.string(),
  name: z.string(),
  day,
  made_at: time,
  window_start: time,
  health: Health,
  unread: z.array(Unread),
  sources: z.array(SourceFact),
  sample_left_out: z.array(z.string()),
  changes: z.array(Change),
  compared_with: day.nullable(),
  alerts: z
    .strictObject({ opened: z.int().nonnegative(), closed: z.int().nonnegative(), counted_since: time })
    .nullable(),
  agents: z
    .strictObject({
      agents: z.int().nonnegative(),
      on: z.int().nonnegative(),
      workspace_on: z.boolean(),
      spent_minor: z.int().nonnegative(),
      cap_minor: z.int().nonnegative().nullable(),
    })
    .nullable(),
  scout: z
    .strictObject({
      state: z.enum(["running", "done", "failed"]),
      at: time,
      recommended: z.array(z.string()),
      reason: code.nullable(),
    })
    .nullable(),
});
export type DailySummary = z.infer<typeof DailySummary>;
export const SummaryLine = z.strictObject({ day, made_at: time, health: Health, line: z.string().nullable() });
export type SummaryLine = z.infer<typeof SummaryLine>;
export const BoardSummaries = z.strictObject({
  connected: z.boolean(),
  workspaces: z.record(z.string(), SummaryLine),
});
export type BoardSummaries = z.infer<typeof BoardSummaries>;
export const SummaryDay = z.strictObject({
  day,
  made_at: time,
  line: z.string().nullable(),
  summary: DailySummary.nullable(),
});
export type SummaryDay = z.infer<typeof SummaryDay>;
export const WorkspaceSummaries = z.strictObject({ connected: z.boolean(), days: z.array(SummaryDay) });
export type WorkspaceSummaries = z.infer<typeof WorkspaceSummaries>;
export const Briefing = z.strictObject({
  day,
  made_at: time,
  title: z.string(),
  message: z.string(),
  state: z.enum(["waiting", "sent", "not_sent"]),
  sent_at: time.nullable(),
  reason: code.nullable(),
});
export type Briefing = z.infer<typeof Briefing>;
export const Briefings = z.strictObject({ connected: z.boolean(), briefings: z.array(Briefing) });
export type Briefings = z.infer<typeof Briefings>;
const Made = z.strictObject({ made: z.int().nonnegative(), failed: z.int().nonnegative() });

// The kept log lines (ADR-034 slice 3). Mirrors src/agentzaaheen/core/api/logs.py. Only our own
// sentences and plain values arrive: codes, names, counts and durations.
export const LogLevel = z.enum(["info", "warning", "error"]);
export type LogLevel = z.infer<typeof LogLevel>;
export const LogLine = z.strictObject({
  timestamp: z.string().nullable(),
  level: LogLevel,
  event: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
  component: z.string().nullable(),
  what: z.string().nullable(),
  why: z.string().nullable(),
  next: z.string().nullable(),
  trace_id: z.string().regex(/^[0-9a-f]{32}$/).nullable(),
  fields: z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])),
});
export type LogLine = z.infer<typeof LogLine>;
export const LogsPage = z.strictObject({ kept: z.boolean(), lines: z.array(LogLine), more: z.boolean(), cut: z.boolean() });
export type LogsPage = z.infer<typeof LogsPage>;
export type LogFilters = { level: LogLevel; days: number; project?: string | undefined; trace?: string | undefined };

function logsPath(filters: LogFilters): string {
  const query = new URLSearchParams({ level: filters.level, days: String(filters.days) });
  if (filters.project) query.set("project", filters.project);
  if (filters.trace) query.set("trace", filters.trace);
  return `/api/logs?${query.toString()}`;
}

// The Chairman's passkey (ADR-SEC-007): whether it exists, and whether it may be set up on this run.
export const PasskeyStatus = z.strictObject({ enrolled: z.boolean(), enrolment_open: z.boolean() });
export type PasskeyStatus = z.infer<typeof PasskeyStatus>;

// The Claude API key (ADR-033): only whether it is saved and when, never the key itself.
export const ClaudeKeyStatus = z.strictObject({
  switches: z.boolean(),
  vault: z.boolean(),
  saved: z.boolean(),
  saved_at: z.string().nullable(),
});
export type ClaudeKeyStatus = z.infer<typeof ClaudeKeyStatus>;

// WebAuthn options as JSON, handed straight to the browser's own parser (it checks them).
const PasskeyOptions = z.strictObject({ options: z.record(z.string(), z.unknown()) });

export class ApiError extends Error {
  constructor(readonly path: string, readonly status: number | null, readonly code: string | null = null) {
    super(status === null ? `${path}: the answer did not match the contract` : `${path}: HTTP ${status}`);
  }
}

// The server's error code ("chat.busy", "net.timeout"), if it sent one.
async function errorCode(response: Response): Promise<string | null> {
  try {
    const parsed = z.object({ detail: code }).safeParse(await response.json());
    return parsed.success ? parsed.data.detail : null;
  } catch {
    return null;
  }
}

// Every POST: the custom header and JSON body mean the server only answers our
// own page (another site's request is refused).
async function postJson<T>(path: string, body: unknown, schema: z.ZodType<T>): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-AgentZaaheen-Request": "1" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(path, response.status, await errorCode(response));
  const parsed = schema.safeParse(await response.json());
  if (!parsed.success) throw new ApiError(path, null);
  return parsed.data;
}

// A question for the chat. The address chooses the workspace (ADR-039); with none, it is the Board's chat.
function askChat(workspace: string | null, modelId: string, question: string): Promise<ChatAnswer> {
  const path = workspace === null ? "/api/chat" : `/api/workspaces/${encodeURIComponent(workspace)}/chat`;
  return postJson(path, { model_id: modelId, question }, ChatAnswer);
}

async function getJson<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new ApiError(path, response.status);
  const parsed = schema.safeParse(await response.json());
  if (!parsed.success) throw new ApiError(path, null);
  return parsed.data;
}

const workspacePath = (slug: string) => `/api/workspaces/${encodeURIComponent(slug)}`;
const agentPath = (slug: string, agentId: string) => `${workspacePath(slug)}/agents/${encodeURIComponent(agentId)}`;
const archivedPath = (slug: string) => `/api/archived/${encodeURIComponent(slug)}`;

export const api = {
  projects: () => getJson("/api/workspaces", z.array(ProjectSummary)),
  project: (id: string) => getJson(`/api/workspaces/${encodeURIComponent(id)}`, ProjectDetail),
  models: async () => (await getJson("/api/models", ModelList)).models,
  registry: () => getJson("/api/registry", RegistryPage),
  // The registry switches (ADR-030): each answers with the page as it now stands. Nothing here switches on.
  switchOff: (kind: Kind, id: string) => postJson("/api/registry/switch-off", { kind, id }, RegistryPage),
  requestOn: (kind: Kind, id: string) => postJson("/api/registry/requests", { kind, id }, RegistryPage),
  withdraw: (requestId: string) =>
    postJson(`/api/registry/requests/${encodeURIComponent(requestId)}/withdraw`, {}, RegistryPage),
  refuse: (requestId: string) =>
    postJson(`/api/registry/requests/${encodeURIComponent(requestId)}/refuse`, {}, RegistryPage),
  // Approving: options for one request, Windows Hello signs, the server checks the signature.
  approveOptions: async (requestId: string) =>
    (await postJson(`/api/registry/requests/${encodeURIComponent(requestId)}/approve/options`, {}, PasskeyOptions))
      .options,
  approve: (requestId: string, credential: unknown) =>
    postJson(`/api/registry/requests/${encodeURIComponent(requestId)}/approve`, { credential }, RegistryPage),
  passkey: () => getJson("/api/passkey", PasskeyStatus),
  roles: () => getJson("/api/roles", z.array(RoleChoice)),
  agents: (slug: string) => getJson(`${workspacePath(slug)}/agents`, WorkspaceAgents),
  giveAgent: (slug: string, roleId: string) =>
    postJson(`${workspacePath(slug)}/agents`, { role_id: roleId }, WorkspaceAgents),
  setAgent: (slug: string, agentId: string, change: SwitchChange, credential?: unknown) =>
    postJson(agentPath(slug, agentId), { ...change, credential }, WorkspaceAgents),
  agentOptions: async (slug: string, agentId: string, change: SwitchChange) =>
    (await postJson(`${agentPath(slug, agentId)}/options`, change, PasskeyOptions)).options,
  setWorkspace: (slug: string, change: SwitchChange, credential?: unknown) =>
    postJson(`${workspacePath(slug)}/settings`, { ...change, credential }, WorkspaceAgents),
  workspaceOptions: async (slug: string, change: SwitchChange) =>
    (await postJson(`${workspacePath(slug)}/settings/options`, change, PasskeyOptions)).options,
  // Removing a project: each answers `workspaces.needs_passkey` until Windows Hello signs that exact change.
  archive: (slug: string, credential?: unknown) =>
    postJson(`${workspacePath(slug)}/archive`, { credential }, ArchiveResult),
  archiveOptions: async (slug: string) =>
    (await postJson(`${workspacePath(slug)}/archive/options`, {}, PasskeyOptions)).options,
  archived: () => getJson("/api/archived", ArchivedList),
  restore: (slug: string, credential?: unknown) =>
    postJson(`${archivedPath(slug)}/restore`, { credential }, ArchivedList),
  restoreOptions: async (slug: string) =>
    (await postJson(`${archivedPath(slug)}/restore/options`, {}, PasskeyOptions)).options,
  erase: (slug: string, credential?: unknown) => postJson(`${archivedPath(slug)}/erase`, { credential }, ArchivedList),
  eraseOptions: async (slug: string) =>
    (await postJson(`${archivedPath(slug)}/erase/options`, {}, PasskeyOptions)).options,
  // Adding a project: no Windows Hello (nothing in a new project runs or spends).
  repos: () => getJson("/api/github/repos", RepoList),
  addProject: (name: string, repo: string | null) => postJson("/api/workspaces", { name, repo }, AddedProject),
  profile: (slug: string) => getJson(`${workspacePath(slug)}/profile`, Profile),
  // The scout only reads and advises, so asking needs no Windows Hello.
  scout: (slug: string) => getJson(`${workspacePath(slug)}/scout`, ScoutView),
  askScout: (slug: string) => postJson(`${workspacePath(slug)}/scout`, {}, ScoutView),
  // Daily summaries: read-only, and "make now" only reads and saves (nothing is sent).
  summaries: () => getJson("/api/summaries", BoardSummaries),
  workspaceSummaries: (slug: string) => getJson(`${workspacePath(slug)}/summaries`, WorkspaceSummaries),
  briefings: () => getJson("/api/briefings", Briefings),
  makeSummaries: () => postJson("/api/summaries/make", {}, Made),
  // The Logs page: read-only.
  logs: (filters: LogFilters) => getJson(logsPath(filters), LogsPage),
  // The Claude key: saving answers `claude_key.needs_passkey` until Windows Hello signs that exact save.
  claudeKey: () => getJson("/api/claude-key", ClaudeKeyStatus),
  saveClaudeKey: (key: string, credential?: unknown) =>
    postJson("/api/claude-key", { key, credential }, ClaudeKeyStatus),
  claudeKeyOptions: async (key: string) =>
    (await postJson("/api/claude-key/options", { key }, PasskeyOptions)).options,
  enrolOptions: async (code: string) =>
    (await postJson("/api/passkey/enrol/options", { code }, PasskeyOptions)).options,
  enrol: (code: string, credential: unknown) => postJson("/api/passkey/enrol", { code, credential }, PasskeyStatus),
  ask: askChat,
};
