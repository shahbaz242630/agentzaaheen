import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddProject } from "./AddProject";

type Route = [status: number, body: unknown];

const REPOS: Route = [
  200,
  {
    repos: [
      { repo: "owner/shop", used_by: null },
      { repo: "owner/zaaheen", used_by: "zaaheen" },
    ],
    connected: true,
  },
];

// A pretend server: each path answers the next reply queued for it.
function serve(replies: Record<string, Route[]>) {
  const fetch = vi.fn((path: string) => {
    const [status, body] = replies[path]?.shift() ?? [404, { detail: "nope" }];
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function posted(fetch: ReturnType<typeof serve>) {
  return (fetch.mock.calls as unknown as [string, RequestInit | undefined][])
    .filter(([, init]) => init?.method === "POST")
    .map(([path, init]) => ({ path, body: JSON.parse(init?.body as string) as Record<string, unknown> }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openForm(onAdded = vi.fn()) {
  render(<AddProject onAdded={onAdded} />);
  await userEvent.click(screen.getByRole("button", { name: "Add a project" }));
  return onAdded;
}

describe("adding a project", () => {
  it("adds a project with one of the token's repositories and opens it", async () => {
    const fetch = serve({ "/api/github/repos": [REPOS], "/api/workspaces": [[201, { slug: "shop", name: "Shop" }]] });
    const onAdded = await openForm();
    await userEvent.type(screen.getByLabelText("Project name"), "Shop");
    await userEvent.selectOptions(await screen.findByLabelText("GitHub repository (read-only)"), "owner/shop");
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    expect(onAdded).toHaveBeenCalledWith("shop");
    expect(posted(fetch)).toEqual([{ path: "/api/workspaces", body: { name: "Shop", repo: "owner/shop" } }]);
  });

  it("shows a repository another project reads, but it cannot be chosen", async () => {
    serve({ "/api/github/repos": [REPOS] });
    await openForm();
    const taken = await screen.findByRole<HTMLOptionElement>("option", { name: "owner/zaaheen (used by zaaheen)" });
    expect(taken.disabled).toBe(true);
    expect(screen.getByText(/add it to the token on GitHub first/)).toBeTruthy();
  });

  it("adds a project without a repository when GitHub is not connected", async () => {
    const fetch = serve({
      "/api/github/repos": [[200, { repos: [], connected: false }]],
      "/api/workspaces": [[201, { slug: "notes", name: "Notes" }]],
    });
    const onAdded = await openForm();
    expect(await screen.findByText(/GitHub is not connected on this run/)).toBeTruthy();
    expect(screen.queryByLabelText("GitHub repository (read-only)")).toBeNull();
    await userEvent.type(screen.getByLabelText("Project name"), "Notes");
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    expect(onAdded).toHaveBeenCalledWith("notes");
    expect(posted(fetch)[0]?.body).toEqual({ name: "Notes", repo: null });
  });

  it("says in words why a project was not added, and stays open to fix it", async () => {
    serve({
      "/api/github/repos": [REPOS],
      "/api/workspaces": [[409, { detail: "workspaces.name_taken" }], [409, { detail: "workspaces.repo_not_covered" }]],
    });
    const onAdded = await openForm();
    await userEvent.type(screen.getByLabelText("Project name"), "Zaaheen");
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "A project with that name exists (or did once). Choose another name.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Add it to the token on GitHub first/);
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("still lets a project be added when the repository list cannot be read", async () => {
    serve({ "/api/github/repos": [[503, { detail: "source.unavailable" }]] });
    await openForm();
    expect(await screen.findByText(/could not be read. You can still add the project without one/)).toBeTruthy();
  });

  it("needs a name first, and Cancel closes it without asking the server to add anything", async () => {
    const fetch = serve({ "/api/github/repos": [REPOS] });
    await openForm();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Add project" }).disabled).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Add a project" })).toBeTruthy();
    expect(posted(fetch)).toEqual([]);
  });
});
