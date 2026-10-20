import { describe, expect, it } from "vitest";

import { ModelList, ProjectDetail, ProjectSummary, RegistryPage, Value } from "./api";
import sample from "./contract/sample.json";

describe("the contract fixture written by the API", () => {
  it("matches every UI shape", () => {
    for (const project of sample.projects) expect(ProjectSummary.parse(project)).toEqual(project);
    for (const detail of Object.values(sample.details)) expect(ProjectDetail.parse(detail)).toEqual(detail);
    expect(ModelList.parse(sample.models)).toEqual(sample.models);
    expect(RegistryPage.parse(sample.registry)).toEqual(sample.registry);
  });

  it("refuses a registry item with an unknown status, an extra field or a short fingerprint", () => {
    const [first] = sample.registry.connectors;
    const bad = [
      { ...first, uses: [{ project_id: "zaaheen", status: "on" }] },
      { ...first, secret: "x" },
      { ...first, fingerprint: "short" },
    ];
    for (const connector of bad) {
      expect(RegistryPage.safeParse({ ...sample.registry, connectors: [connector] }).success).toBe(false);
    }
  });

  it("refuses an unknown item state or a request that is not well formed", () => {
    const [first] = sample.registry.connectors;
    const request = {
      id: "6f1c7a52-3b9e-4d0a-9a43-1f6f1d2b8c11",
      kind: "skill",
      item_id: "triage-failing-build",
      requested_at: "2026-10-20T12:00:00Z",
      stale: false,
    };
    expect(RegistryPage.safeParse({ ...sample.registry, requests: [request] }).success).toBe(true);
    const bad = [
      { ...sample.registry, connectors: [{ ...first, state: "enabled" }] },
      { ...sample.registry, requests: [{ ...request, id: "1" }] },
      { ...sample.registry, requests: [{ ...request, item_id: "../x" }] },
      { ...sample.registry, switches: "yes" },
    ];
    for (const page of bad) expect(RegistryPage.safeParse(page).success).toBe(false);
  });
});

describe("values", () => {
  it.each([
    { kind: "money", minor: 1.5, currency: "USD" },
    { kind: "money", minor: 100, currency: "usd" },
    { kind: "count", value: -1 },
    { kind: "percent", basis_points: 10_001 },
    { kind: "unknown", reason: "Not Known!" },
    { kind: "count", value: 1, email: "a@b.c" },
    { kind: "float", value: 1 },
  ])("refuses %o", (bad) => {
    expect(Value.safeParse(bad).success).toBe(false);
  });
});
