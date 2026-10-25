import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Briefing, DailySummary, SummaryDay } from "../api";
import { boardLine, BriefingsPanel, DailySummaryPanel, dayWords, reasonWords } from "./Summaries";

const SUMMARIES = "/api/workspaces/sage/summaries";
const NOW = "2026-10-25T01:00:00Z";

const SUMMARY: DailySummary = {
  version: 1,
  workspace: "sage",
  name: "Sage",
  day: "2026-10-25",
  made_at: NOW,
  window_start: "2026-10-24T01:00:00Z",
  health: "alert",
  unread: [{ title: "Revenue", reason: "source.auth" }],
  sources: [
    {
      card: "code",
      title: "Code",
      metrics: [{ label: "Open issues", value: { kind: "count", value: 4 }, detail: null }],
      flagged: [{ text: "Newest build on main failed: 1 workflow (abc1234)", tone: "alert", at: null }],
    },
  ],
  sample_left_out: ["Support email"],
  changes: [
    {
      title: "Revenue",
      label: "MRR",
      before: { kind: "money", minor: 10000, currency: "USD" },
      after: { kind: "money", minor: 12000, currency: "USD" },
    },
  ],
  compared_with: "2026-10-24",
  alerts: { opened: 2, closed: 1, counted_since: "2026-10-24T20:00:00Z" },
  agents: { agents: 3, on: 1, workspace_on: true, spent_minor: 40, cap_minor: 500 },
  scout: { state: "done", at: NOW, recommended: ["monitoring", "security"], reason: null },
};
const DAY: SummaryDay = { day: "2026-10-25", made_at: NOW, line: "Could not read Revenue; Needs you now.", summary: SUMMARY };
const BRIEFING: Briefing = {
  day: "2026-10-25",
  made_at: "2026-10-25T03:00:00Z",
  title: "Yaqzan briefing, Sun 25 Oct",
  message: "2 projects; 1 need a look.\nSage: Needs you now.\nDetails on the dashboard.",
  state: "sent",
  sent_at: "2026-10-25T03:00:02Z",
  reason: null,
};

function serve(answers: Record<string, unknown>, posts: string[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(path);
      const found = answers[path];
      if (found instanceof Response) return Promise.resolve(found);
      return Promise.resolve(new Response(JSON.stringify(found), { status: found === undefined ? 404 : 200 }));
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the Board card's summary line", () => {
  it("says plainly when there is no database, none yet, or it could not be read", () => {
    expect(boardLine(undefined, "sage")).toBe("Daily summary: …");
    expect(boardLine("failed", "sage")).toBe("Daily summary: could not be read");
    expect(boardLine({ connected: false, workspaces: {} }, "sage")).toBe("Daily summary: not set up yet");
    expect(boardLine({ connected: true, workspaces: {} }, "sage")).toBe("Daily summary: none yet (made at 05:00)");
  });

  it("shows the day and the line code wrote, or that the saved one is unreadable", () => {
    const line = { day: "2026-10-25", made_at: NOW, health: "ok" as const, line: "All fine." };
    expect(boardLine({ connected: true, workspaces: { sage: line } }, "sage")).toBe("Sun 25 Oct: All fine.");
    expect(boardLine({ connected: true, workspaces: { sage: { ...line, line: null } } }, "sage")).toBe(
      "Sun 25 Oct: the saved summary could not be read",
    );
  });

  it("words days and reasons in plain English", () => {
    expect(dayWords("2026-03-29")).toBe("Sun 29 Mar");
    expect(reasonWords("source.auth")).toBe("The service refused our key; it may have expired");
    expect(reasonWords("something.new")).toBe("Cannot be worked out right now");
  });
});

describe("a workspace's daily summary", () => {
  it("puts what could not be read first, then the facts, changes, alerts, agents and the scout", async () => {
    serve({ [SUMMARIES]: { connected: true, days: [DAY, { ...DAY, day: "2026-10-24", line: "All fine." }] } });
    render(<DailySummaryPanel slug="sage" />);
    const panel = await screen.findByRole("region", { name: "Daily summary" });
    expect(await within(panel).findByText("Could not read Revenue; Needs you now.")).toBeInTheDocument();
    const unread = within(panel).getByRole("heading", { name: "Could not be read" });
    const facts = within(panel).getByText("Code");
    expect(unread.compareDocumentPosition(facts) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(panel).getByText("Revenue: The service refused our key; it may have expired.")).toBeInTheDocument();
    expect(within(panel).getByText("· Open issues 4")).toBeInTheDocument();
    expect(within(panel).getByText(/Newest build on main failed/)).toBeInTheDocument();
    expect(within(panel).getByText("Revenue MRR: $100 → $120")).toBeInTheDocument();
    expect(within(panel).getByText(/2 went down, 1 came back up \(counted only since the dashboard started\)/))
      .toBeInTheDocument();
    expect(within(panel).getByText("Agents: 1 of 3 on · spent $0.40 · cap $5.00")).toBeInTheDocument();
    expect(within(panel).getByText("The scout recommends 2 roles: monitoring, security.")).toBeInTheDocument();
    expect(within(panel).getByText("Left out (sample data, not real): Support email.")).toBeInTheDocument();
    expect(within(panel).getByText("Sat 24 Oct:")).toBeInTheDocument();
  });

  it("says none yet for a workspace with no agents, never 0 of 0", async () => {
    const none = { ...SUMMARY, agents: { agents: 0, on: 0, workspace_on: false, spent_minor: 0, cap_minor: null } };
    serve({ [SUMMARIES]: { connected: true, days: [{ ...DAY, summary: none }] } });
    render(<DailySummaryPanel slug="sage" />);
    expect(await screen.findByText("Agents: none yet")).toBeInTheDocument();
    expect(screen.queryByText(/0 of 0/)).not.toBeInTheDocument();
  });

  it("never guesses from a saved summary that no longer reads", async () => {
    serve({ [SUMMARIES]: { connected: true, days: [{ ...DAY, line: null, summary: null }] } });
    render(<DailySummaryPanel slug="sage" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be read, so nothing from it is shown");
    expect(screen.queryByText(/Needs you now/)).not.toBeInTheDocument();
  });

  it("is honest when there is none yet, no database, or it failed to load", async () => {
    serve({ [SUMMARIES]: { connected: true, days: [] } });
    render(<DailySummaryPanel slug="sage" />);
    expect(await screen.findByText("None yet. The first one is made at 05:00.")).toBeInTheDocument();
    cleanup();
    serve({ [SUMMARIES]: { connected: false, days: [] } });
    render(<DailySummaryPanel slug="sage" />);
    expect(await screen.findByText(/Not set up yet: daily summaries need the laptop database/)).toBeInTheDocument();
    cleanup();
    serve({ [SUMMARIES]: new Response("{}", { status: 503 }) });
    render(<DailySummaryPanel slug="sage" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The daily summary could not be loaded.");
  });

  it("refuses an answer that does not match the contract", async () => {
    serve({ [SUMMARIES]: { connected: true, days: [{ ...DAY, summary: { ...SUMMARY, health: "great" } }] } });
    render(<DailySummaryPanel slug="sage" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be loaded");
  });
});

describe("the morning briefings", () => {
  it("shows the exact words and whether they reached the phone", async () => {
    const failed = { ...BRIEFING, day: "2026-10-24", state: "not_sent", sent_at: null, reason: "net.timeout" };
    const kept = { ...BRIEFING, day: "2026-10-23", state: "not_sent", sent_at: null, reason: "briefing.no_topic" };
    serve({ "/api/briefings": { connected: true, briefings: [BRIEFING, failed, kept] } });
    render(<BriefingsPanel onMade={vi.fn()} />);
    const panel = await screen.findByRole("region", { name: "Morning briefings" });
    expect(await within(panel).findByText(/^Sent to your phone/)).toBeInTheDocument();
    expect(within(panel).getAllByText(/Sage: Needs you now\./)).toHaveLength(3); // each day keeps its words
    expect(within(panel).getByText(/Not sent: ntfy did not answer in time\. Yaqzan tries again/)).toBeInTheDocument();
    expect(within(panel).getByText("Kept here only: no phone (ntfy) topic is set up")).toBeInTheDocument();
  });

  it("makes today's summaries on request, sending nothing, and says how it went", async () => {
    const posts: string[] = [];
    const onMade = vi.fn();
    serve({ "/api/briefings": { connected: true, briefings: [] }, "/api/summaries/make": { made: 4, failed: 1 } }, posts);
    render(<BriefingsPanel onMade={onMade} />);
    expect(await screen.findByText("No briefing yet. The first one goes at 07:00.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Make today's summaries now" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Made 4 summaries; 1 could not be made.");
    expect(posts).toEqual(["/api/summaries/make"]);
    expect(onMade).toHaveBeenCalledOnce();
  });

  it("says so when making them fails, and hides itself without a database", async () => {
    serve({
      "/api/briefings": { connected: true, briefings: [] },
      "/api/summaries/make": new Response('{"detail":"summaries.store_unavailable"}', { status: 503 }),
    });
    render(<BriefingsPanel onMade={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Make today's summaries now" }));
    expect(await screen.findByRole("status")).toHaveTextContent("could not be made. Is the laptop database running?");
    cleanup();
    serve({ "/api/briefings": { connected: false, briefings: [] } });
    const { container } = render(<BriefingsPanel onMade={vi.fn()} />);
    await vi.waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });
});
