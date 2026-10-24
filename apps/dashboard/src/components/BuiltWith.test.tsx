import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RegistryPage } from "../api";
import { BuiltWith, percent, slices } from "./BuiltWith";

const READY = {
  state: "ready",
  repo: "owner/shop",
  branch: "main",
  languages: [
    { name: "TypeScript", bytes: 900, share_bp: 9000 },
    { name: "CSS", bytes: 99, share_bp: 995 },
    { name: "Ruby", bytes: 1, share_bp: 5 },
  ],
  providers: [
    { id: "github", name: "GitHub", category: "code", evidence: [".github/"] },
    {
      id: "revenuecat",
      name: "RevenueCat",
      category: "payments",
      evidence: ["apps/mobile/package.json: react-native-purchases"],
    },
  ],
  changed_at: "2026-10-24T08:00:00Z",
  checked_at: "2026-10-24T09:00:00Z",
  reason: null,
};
const EMPTY = {
  state: "not_connected",
  repo: null,
  branch: null,
  languages: [],
  providers: [],
  changed_at: null,
  checked_at: null,
  reason: null,
};

// The registry says Yaqzan reads GitHub for "shop" (and for another workspace, which must not count).
const REGISTRY = {
  connectors: [
    { id: "github", uses: [{ project_id: "shop", status: "live" }] },
    { id: "revenuecat", uses: [{ project_id: "other", status: "live" }] },
  ],
} as unknown as RegistryPage;

function serve(status: number, body: unknown) {
  const fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status })));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("what a workspace is built with", () => {
  it("asks only for this workspace's profile", async () => {
    const fetch = serve(200, READY);
    render(<BuiltWith slug="shop" registry={null} />);
    await screen.findByText("TypeScript");
    expect(fetch).toHaveBeenCalledWith("/api/workspaces/shop/profile", expect.anything());
  });

  it("shows the languages like GitHub's bar, with shares", async () => {
    serve(200, READY);
    render(<BuiltWith slug="shop" registry={null} />);
    const bar = await screen.findByRole("img");
    expect(bar.getAttribute("aria-label")).toBe("TypeScript 90.0%, CSS 10.0%, Ruby <0.1%");
    expect(screen.getByText("90.0%")).toBeTruthy();
  });

  it("groups providers, gives the evidence, and says which ones Yaqzan watches", async () => {
    serve(200, READY);
    render(<BuiltWith slug="shop" registry={REGISTRY} />);
    const payments = (await screen.findByText("Payments")).parentElement as HTMLElement;
    expect(within(payments).getByText("RevenueCat")).toBeTruthy();
    expect(within(payments).getByText("Not watched yet")).toBeTruthy(); // watched for another workspace only
    expect(within(payments).getByText("Found in apps/mobile/package.json: react-native-purchases")).toBeTruthy();
    const code = screen.getByText("Code and builds").parentElement as HTMLElement;
    expect(within(code).getByText("Yaqzan watches it")).toBeTruthy();
    expect(screen.getByText("Outside providers (2)")).toBeTruthy();
  });

  it("names services Yaqzan watches that the code does not call, but never its own checks", async () => {
    serve(200, READY);
    const registry = {
      connectors: [
        { id: "github", name: "GitHub", category: "Code", uses: [{ project_id: "shop", status: "live" }] },
        { id: "supabase", name: "Supabase", category: "Backend", uses: [{ project_id: "shop", status: "live" }] },
        { id: "paddle", name: "Paddle", category: "Revenue", uses: [{ project_id: "shop", status: "failed" }] },
        { id: "uptime", name: "Uptime checker", category: "Health", uses: [{ project_id: "shop", status: "live" }] },
      ],
    } as unknown as RegistryPage;
    render(<BuiltWith slug="shop" registry={registry} />);
    expect(
      await screen.findByText(
        "Yaqzan also watches Supabase for this project. The code in this repository does not call it, so it is connected some other way.",
      ),
    ).toBeTruthy();
  });

  it.each([
    ["not_connected", "GitHub is not connected, so the languages and providers can't be read."],
    ["no_repo", "No code repository is connected yet."],
  ])("says plainly when it is %s", async (state, words) => {
    serve(200, { ...EMPTY, state });
    render(<BuiltWith slug="shop" registry={null} />);
    expect(await screen.findByText(words)).toBeTruthy();
  });

  it("explains a GitHub failure in words", async () => {
    serve(200, { ...EMPTY, state: "failed", reason: "source.auth" });
    render(<BuiltWith slug="shop" registry={null} />);
    expect((await screen.findByRole("alert")).textContent).toBe("GitHub refused the token. It may have expired.");
  });

  it("refuses shares that do not add up to 100%", async () => {
    serve(200, { ...READY, languages: [{ name: "Go", bytes: 1, share_bp: 5000 }] });
    render(<BuiltWith slug="shop" registry={null} />);
    expect((await screen.findByRole("alert")).textContent).toBe("Could not load what this project is built with.");
  });

  it("says when there is nothing to show yet", async () => {
    serve(200, { ...READY, languages: [], providers: [] });
    render(<BuiltWith slug="shop" registry={null} />);
    expect(await screen.findByText("GitHub has not counted any languages yet.")).toBeTruthy();
    expect(screen.getByText("No outside providers found in the code yet.")).toBeTruthy();
  });
});

describe("language slices", () => {
  it("names the largest eight and puts the rest in Other", () => {
    const many = Array.from({ length: 10 }, (_, n) => ({ name: `L${String(n)}`, bytes: 1, share_bp: 1000 }));
    const shown = slices(many);
    expect(shown.map((s) => s.name)).toEqual(["L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "Other"]);
    expect(shown.at(-1)?.share_bp).toBe(2000);
  });

  it("writes tiny shares as <0.1%", () => {
    expect([percent(0), percent(5), percent(10), percent(10_000)]).toEqual(["0.0%", "<0.1%", "0.1%", "100.0%"]);
  });
});
