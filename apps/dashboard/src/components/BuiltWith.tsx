import { useEffect, useState } from "react";

import { api, type Profile, type ProviderCategory, type RegistryPage } from "../api";
import { ago } from "../format";

type Load = { state: "loading" } | { state: "ready"; data: Profile } | { state: "failed" };

// GitHub's own language colours (linguist), so the bar reads like GitHub's. Others are grey.
const LANGUAGE_COLOUR: Record<string, string> = {
  TypeScript: "#3178c6",
  JavaScript: "#f1e05a",
  Python: "#3572a5",
  Rust: "#dea584",
  Go: "#00add8",
  Swift: "#f05138",
  Kotlin: "#a97bff",
  Java: "#b07219",
  Dart: "#00b4ab",
  Ruby: "#701516",
  PHP: "#4f5d95",
  "C#": "#178600",
  C: "#555555",
  "C++": "#f34b7d",
  "Objective-C": "#438eff",
  HTML: "#e34c26",
  CSS: "#663399",
  SCSS: "#c6538c",
  Astro: "#ff5a03",
  Vue: "#41b883",
  Svelte: "#ff3e00",
  MDX: "#fcb32c",
  Shell: "#89e051",
  PowerShell: "#012456",
  Dockerfile: "#384d54",
  PLpgSQL: "#336790",
  Bicep: "#519aba",
  HCL: "#844fba",
  Handlebars: "#f7931e",
  "Jupyter Notebook": "#da5b0b",
};
const OTHER_COLOUR = "#9ca3af";
const MAX_SHOWN = 8; // languages named in the legend; the rest are "Other"

const CATEGORY: Record<ProviderCategory, string> = {
  code: "Code and builds",
  hosting: "Hosting and servers",
  stores: "App stores",
  payments: "Payments",
  data: "Databases and storage",
  monitoring: "Errors and analytics",
  messages: "Email, texts and push",
  ai: "AI models",
  sign_in: "Sign-in",
};
const REASON: Record<string, string> = {
  "source.auth": "GitHub refused the token. It may have expired.",
  "source.refused": "GitHub refused the request. The token may not cover this repository.",
  "source.rate_limited": "GitHub asked us to slow down. Try again in a few minutes.",
  "source.unavailable": "GitHub did not answer. Try again in a moment.",
  "source.bad_response": "GitHub sent something we could not read.",
  "workspaces.store_unavailable": "The laptop database did not answer. Is it running?",
};

export function percent(basisPoints: number): string {
  return basisPoints > 0 && basisPoints < 10 ? "<0.1%" : `${(Math.round(basisPoints / 10) / 10).toFixed(1)}%`;
}

// The largest languages, then everything smaller as one "Other" slice (as GitHub's bar does).
export function slices(languages: Profile["languages"]): { name: string; share_bp: number; colour: string }[] {
  const named = languages.slice(0, MAX_SHOWN).map((l) => ({
    name: l.name,
    share_bp: l.share_bp,
    colour: LANGUAGE_COLOUR[l.name] ?? OTHER_COLOUR,
  }));
  const rest = languages.slice(MAX_SHOWN).reduce((sum, l) => sum + l.share_bp, 0);
  return rest > 0 ? [...named, { name: "Other", share_bp: rest, colour: OTHER_COLOUR }] : named;
}

interface Props {
  slug: string;
  // The Customize page's connectors: which providers Yaqzan itself reads for this workspace.
  registry: RegistryPage | null | "failed";
}

// What this project is built with (F17 slice 5b-1): its languages, and the outside providers
// code found in its repository, each with the files that show it. Checked again each time the page opens.
export function BuiltWith({ slug, registry }: Props) {
  const [loaded, setLoaded] = useState<Load>({ state: "loading" });

  useEffect(() => {
    let live = true;
    api.profile(slug).then(
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
  }, [slug]);

  return (
    <section aria-labelledby="built-with" className="flex flex-col gap-4 rounded-2xl border border-line bg-panel p-5">
      <h2 id="built-with" className="text-base font-semibold text-ink">
        Built with
      </h2>
      <Body loaded={loaded} slug={slug} registry={registry} />
    </section>
  );
}

function Body({ loaded, slug, registry }: { loaded: Load } & Props) {
  if (loaded.state === "loading") return <p className="text-sm text-muted">Reading the repository…</p>;
  if (loaded.state === "failed") {
    return (
      <p role="alert" className="text-sm text-alert">
        Could not load what this project is built with.
      </p>
    );
  }
  const profile = loaded.data;
  if (profile.state === "not_connected") {
    return <p className="text-sm text-muted">GitHub is not connected, so the languages and providers can't be read.</p>;
  }
  if (profile.state === "no_repo") return <p className="text-sm text-muted">No code repository is connected yet.</p>;
  if (profile.state === "failed") {
    return (
      <p role="alert" className="text-sm text-alert">
        {(profile.reason && REASON[profile.reason]) ?? "GitHub could not be read just now."}
      </p>
    );
  }
  // Providers Yaqzan itself reads for this workspace (its connectors), so each can say so.
  const watched = new Set(
    registry !== null && registry !== "failed"
      ? registry.connectors.filter((c) => c.uses.some((u) => u.project_id === slug && u.status === "live")).map((c) => c.id)
      : [],
  );
  return (
    <div className="flex flex-col gap-5">
      <p className="text-xs text-faint">
        {profile.repo} ({profile.branch}) · checked {profile.checked_at ? ago(profile.checked_at) : "now"}
        {profile.changed_at && ` · last change ${ago(profile.changed_at)}`}
      </p>
      <Languages languages={profile.languages} />
      <Providers providers={profile.providers} watched={watched} />
    </div>
  );
}

function Languages({ languages }: { languages: Profile["languages"] }) {
  if (languages.length === 0) return <p className="text-sm text-muted">GitHub has not counted any languages yet.</p>;
  const shown = slices(languages);
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-sm font-medium text-muted">Languages</h3>
      <div
        role="img"
        aria-label={shown.map((s) => `${s.name} ${percent(s.share_bp)}`).join(", ")}
        className="flex h-2.5 overflow-hidden rounded-full bg-raised"
      >
        {shown.map((s) => (
          <span key={s.name} style={{ width: `${String(s.share_bp / 100)}%`, backgroundColor: s.colour }} />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
        {shown.map((s) => (
          <li key={s.name} className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-full" style={{ backgroundColor: s.colour }} aria-hidden="true" />
            <span className="font-medium text-ink">{s.name}</span>
            <span className="text-muted tabular">{percent(s.share_bp)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Providers({ providers, watched }: { providers: Profile["providers"]; watched: ReadonlySet<string> }) {
  if (providers.length === 0) {
    return <p className="text-sm text-muted">No outside providers found in the code yet.</p>;
  }
  const groups = (Object.keys(CATEGORY) as ProviderCategory[])
    .map((category) => ({ category, items: providers.filter((p) => p.category === category) }))
    .filter((g) => g.items.length > 0);
  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-medium text-muted">Outside providers ({providers.length})</h3>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(16rem,1fr))] gap-4">
        {groups.map(({ category, items }) => (
          <div key={category} className="flex flex-col gap-2">
            <h4 className="text-xs font-medium tracking-wide text-faint uppercase">{CATEGORY[category]}</h4>
            <ul className="flex flex-col gap-2">
              {items.map((p) => (
                <li key={p.id} className="flex flex-col gap-0.5">
                  <span className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium text-ink">{p.name}</span>
                    {watched.has(p.id) ? (
                      <span className="rounded-full bg-ok-soft px-2 py-0.5 text-xs text-ink">Yaqzan watches it</span>
                    ) : (
                      <span className="rounded-full bg-raised px-2 py-0.5 text-xs text-muted">Not watched yet</span>
                    )}
                  </span>
                  <span className="text-xs [overflow-wrap:anywhere] text-faint">Found in {p.evidence.join(" · ")}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
