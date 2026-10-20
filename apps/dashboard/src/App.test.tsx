import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { failureReply, notConnectedReply, SAMPLE_NOTE } from "./components/ChatPanel";
import sample from "./contract/sample.json";

function answer(path: string): unknown {
  if (path === "/api/projects") return sample.projects;
  if (path === "/api/models") return sample.models;
  if (path === "/api/registry") return sample.registry;
  const id = decodeURIComponent(path.replace("/api/projects/", ""));
  return (sample.details as Record<string, unknown>)[id];
}

function serve(override?: (path: string) => Response | undefined) {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) => {
      const custom = override?.(path);
      if (custom) return Promise.resolve(custom);
      return Promise.resolve(new Response(JSON.stringify(answer(path)), { status: 200 }));
    }),
  );
}

beforeEach(() => {
  serve();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the dashboard", () => {
  it("lists the projects and opens the first one", async () => {
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Projects" });
    const names = within(nav)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(names).toEqual([
      expect.stringContaining("Zaaheen"),
      expect.stringContaining("Sandoq Kin"),
      expect.stringContaining("Agent X"),
      expect.stringContaining("Eva"),
      expect.stringContaining("Family Check-in"),
    ]);
    expect(await screen.findByRole("heading", { level: 1, name: "Zaaheen" })).toBeInTheDocument();
    expect(screen.getByText(/Sample data/)).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Support email" })).toHaveTextContent("Responded23");
  });

  it("switches project when one is clicked", async () => {
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /Agent X/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Agent X" })).toBeInTheDocument();
    expect(screen.getByText("99.95%")).toBeInTheDocument();
  });

  it("shows unknown, never 0, for a source that is not connected", async () => {
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /Eva/ }));
    const revenue = await screen.findByRole("article", { name: "Revenue" });
    expect(revenue).toHaveTextContent("Not connected yet");
    expect(within(revenue).getAllByText("unknown")).toHaveLength(2);
    expect(revenue).not.toHaveTextContent(/\b0\b/);
  });

  it("says so when the server cannot be reached", async () => {
    serve(() => new Response("{}", { status: 500 }));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the projects");
  });

  it("refuses an answer that breaks the contract", async () => {
    serve((path) =>
      path === "/api/projects"
        ? new Response(JSON.stringify([{ ...sample.projects[0], email: "a@b.c" }]), { status: 200 })
        : undefined,
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the projects");
  });
});

describe("real and sample cards side by side", () => {
  it("tags sample cards and says the others are real", async () => {
    const zaaheen = sample.details.zaaheen;
    const mixed = {
      ...zaaheen,
      cards: zaaheen.cards.map((card) => (card.id === "paddle" ? { ...card, sample: false } : card)),
    };
    serve((path) =>
      path === "/api/projects/zaaheen" ? new Response(JSON.stringify(mixed), { status: 200 }) : undefined,
    );
    render(<App />);
    expect(await screen.findByText(/The others are real/)).toBeInTheDocument();
    const revenue = screen.getAllByRole("article", { name: "Revenue" });
    expect(revenue[0]).not.toHaveTextContent("Sample");
    expect(revenue[1]).toHaveTextContent("Sample");
  });
});

describe("the UI enforces the API's rules too", () => {
  const zaaheen = sample.details.zaaheen;

  it("refuses a stale card that carries a number", async () => {
    const stale = {
      ...zaaheen,
      cards: zaaheen.cards.map((card) => (card.id === "paddle" ? { ...card, freshness: "stale" } : card)),
    };
    serve((path) =>
      path === "/api/projects/zaaheen" ? new Response(JSON.stringify(stale), { status: 200 }) : undefined,
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this project");
    expect(screen.queryByText("$1,240")).not.toBeInTheDocument();
  });

  it("refuses a card that never synced but claims a last success", async () => {
    const eva = sample.details.eva;
    const odd = {
      ...eva,
      cards: eva.cards.map((card) => ({ ...card, last_success_at: "2026-10-18T11:00:00Z" })),
    };
    serve((path) => (path === "/api/projects/eva" ? new Response(JSON.stringify(odd), { status: 200 }) : undefined));
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /Eva/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this project");
  });

  it.each([
    ["a cloud default", (m: { id: string }) => ({ default: m.id === "cloud-claude" })],
    ["no default", () => ({ default: false })],
  ])("refuses a model list with %s and never pre-selects a cloud model", async (_name, change) => {
    const models = { models: sample.models.models.map((m) => ({ ...m, ...change(m) })) };
    serve((path) => (path === "/api/models" ? new Response(JSON.stringify(models), { status: 200 }) : undefined));
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "Zaaheen" });
    expect(screen.queryByRole("option", { name: /Claude/ })).not.toBeInTheDocument();
    expect(screen.getByText(/No model list yet/)).toBeInTheDocument();
  });

  it("says so when there are no projects", async () => {
    serve((path) => (path === "/api/projects" ? new Response("[]", { status: 200 }) : undefined));
    render(<App />);
    expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
  });
});

describe("the chat", () => {
  it("defaults to the local model and lists cloud ones as not connected", async () => {
    render(<App />);
    await screen.findByRole("option", { name: /Gemma/ });
    expect(screen.getByLabelText("Model")).toHaveDisplayValue(/Gemma \(local\) · default/);
    expect(screen.getByRole("option", { name: /Claude \(cloud\) · not connected/ })).toBeInTheDocument();
  });

  it("answers honestly that no model is connected yet", async () => {
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "Zaaheen" });
    await userEvent.type(screen.getByLabelText("Message"), "Summarise yesterday's actions{Enter}");
    expect(screen.getByText("Summarise yesterday's actions")).toBeInTheDocument();
    expect(screen.getByText(/so I can.t answer for real/)).toHaveTextContent("Zaaheen");
    expect(screen.getByLabelText("Message")).toHaveValue("");
  });

  it("follows the model picked in the menu", async () => {
    render(<App />);
    await screen.findByRole("option", { name: /Claude/ });
    await userEvent.selectOptions(screen.getByLabelText("Model"), "cloud-claude");
    await userEvent.type(screen.getByLabelText("Message"), "hello{Enter}");
    expect(screen.getByText(/^Claude \(cloud\) is not connected yet/)).toBeInTheDocument();
  });

  it("sends a suggestion with one click and keeps voice switched off", async () => {
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "What is escalated to me?" }));
    expect(screen.getByText("What is escalated to me?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Voice (coming later)" })).toBeDisabled();
  });

  it("ignores an empty message", async () => {
    render(<App />);
    const input = await screen.findByLabelText("Message");
    await userEvent.type(input, "   {Enter}");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.queryByText(/so I can.t answer for real/)).not.toBeInTheDocument();
  });

  it("shows text exactly as written, never as HTML", async () => {
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "Zaaheen" });
    await userEvent.type(screen.getByLabelText("Message"), "<b>bold</b>{Enter}");
    expect(screen.getByText("<b>bold</b>")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("words the reply without a model or a project", () => {
    expect(notConnectedReply(undefined, null)).toMatch(/^The model is not connected yet.*your projects/);
  });
});

describe("the chat with the local model connected", () => {
  const connected = { models: sample.models.models.map((m) => ({ ...m, available: m.id === "local-gemma" })) };

  function reply(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status });
  }

  function serveChat(chat: () => Promise<Response>): RequestInit[] {
    const calls: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string, init?: RequestInit) => {
        if (path === "/api/chat") {
          calls.push(init ?? {});
          return chat();
        }
        return Promise.resolve(reply(path === "/api/models" ? connected : answer(path)));
      }),
    );
    return calls;
  }

  async function ask(question: string) {
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "Zaaheen" });
    await screen.findByRole("option", { name: /Gemma \(local\) · default$/ });
    await userEvent.type(screen.getByLabelText("Message"), `${question}{Enter}`);
  }

  it("asks the model about the open project and shows its answer", async () => {
    const calls = serveChat(() =>
      Promise.resolve(reply({ model_id: "local-gemma", answer: "MRR is $1,240.", from_sample: false, used: [] })),
    );
    await ask("How is revenue?");
    expect(await screen.findByText("MRR is $1,240.")).toBeInTheDocument();
    expect(calls[0]?.method).toBe("POST");
    expect(calls[0]?.headers).toMatchObject({ "Content-Type": "application/json", "X-Yaqzan-Request": "1" });
    expect(JSON.parse(calls[0]?.body as string)).toEqual({
      project_id: "zaaheen",
      model_id: "local-gemma",
      question: "How is revenue?",
    });
  });

  it("says when an answer leans on sample data", async () => {
    serveChat(() =>
      Promise.resolve(reply({ model_id: "local-gemma", answer: "There are 2 escalations.", from_sample: true, used: [] })),
    );
    await ask("What is escalated to me?");
    expect(await screen.findByText(/There are 2 escalations\./)).toHaveTextContent(SAMPLE_NOTE);
  });

  it("says which skill and reads an answer used", async () => {
    const used = ["Skill: triage-failing-build", "Build log: eva"];
    serveChat(() =>
      Promise.resolve(reply({ model_id: "local-gemma", answer: "A test broke.", from_sample: false, used })),
    );
    await ask("Why is Eva red?");
    expect(await screen.findByText(/A test broke\./)).toHaveTextContent(
      "Used: Skill: triage-failing-build · Build log: eva",
    );
  });

  it("shows a placeholder and blocks a second question while the model thinks", async () => {
    let finish: (response: Response) => void = () => undefined;
    serveChat(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    await ask("How is revenue?");
    expect(screen.getByText("Thinking…")).toHaveAttribute("aria-busy", "true");
    await userEvent.type(screen.getByLabelText("Message"), "again");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    finish(reply({ model_id: "local-gemma", answer: "Done.", from_sample: false, used: [] }));
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it.each([
    ["net.unavailable", 502, /model server isn't running/],
    ["net.timeout", 502, /took too long/],
    ["chat.busy", 429, /still answering the last question/],
    ["chat.not_connected", 503, /so I can.t answer for real/],
    ["net.bad_response", 502, /went wrong asking the model \(net\.bad_response\)\. Nothing was changed\./],
  ])("explains %s in words", async (code, status, words) => {
    serveChat(() => Promise.resolve(reply({ detail: code }, status)));
    await ask("How is revenue?");
    expect(await screen.findByText(words)).toBeInTheDocument();
  });

  it("refuses an answer that does not match the contract", async () => {
    serveChat(() => Promise.resolve(reply({ answer: "<script>" })));
    await ask("How is revenue?");
    expect(await screen.findByText(/^Something went wrong asking the model\. Nothing was changed\.$/)).toBeInTheDocument();
  });

  it("explains an error with no code", () => {
    expect(failureReply(new Error("boom"), undefined, null)).toBe(
      "Something went wrong asking the model. Nothing was changed.",
    );
  });
});
