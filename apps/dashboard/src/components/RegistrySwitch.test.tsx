import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RegistryPage } from "../api";
import sample from "../contract/sample.json";
import { CustomizeView } from "./CustomizeView";

const base = sample.registry as RegistryPage;
const page: RegistryPage = { ...base, switches: true };
const REQUEST_ID = "6f1c7a52-3b9e-4d0a-9a43-1f6f1d2b8c11";
const skillOff: RegistryPage = { ...page, skills: page.skills.map((s) => ({ ...s, state: "off" as const })) };
const waiting: RegistryPage = {
  ...skillOff,
  requests: [
    { id: REQUEST_ID, kind: "skill", item_id: "triage-failing-build", requested_at: "2026-10-20T12:00:00Z", stale: false },
  ],
};

function answer(body: unknown, status = 200) {
  const fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status })));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function sent(fetch: ReturnType<typeof answer>) {
  const [path, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
  return { path, init, body: JSON.parse(init.body as string) as unknown };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the registry switches", () => {
  it("shows the heads-up while switches are active", () => {
    render(<CustomizeView page={page} projectName={(id) => id} onChange={vi.fn()} />);
    expect(
      screen.getByText(
        "Heads up · Until the next slice adds the Approve button, anything you switch off on the Customize page stays off.",
      ),
    ).toBeInTheDocument();
  });

  it("asks before switching off, and 'Keep it on' sends nothing", async () => {
    const fetch = answer(skillOff);
    render(<CustomizeView page={page} projectName={(id) => id} onChange={vi.fn()} />);
    const paddle = screen.getByRole("article", { name: "Paddle" });
    expect(paddle).toHaveTextContent("On");
    await userEvent.click(within(paddle).getByRole("button", { name: "Switch off" }));
    expect(within(paddle).getByRole("group", { name: "Switch Paddle off?" })).toBeInTheDocument();
    await userEvent.click(within(paddle).getByRole("button", { name: "Keep it on" }));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("switches off over our own guarded POST and shows the new page", async () => {
    const fetch = answer(skillOff);
    const onChange = vi.fn();
    render(<CustomizeView page={page} projectName={(id) => id} onChange={onChange} />);
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    const triage = screen.getByRole("article", { name: "Triage failing build" });
    await userEvent.click(within(triage).getByRole("button", { name: "Switch off" }));
    await userEvent.click(within(triage).getByRole("button", { name: "Switch off" }));
    const { path, init, body } = sent(fetch);
    expect(path).toBe("/api/registry/switch-off");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "X-Yaqzan-Request": "1", "Content-Type": "application/json" });
    expect(body).toEqual({ kind: "skill", id: "triage-failing-build" });
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(skillOff);
    });
  });

  it("lists what is off under Discover with 'Ask to switch on', which files a request", async () => {
    const fetch = answer(waiting);
    const onChange = vi.fn();
    render(<CustomizeView page={skillOff} projectName={(id) => id} onChange={onChange} />);
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    expect(screen.getByText("No skills are on.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    const triage = screen.getByRole("article", { name: "Triage failing build" });
    expect(triage).toHaveTextContent("Off");
    await userEvent.click(within(triage).getByRole("button", { name: "Ask to switch on" }));
    expect(sent(fetch).path).toBe("/api/registry/requests");
    expect(sent(fetch).body).toEqual({ kind: "skill", id: "triage-failing-build" });
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(waiting);
    });
  });

  it("shows waiting requests, which can be withdrawn, and never offers to approve here", async () => {
    const fetch = answer(skillOff);
    render(<CustomizeView page={waiting} projectName={(id) => id} onChange={vi.fn()} />);
    const list = screen.getByRole("region", { name: "Waiting for approval" });
    expect(list).toHaveTextContent("Triage failing build");
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    expect(screen.getByRole("article", { name: "Triage failing build" })).toHaveTextContent(
      "Waiting for your approval",
    );
    expect(screen.queryByRole("button", { name: /approve|^switch on/i })).not.toBeInTheDocument();
    await userEvent.click(within(list).getByRole("button", { name: /Withdraw/ }));
    expect(sent(fetch).path).toBe(`/api/registry/requests/${REQUEST_ID}/withdraw`);
  });

  it("warns when a request's files changed since it was asked for", () => {
    const stale = { ...waiting, requests: waiting.requests.map((r) => ({ ...r, stale: true })) };
    render(<CustomizeView page={stale} projectName={(id) => id} onChange={vi.fn()} />);
    expect(screen.getByRole("region", { name: "Waiting for approval" })).toHaveTextContent(
      "Its files changed since you asked",
    );
  });

  it("explains a refusal in plain words and changes nothing", async () => {
    answer({ detail: "registry.already_asked" }, 409);
    const onChange = vi.fn();
    render(<CustomizeView page={skillOff} projectName={(id) => id} onChange={onChange} />);
    await userEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    await userEvent.click(screen.getByRole("button", { name: "Discover" }));
    await userEvent.click(screen.getByRole("button", { name: "Ask to switch on" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You have already asked");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("refuses an answer that does not match the contract", async () => {
    answer({ ...skillOff, switches: "yes" });
    const onChange = vi.fn();
    render(<CustomizeView page={page} projectName={(id) => id} onChange={onChange} />);
    const paddle = screen.getByRole("article", { name: "Paddle" });
    await userEvent.click(within(paddle).getByRole("button", { name: "Switch off" }));
    await userEvent.click(within(paddle).getByRole("button", { name: "Switch off" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That did not work");
    expect(onChange).not.toHaveBeenCalled();
  });
});
