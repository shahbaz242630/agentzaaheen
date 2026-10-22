import { describe, expect, it } from "vitest";

import { changes, reports, type Seen } from "./alerts";
import type { Reason } from "./probe";

function seen(
  id: string,
  project: string,
  up: boolean,
  reason: Reason = up ? "ok" : "not_found",
  part: string | null = null,
): Seen {
  const projectName = project === "eva" ? "Eva" : "Sandoq Kin";
  return { id, up, reason, target: { project, projectName, url: `https://${id}.example.com`, expect: "ok", part, status: null } };
}

const NO_HOURLY = false;

describe("changes", () => {
  it("is every address whose state differs from the stored one, including first sightings", () => {
    const now = [seen("a", "eva", false), seen("b", "eva", true), seen("c", "eva", true)];
    const stored = new Map([
      ["a", true],
      ["b", true],
    ]);
    expect(changes(now, stored)).toEqual([
      { id: "a", project: "eva", up: false },
      { id: "c", project: "eva", up: true },
    ]);
  });
});

describe("reports", () => {
  it("a confirmed down: one fail report in our own words, never an address", () => {
    const now = [seen("a", "eva", false, "not_found"), seen("b", "eva", false, "no_answer")];
    const stored = new Map([
      ["a", true],
      ["b", true],
    ]);
    const list = reports(now, changes(now, stored), stored, NO_HOURLY);
    expect(list).toEqual([
      { project: "eva", down: true, message: "2 of 2 addresses down (did not answer, page not found). Details on the dashboard." },
    ]);
    expect(JSON.stringify(list)).not.toContain("example.com");
  });

  it("down the first time it is seen reports once", () => {
    const now = [seen("a", "eva", false)];
    expect(reports(now, changes(now, new Map()), new Map(), NO_HOURLY)).toHaveLength(1);
  });

  it("staying down, or a first sighting that is up, says nothing between hourly reports", () => {
    const down = [seen("a", "eva", false)];
    const stored = new Map([["a", false]]);
    expect(reports(down, changes(down, stored), stored, NO_HOURLY)).toEqual([]);
    const up = [seen("b", "eva", true)];
    expect(reports(up, changes(up, new Map()), new Map(), NO_HOURLY)).toEqual([]);
  });

  it("all back up: a success report with the count", () => {
    const now = [seen("a", "eva", true), seen("b", "eva", true)];
    const stored = new Map([
      ["a", false],
      ["b", true],
    ]);
    expect(reports(now, changes(now, stored), stored, NO_HOURLY)).toEqual([
      { project: "eva", down: false, message: "1 address answering again; 2 of 2 up now." },
    ]);
  });

  it("partly back up: still a fail report (Healthchecks does not alert again)", () => {
    const now = [seen("a", "eva", true), seen("b", "eva", false)];
    const stored = new Map([
      ["a", false],
      ["b", false],
    ]);
    expect(reports(now, changes(now, stored), stored, NO_HOURLY)).toEqual([
      { project: "eva", down: true, message: "1 address answering again; 1 of 2 up now." },
    ]);
  });

  it("hourly: every project reports its state, projects in order", () => {
    const now = [seen("s", "sandoq-kin", true), seen("a", "eva", false), seen("b", "eva", true)];
    const stored = new Map([
      ["s", true],
      ["a", false],
      ["b", true],
    ]);
    expect(reports(now, changes(now, stored), stored, true)).toEqual([
      { project: "eva", down: true, message: "1 of 2 addresses up." },
      { project: "sandoq-kin", down: false, message: "1 of 1 address up." },
    ]);
  });

  it("names the parts that went down, and those back up, when every address has one", () => {
    const stored = new Map([
      ["a", true],
      ["b", true],
      ["c", true],
    ]);
    const now = [seen("a", "eva", false, "server_error", "api"), seen("b", "eva", false, "no_answer", "sign-in"), seen("c", "eva", true, "ok", "site")];
    expect(reports(now, changes(now, stored), stored, NO_HOURLY)).toEqual([
      {
        project: "eva",
        down: true,
        message: "api, sign-in down (did not answer, server error); 2 of 3 addresses down. Details on the dashboard.",
      },
    ]);
    const before = new Map([
      ["a", false],
      ["b", false],
      ["c", true],
    ]);
    const later = [seen("a", "eva", true, "ok", "api"), seen("b", "eva", false, "no_answer", "sign-in"), seen("c", "eva", true, "ok", "site")];
    expect(reports(later, changes(later, before), before, NO_HOURLY)).toEqual([
      { project: "eva", down: true, message: "api answering again; 2 of 3 up now." },
    ]);
  });

  it("says a wrong answer in its own words (another service answering at the address)", () => {
    const stored = new Map([["a", true]]);
    const now = [seen("a", "eva", false, "wrong_answer", "api")];
    expect(reports(now, changes(now, stored), stored, NO_HOURLY)[0]?.message).toBe(
      "api down (answered as something else); 1 of 1 address down. Details on the dashboard.",
    );
  });

  it("falls back to counts when an address has no part", () => {
    const stored = new Map([
      ["a", true],
      ["b", true],
    ]);
    const now = [seen("a", "eva", false, "server_error", "api"), seen("b", "eva", false, "server_error")];
    expect(reports(now, changes(now, stored), stored, NO_HOURLY)[0]?.message).toBe(
      "2 of 2 addresses down (server error). Details on the dashboard.",
    );
  });
});
