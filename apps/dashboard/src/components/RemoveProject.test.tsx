import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArchivedProjects, RemoveButton, explainer } from "./RemoveProject";

const SIGNED = { id: "cred", response: { signature: "sig" } };
const NEEDS = [409, { detail: "workspaces.needs_passkey" }] as Route;
const OPTIONS = [200, { options: { challenge: "c" } }] as Route;
const ARCHIVED = { agents_stopped: 1, keys_deleted: 2 };

type Route = [status: number, body: unknown];

// A pretend server: each path answers the next reply queued for it.
function serve(replies: Record<string, Route[]>) {
  const fetch = vi.fn((path: string) => {
    const [status, body] = replies[path]?.shift() ?? [404, { detail: "nope" }];
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function calls(fetch: ReturnType<typeof serve>) {
  return (fetch.mock.calls as unknown as [string, RequestInit | undefined][]).map(([path, init]) => ({
    path,
    body: init?.body === undefined ? undefined : (JSON.parse(init.body as string) as Record<string, unknown>),
  }));
}

// A pretend Windows Hello at localhost: it signs, or the person closes it.
function windowsHello(...outcomes: ("signs" | "cancels")[]) {
  class FakeCredential {
    toJSON() {
      return SIGNED;
    }
  }
  const credentials = {
    get: vi.fn(() =>
      (outcomes.shift() ?? "signs") === "signs"
        ? Promise.resolve(new FakeCredential())
        : Promise.reject(new DOMException("closed", "NotAllowedError")),
    ),
  };
  vi.stubGlobal(
    "PublicKeyCredential",
    Object.assign(FakeCredential, {
      parseCreationOptionsFromJSON: (o: unknown) => ({ parsed: o }),
      parseRequestOptionsFromJSON: (o: unknown) => ({ parsed: o }),
    }),
  );
  vi.stubGlobal("navigator", { credentials });
  vi.stubGlobal("location", { hostname: "localhost", port: "8765" });
  return credentials;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openPopup(onRemoved = vi.fn()) {
  render(<RemoveButton slug="eva" name="Eva" onRemoved={onRemoved} />);
  await userEvent.click(screen.getByRole("button", { name: "Remove project" }));
  return onRemoved;
}

describe("removing a project", () => {
  it("offers Archive and Delete for good, each with what it does, and Cancel changes nothing", async () => {
    const fetch = serve({});
    await openPopup();
    const dialog = screen.getByRole("dialog", { name: "Remove Eva" });
    expect(dialog).toHaveTextContent(explainer("archive", "Eva"));
    expect(dialog).toHaveTextContent(explainer("erase", "Eva"));
    expect(screen.getByRole("radio", { name: /^Archive/ })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("archiving asks Windows Hello once, then says how to bring it back", async () => {
    const credentials = windowsHello();
    const fetch = serve({
      "/api/workspaces/eva/archive": [NEEDS, [200, ARCHIVED]],
      "/api/workspaces/eva/archive/options": [OPTIONS],
    });
    const onRemoved = await openPopup();
    await userEvent.click(screen.getByRole("button", { name: "Archive Eva" }));
    await vi.waitFor(() => {
      expect(onRemoved).toHaveBeenCalledWith("Eva is archived. You can bring it back from the Board.");
    });
    expect(credentials.get).toHaveBeenCalledTimes(1);
    expect(calls(fetch)).toEqual([
      { path: "/api/workspaces/eva/archive", body: {} },
      { path: "/api/workspaces/eva/archive/options", body: {} },
      { path: "/api/workspaces/eva/archive", body: { credential: SIGNED } },
    ]);
  });

  it("deleting for good archives first, so Windows Hello asks twice", async () => {
    const credentials = windowsHello();
    const fetch = serve({
      "/api/workspaces/eva/archive": [NEEDS, [200, ARCHIVED]],
      "/api/workspaces/eva/archive/options": [OPTIONS],
      "/api/archived/eva/erase": [NEEDS, [200, { workspaces: [], switches: true }]],
      "/api/archived/eva/erase/options": [OPTIONS],
    });
    const onRemoved = await openPopup();
    await userEvent.click(screen.getByRole("radio", { name: /^Delete for good/ }));
    expect(screen.getByText("Windows Hello asks twice: once to archive, once to delete.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete Eva for good" }));
    await vi.waitFor(() => {
      expect(onRemoved).toHaveBeenCalledWith(
        "Eva was deleted for good. The audit log keeps a record that it existed.",
      );
    });
    expect(credentials.get).toHaveBeenCalledTimes(2);
    expect(calls(fetch).map((c) => c.path)).toEqual([
      "/api/workspaces/eva/archive",
      "/api/workspaces/eva/archive/options",
      "/api/workspaces/eva/archive",
      "/api/archived/eva/erase",
      "/api/archived/eva/erase/options",
      "/api/archived/eva/erase",
    ]);
  });

  it("if the second Windows Hello is closed, it says the project is archived but not deleted", async () => {
    windowsHello("signs", "cancels");
    serve({
      "/api/workspaces/eva/archive": [NEEDS, [200, ARCHIVED]],
      "/api/workspaces/eva/archive/options": [OPTIONS],
      "/api/archived/eva/erase": [NEEDS],
      "/api/archived/eva/erase/options": [OPTIONS],
    });
    const onRemoved = await openPopup();
    await userEvent.click(screen.getByRole("radio", { name: /^Delete for good/ }));
    await userEvent.click(screen.getByRole("button", { name: "Delete Eva for good" }));
    await vi.waitFor(() => {
      expect(onRemoved).toHaveBeenCalledWith(
        "Eva is archived, but it was not deleted for good: Windows Hello was closed, so nothing changed. You can delete it from the Board.",
      );
    });
  });

  it("closing the first Windows Hello changes nothing and stays open", async () => {
    windowsHello("cancels");
    serve({
      "/api/workspaces/eva/archive": [NEEDS],
      "/api/workspaces/eva/archive/options": [OPTIONS],
    });
    const onRemoved = await openPopup();
    await userEvent.click(screen.getByRole("button", { name: "Archive Eva" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Windows Hello was closed, so nothing changed.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onRemoved).not.toHaveBeenCalled();
  });
});

describe("the Board's archived projects", () => {
  const one = { workspaces: [{ slug: "eva", name: "Eva" }], switches: true };

  it("without the laptop database shows nothing", async () => {
    const fetch = serve({ "/api/archived": [[200, { workspaces: [], switches: false }]] });
    const { container } = render(<ArchivedProjects onChanged={vi.fn()} />);
    await vi.waitFor(() => {
      expect(fetch).toHaveBeenCalled();
    });
    expect(container).toBeEmptyDOMElement();
  });

  it("brings one back with Windows Hello, and says it comes back switched off", async () => {
    windowsHello();
    const onChanged = vi.fn();
    serve({
      "/api/archived": [[200, one]],
      "/api/archived/eva/restore": [NEEDS, [200, { workspaces: [], switches: true }]],
      "/api/archived/eva/restore/options": [OPTIONS],
    });
    render(<ArchivedProjects onChanged={onChanged} />);
    await userEvent.click(await screen.findByRole("button", { name: "Bring Eva back" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Eva is back, switched off. Reconnect its keys and switch its agents on when you are ready.",
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("deleting an archived one offers only Delete for good, signed once", async () => {
    const credentials = windowsHello();
    serve({
      "/api/archived": [[200, one], [200, { workspaces: [], switches: true }]],
      "/api/archived/eva/erase": [NEEDS, [200, { workspaces: [], switches: true }]],
      "/api/archived/eva/erase/options": [OPTIONS],
    });
    render(<ArchivedProjects onChanged={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete Eva for good" }));
    const dialog = screen.getByRole("dialog", { name: "Remove Eva" });
    expect(screen.queryByRole("radio", { name: /^Archive/ })).not.toBeInTheDocument();
    expect(dialog).not.toHaveTextContent("asks twice");
    const buttons = screen.getAllByRole("button", { name: "Delete Eva for good" });
    await userEvent.click(buttons[buttons.length - 1] as HTMLElement);
    expect(await screen.findByRole("status")).toHaveTextContent("Eva was deleted for good.");
    expect(credentials.get).toHaveBeenCalledTimes(1);
  });
});
