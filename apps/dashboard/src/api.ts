// The API contract, checked on arrival (Rulebook §4: zod at the API boundary).
// Mirrors src/yaqzan/core/api/views.py. Anything that does not match is refused.
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

export class ApiError extends Error {
  constructor(readonly path: string, readonly status: number | null) {
    super(status === null ? `${path}: the answer did not match the contract` : `${path}: HTTP ${status}`);
  }
}

async function getJson<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new ApiError(path, response.status);
  const parsed = schema.safeParse(await response.json());
  if (!parsed.success) throw new ApiError(path, null);
  return parsed.data;
}

export const api = {
  projects: () => getJson("/api/projects", z.array(ProjectSummary)),
  project: (id: string) => getJson(`/api/projects/${encodeURIComponent(id)}`, ProjectDetail),
  models: async () => (await getJson("/api/models", ModelList)).models,
};
