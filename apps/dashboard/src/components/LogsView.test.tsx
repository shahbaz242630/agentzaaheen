import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LogsPage, type LogLine, type ProjectSummary } from "../api";
import { logTime, LogsView } from "./LogsView";

const TRACE = "0123456789abcdef0123456789abcdef";
const LINE: LogLine = {
  timestamp: "2026-10-25T09:30:00Z",
  level: "warning",
  event: "uptime_round_failed",
  component: "yaqzan.uptime",
  what: "The uptime check for Zaaheen could not run.",
  why: "The network timed out after 8 seconds.",
  next: "It retries in 5 minutes; look only if it fails 3 times.",
  trace_id: TRACE,
  fields: { project: "zaaheen", duration_ms: 8000 },
};
const WORKSPACES: ProjectSummary[] = [
  {
    id: "zaaheen",
    name: "Zaaheen",
    summary: "",
    health: "ok",
    escalations: 0,
    sample: false,
  },
];

function serve(answer: (path: string) => unknown) {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) => {
      asked.push(path);
      const found = answer(path);
      if (found instanceof Response) return Promise.resolve(found);
      return Promise.resolve(
        new Response(JSON.stringify(found), { status: 200 }),
      );
    }),
  );
  return asked;
}

const page = (lines: LogLine[], extra: Partial<LogsPage> = {}): LogsPage => ({
  kept: true,
  lines,
  more: false,
  cut: false,
  ...extra,
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LogsView", () => {
  it("starts on today's warnings and errors and shows each line in plain English", async () => {
    const asked = serve(() => page([LINE]));
    render(<LogsView workspaces={WORKSPACES} />);
    const item = await screen.findByRole("listitem");
    expect(asked[0]).toBe("/api/logs?level=warning&days=1");
    expect(
      within(item).getByText("The uptime check for Zaaheen could not run."),
    ).toBeTruthy();
    expect(
      within(item).getByText("Why: The network timed out after 8 seconds."),
    ).toBeTruthy();
    expect(within(item).getByText(/^Next: It retries/)).toBeTruthy();
    expect(within(item).getByText("Warning")).toBeTruthy();
    expect(within(item).getByText("uptime_round_failed")).toBeTruthy();
    expect(within(item).getByText("duration_ms: 8000")).toBeTruthy();
  });

  it("asks again when a filter changes", async () => {
    const asked = serve(() => page([]));
    render(<LogsView workspaces={WORKSPACES} />);
    await screen.findByText("Nothing matches these filters.");
    await userEvent.selectOptions(screen.getByLabelText("Show"), "error");
    await userEvent.selectOptions(screen.getByLabelText("When"), "7");
    await userEvent.selectOptions(screen.getByLabelText("Project"), "zaaheen");
    expect(asked.at(-1)).toBe("/api/logs?level=error&days=7&project=zaaheen");
  });

  it("follows a trace: every line of it, any level, the last 30 days", async () => {
    const asked = serve(() => page([LINE]));
    render(<LogsView workspaces={WORKSPACES} />);
    await userEvent.click(
      await screen.findByRole("button", {
        name: `Show every line of trace ${TRACE}`,
      }),
    );
    expect(asked.at(-1)).toBe(`/api/logs?level=info&days=30&trace=${TRACE}`);
    expect(
      screen.getByLabelText<HTMLInputElement>("Trace ID (from an error)").value,
    ).toBe(TRACE);
    await userEvent.click(
      screen.getByRole("button", { name: "Clear the trace" }),
    );
    expect(asked.at(-1)).toBe("/api/logs?level=info&days=30");
  });

  it("refuses a trace ID that is not one, and never asks with it", async () => {
    const asked = serve(() => page([]));
    render(<LogsView workspaces={WORKSPACES} />);
    await screen.findByText("Nothing matches these filters.");
    const before = asked.length;
    await userEvent.type(
      screen.getByLabelText("Trace ID (from an error)"),
      "not-a-trace",
    );
    expect(
      screen.getByText("A trace ID is 32 letters (a–f) and digits."),
    ).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Find" }).disabled,
    ).toBe(true);
    expect(asked.length).toBe(before);
  });

  it("finds a typed trace ID", async () => {
    const asked = serve(() => page([]));
    render(<LogsView workspaces={WORKSPACES} />);
    await screen.findByText("Nothing matches these filters.");
    await userEvent.type(
      screen.getByLabelText("Trace ID (from an error)"),
      TRACE.toUpperCase(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Find" }));
    expect(asked.at(-1)).toBe(`/api/logs?level=warning&days=1&trace=${TRACE}`);
  });

  it("says how to keep logs when none are kept", async () => {
    serve(() => ({ kept: false, lines: [], more: false, cut: false }));
    render(<LogsView workspaces={WORKSPACES} />);
    expect(await screen.findByText(/Logs are not kept yet/)).toBeTruthy();
  });

  it("says when more lines matched, or a day was cut", async () => {
    serve(() => page([LINE], { more: true, cut: true }));
    render(<LogsView workspaces={WORKSPACES} />);
    expect(await screen.findByText(/Showing the newest 1 lines/)).toBeTruthy();
    expect(screen.getByText(/only its newest part was searched/)).toBeTruthy();
  });

  it("says when the logs could not be read, or the answer is not a logs page", async () => {
    serve(() => new Response("{}", { status: 503 }));
    render(<LogsView workspaces={WORKSPACES} />);
    expect((await screen.findByRole("alert")).textContent).toContain(
      "could not be read",
    );
    cleanup();
    serve(() => page([{ ...LINE, level: "debug" } as unknown as LogLine]));
    render(<LogsView workspaces={WORKSPACES} />);
    expect((await screen.findByRole("alert")).textContent).toContain(
      "could not be read",
    );
  });

  it("shows the event code when a line has no sentence, and no why or next when missing", async () => {
    serve(() =>
      page([
        {
          ...LINE,
          what: null,
          why: null,
          next: null,
          trace_id: null,
          fields: {},
        },
      ]),
    );
    render(<LogsView workspaces={WORKSPACES} />);
    const item = await screen.findByRole("listitem");
    expect(within(item).getAllByText("uptime_round_failed")).toHaveLength(2);
    expect(within(item).queryByText(/^Why:/)).toBeNull();
    expect(within(item).queryByText(/^Next:/)).toBeNull();
    expect(within(item).queryByRole("button")).toBeNull();
  });
});

describe("logTime", () => {
  it("reads a time, and says when there is none", () => {
    expect(logTime("2026-10-25T09:30:00Z")).toMatch(/25 Oct \d\d:30:00/);
    expect(logTime(null)).toBe("time unknown");
    expect(logTime("nonsense")).toBe("time unknown");
  });
});
