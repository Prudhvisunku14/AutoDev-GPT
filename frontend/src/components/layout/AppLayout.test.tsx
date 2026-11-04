import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { UnauthorizedError } from "../../lib/api";
import { getToken } from "../../lib/token";
import type { Approval } from "../../lib/types";
import { makeProject } from "../../test/fixtures";
import { renderApp, stubApi } from "../../test/render";

describe("navigation and layout", () => {
  it("asks for the API token before showing any page", () => {
    renderApp({ token: null });
    expect(screen.getByRole("heading", { name: "Connect to your DevPilot backend" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /What would you like to/ })).not.toBeInTheDocument();
  });

  it("refuses a token the backend does not accept, without storing it", async () => {
    const api = stubApi({ checkToken: vi.fn(() => Promise.reject(new UnauthorizedError())) });
    renderApp({ api, token: null });
    await userEvent.type(screen.getByLabelText("API token"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText("The backend did not accept this token.")).toBeInTheDocument();
    expect(api.checkToken).toHaveBeenCalledWith("wrong");
    expect(getToken()).toBeNull();
    expect(api.listProjects).not.toHaveBeenCalled(); // no page asked anything with the wrong token
  });

  it("opens the dashboard once the token is accepted", async () => {
    renderApp({ token: null });
    await userEvent.type(screen.getByLabelText("API token"), "right");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("heading", { name: /What would you like to build today\?/ })).toBeInTheDocument();
    expect(getToken()).toBe("right");
  });

  it("marks the current page in the sidebar and navigates between pages", async () => {
    renderApp({ route: "/runs" });
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();

    await userEvent.click(within(nav).getByRole("link", { name: "Projects" }));
    expect(screen.getByRole("heading", { name: "Projects" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Projects" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();

    await userEvent.click(within(nav).getByRole("link", { name: "Templates" }));
    expect(screen.getByRole("heading", { name: "Templates" })).toBeInTheDocument();
  });

  it("goes to the composer from New Project", async () => {
    renderApp({ route: "/projects" });
    await userEvent.click(screen.getByRole("button", { name: "New Project" }));
    expect(screen.getByRole("heading", { name: /What would you like to build today\?/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Describe the application you want to build")).toHaveFocus();
  });

  it("opens and closes the mobile navigation drawer", async () => {
    renderApp({ token: null });
    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    const navigation = screen.getByRole("dialog", { name: "Navigation" });
    expect(navigation).toBeInTheDocument();
    await userEvent.click(navigation.querySelectorAll("button[aria-label='Close navigation']").item(1));
    expect(screen.queryByRole("dialog", { name: "Navigation" })).not.toBeInTheDocument();
  });

  it("fills the composer from a template", async () => {
    renderApp({ route: "/templates" });
    await userEvent.click(screen.getAllByRole("button", { name: /Use this template/ })[0] as HTMLElement);
    expect(screen.getByLabelText("Describe the application you want to build")).toHaveValue(
      "Create a Python student attendance tracker that can add students, record daily attendance and calculate attendance percentages. Use only the standard library and include unit tests.",
    );
  });

  it("lists saved projects with a link to each", async () => {
    const api = stubApi({ listProjects: vi.fn(() => Promise.resolve([makeProject({ id: "p7", name: "Habit tracker" })])) });
    renderApp({ api, route: "/projects" });
    expect(await screen.findByRole("link", { name: /Habit tracker/ })).toHaveAttribute("href", "/projects/p7");
    expect(screen.getByText("No runs")).toBeInTheDocument();
  });

  it("shows a page for an unknown address", () => {
    renderApp({ route: "/nowhere" });
    expect(screen.getByRole("heading", { name: "This page does not exist" })).toBeInTheDocument();
  });

  it("shows the notification bell only when an approval is pending", async () => {
    const approval: Approval = {
      id: "a1",
      run_id: "r1",
      kind: "TOOL_CALL",
      tool: "import_repository",
      risk: "HIGH",
      payload: {},
      details: {},
      status: "PENDING",
      response_text: null,
      created_at: "2026-11-01T10:00:00",
      decided_at: null,
    };
    const quiet = renderApp();
    await screen.findByRole("button", { name: "Run budget" });
    expect(screen.queryByRole("button", { name: /pending approval/ })).not.toBeInTheDocument();
    quiet.unmount();

    renderApp({ api: stubApi({ listApprovals: vi.fn(() => Promise.resolve([approval])) }) });
    expect(await screen.findByRole("button", { name: "1 pending approval" })).toBeInTheDocument();
  });

  it("asks for the token again when the backend stops accepting it", async () => {
    const api = stubApi({ listProjects: vi.fn(() => Promise.reject(new UnauthorizedError())) });
    renderApp({ api, route: "/projects" });
    expect(await screen.findByRole("heading", { name: "Connect to your DevPilot backend" })).toBeInTheDocument();
    await waitFor(() => expect(getToken()).toBeNull());
  });
});
