import { describe, expect, it } from "vitest";

import { ModelList, ProjectDetail, ProjectSummary, Value } from "./api";
import sample from "./contract/sample.json";

describe("the contract fixture written by the API", () => {
  it("matches every UI shape", () => {
    for (const project of sample.projects) expect(ProjectSummary.parse(project)).toEqual(project);
    for (const detail of Object.values(sample.details)) expect(ProjectDetail.parse(detail)).toEqual(detail);
    expect(ModelList.parse(sample.models)).toEqual(sample.models);
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
