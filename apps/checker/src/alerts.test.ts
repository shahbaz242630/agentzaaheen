import { describe, expect, it } from "vitest";

import { changes, reports, type Seen } from "./alerts";
import type { Reason } from "./probe";

function seen(id: string, project: string, up: boolean, reason: Reason = up ? "ok" : "not_found"): Seen {
  const projectName = project === "eva" ? "Eva" : "Sandoq Kin";
  return { id, up, reason, target: { project, projectName, url: `https://${id}.example.com`, expect: "ok" } };
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
});
