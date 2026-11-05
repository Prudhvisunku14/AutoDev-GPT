import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/api";
import type { Capabilities } from "../lib/types";
import { makeProject, makeRun } from "../test/fixtures";
import { DEFAULT_BUDGET, DEFAULT_CAPABILITIES, renderWithProviders, stubApi } from "../test/render";
import { Composer } from "./Composer";

const PROMPT = "Create a Python attendance tracker with unit tests.";
const BOX = "Describe the application you want to build";
const APPROVE = /I approve spending up to/;

const ENGINE_DETAIL = "OPENCODE_ENABLED is false; CODING_LIVE_APPROVED is false; OPENCODE_MANIFEST_FILE is not set";
const NO_ENGINE: Capabilities = {
  ...DEFAULT_CAPABILITIES,
  coding: { ready: false, label: "No coding engine", detail: ENGINE_DETAIL, simulated: false },
  can_build: false,
  problems: [`the coding engine: ${ENGINE_DETAIL}`], // the backend words each problem this way
};

async function openDialog(prompt = PROMPT) {
  await userEvent.type(screen.getByLabelText(BOX), prompt);
  await userEvent.click(await screen.findByRole("button", { name: "Build Application" }));
  return screen.findByRole("dialog", { name: "Review build spending" });
}

describe("Composer", () => {
  it("asks for a description instead of opening the spending approval", async () => {
    const api = stubApi();
    renderWithProviders(<Composer />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Build Application" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Describe the application you want to build.");
    expect(screen.queryByRole("dialog", { name: "Review build spending" })).not.toBeInTheDocument();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("asks for more detail when the prompt is too short, and clears the error on typing", async () => {
    renderWithProviders(<Composer />);
    const box = screen.getByLabelText(BOX);
    await userEvent.type(box, "todo app");
    await userEvent.click(await screen.findByRole("button", { name: "Build Application" }));
    expect(screen.getByRole("alert")).toHaveTextContent("at least 15 characters");
    await userEvent.type(box, " with tags");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows no technology-stack section and no technical paragraphs, only a short status", async () => {
    renderWithProviders(<Composer />);
    const status = await screen.findByText("Ready to build");
    expect(status).toBeInTheDocument();
    expect(screen.queryByText(/technology stack/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /FastAPI|React|Static/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/OPENCODE|Docker|verify-fastapi|recipe/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Advanced details")).not.toBeInTheDocument(); // nothing to explain when ready
  });

  it("says setup is required in one line, and keeps the diagnostics in a folded advanced area", async () => {
    renderWithProviders(<Composer />, { api: stubApi({ getCapabilities: async () => NO_ENGINE }) });
    expect(await screen.findByText("Setup required — Coding engine is not configured")).toBeInTheDocument();
    const details = screen.getByText("Advanced details").closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(within(details as HTMLElement).getByText(/OPENCODE_ENABLED is false/)).toBeInTheDocument();
  });

  it("guesses no stack from keywords: the request goes to GPT, which chooses from the stacks this backend can verify", async () => {
    const api = stubApi();
    renderWithProviders(<Composer />, { api });
    const dialog = await openDialog("Create a React dashboard for my sales data");
    expect(screen.queryByText(/not available on this backend/)).not.toBeInTheDocument();
    expect(within(dialog).getByText("Chosen by GPT from: Python command-line application")).toBeInTheDocument();
  });

  it("pauses before spending when the request names a repository it cannot read, and starts once the person decides", async () => {
    const project = makeProject({ id: "p7", stack: "python-cli" });
    const run = makeRun({ id: "r7", project_id: "p7" });
    const needs = {
      resources: [{ kind: "github" as const, key: "github:acme/secret@", url: "https://github.com/acme/secret", label: "acme/secret", full_name: "acme/secret",
                    status: "authorization_required", message: "It may be private, or the name may be wrong.", action: "connect_github" as const }],
      problems: [],
      needs_permission: ["github:acme/secret@"],
    };
    const api = stubApi({
      createProject: vi.fn(() => Promise.resolve(project)),
      createRun: vi.fn(() => Promise.resolve(run)),
      startRun: vi.fn(() => Promise.resolve(makeRun({ ...run, state: "ANALYZING" }))),
      checkResources: vi.fn(() => Promise.resolve(needs)),
    });
    const onCreated = vi.fn();
    renderWithProviders(<Composer onCreated={onCreated} />, { api });
    await openDialog("Create a Python tool that improves https://github.com/acme/secret");
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    await userEvent.click(screen.getByRole("button", { name: "Approve and start build" }));
    expect(await screen.findByRole("dialog", { name: "Permission needed" })).toBeInTheDocument();
    expect(api.checkResources).toHaveBeenCalledWith("p7");
    expect(api.startRun).not.toHaveBeenCalled(); // nothing is spent while the person decides
    await userEvent.click(screen.getByRole("button", { name: "Build without it" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(project, run));
    expect(api.startRun).toHaveBeenCalledWith("r7", DEFAULT_BUDGET.run_limit_usd);
  });

  it("builds a weather forecast request on the Python stack after explicit approval", async () => {
    const project = makeProject({ id: "p9", stack: "python-cli" });
    const run = makeRun({ id: "r9", project_id: "p9" });
    const api = stubApi({
      createProject: vi.fn(() => Promise.resolve(project)),
      createRun: vi.fn(() => Promise.resolve(run)),
      startRun: vi.fn(() => Promise.resolve(makeRun({ ...run, state: "ANALYZING" }))),
    });
    const onCreated = vi.fn();
    renderWithProviders(<Composer onCreated={onCreated} />, { api });
    const dialog = await openDialog("Create a weather forecast application");

    expect(within(dialog).getByText("Create a weather forecast application")).toBeInTheDocument();
    expect(within(dialog).getByText("$2.00")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve and start build" })).toBeDisabled();
    expect(api.createProject).not.toHaveBeenCalled();

    const checkbox = screen.getByRole("checkbox", { name: APPROVE });
    await userEvent.click(checkbox);
    expect(checkbox).toBeChecked();
    await userEvent.click(checkbox);
    expect(checkbox).not.toBeChecked();
    await userEvent.click(checkbox);
    await userEvent.click(screen.getByRole("button", { name: "Approve and start build" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(project, run));
    expect(api.createProject).toHaveBeenCalledWith({ name: "Create a weather forecast application", requirement_text: "Create a weather forecast application" }); // no stack: GPT chooses
    expect(api.createRun).toHaveBeenCalledWith("p9");
    expect(api.startRun).toHaveBeenCalledWith("r9", 2);
    expect(api.getCapabilities).toHaveBeenCalledTimes(2); // asked again at the moment of approval
    expect(api.getBudget).toHaveBeenCalledTimes(2);
  });

  it("offers every verifiable stack to GPT, a static site included, and sends the request as written", async () => {
    const SITE_READY: Capabilities = {
      ...DEFAULT_CAPABILITIES,
      stacks: [...DEFAULT_CAPABILITIES.stacks, { id: "static-html", name: "Static website", language: "html", frameworks: [], description: "HTML, CSS and JavaScript.", verifiable: true, reason: "verified in the self-tested image" }],
    };
    const project = makeProject({ id: "p-site", stack: "static-html" });
    const run = makeRun({ id: "r-site", project_id: "p-site" });
    const api = stubApi({
      getCapabilities: async () => SITE_READY,
      createProject: vi.fn(() => Promise.resolve(project)),
      createRun: vi.fn(() => Promise.resolve(run)),
      startRun: vi.fn(() => Promise.resolve(makeRun({ ...run, state: "ANALYZING" }))),
    });
    renderWithProviders(<Composer />, { api });
    const dialog = await openDialog("Create a homepage for my bakery with opening hours");
    expect(within(dialog).getByText("Chosen by GPT from: Python command-line application, Static website")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    await userEvent.click(screen.getByRole("button", { name: "Approve and start build" }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalled());
    expect(vi.mocked(api.createProject).mock.calls[0]?.[0]).toEqual({ name: "Create a homepage for my bakery with opening hours", requirement_text: "Create a homepage for my bakery with opening hours" });
  });

  it("blocks the start, with every stack's reason, when this backend can verify no kind of application", async () => {
    const NONE: Capabilities = { ...DEFAULT_CAPABILITIES, stacks: DEFAULT_CAPABILITIES.stacks.map((s) => ({ ...s, verifiable: false })) };
    const api = stubApi({ getCapabilities: async () => NONE });
    renderWithProviders(<Composer />, { api });
    const dialog = await openDialog("Create a homepage for my bakery with opening hours");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Setup required — no kind of application can be verified yet");
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    expect(screen.getByRole("button", { name: "Approve and start build" })).toBeDisabled();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("shows only the application, the limit, the approval and two buttons in the dialog", async () => {
    renderWithProviders(<Composer />);
    const dialog = await openDialog();
    expect(within(dialog).getByText("Application")).toBeInTheDocument();
    expect(within(dialog).getByText("Maximum spending")).toBeInTheDocument();
    expect(within(dialog).getAllByRole("button").map((b) => b.textContent)).toEqual(["Cancel", "Approve and start build"]);
    expect(within(dialog).queryByText(/estimate|recorded|stack|session/i)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the checkbox usable but blocks starting, with one concise reason, while the coding engine is unavailable", async () => {
    const api = stubApi({ getCapabilities: async () => NO_ENGINE });
    renderWithProviders(<Composer />, { api });
    const dialog = await openDialog();

    const checkbox = screen.getByRole("checkbox", { name: APPROVE });
    expect(checkbox).toBeEnabled();
    await userEvent.click(checkbox);
    expect(checkbox).toBeChecked();
    expect(screen.getByRole("button", { name: "Approve and start build" })).toBeDisabled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Setup required — Coding engine is not configured");
    expect(within(dialog).queryByText(/OPENCODE_ENABLED/)).not.toBeInTheDocument(); // no environment names for ordinary users
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("rechecks backend readiness at approval and creates nothing if it changed", async () => {
    const api = stubApi({ getCapabilities: vi.fn().mockResolvedValueOnce(DEFAULT_CAPABILITIES).mockResolvedValueOnce(NO_ENGINE) });
    renderWithProviders(<Composer />, { api });
    await openDialog();
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    await userEvent.click(screen.getByRole("button", { name: "Approve and start build" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Setup required — Coding engine is not configured");
    expect(api.createProject).not.toHaveBeenCalled();
    expect(api.createRun).not.toHaveBeenCalled();
    expect(api.startRun).not.toHaveBeenCalled();
  });

  it("requires renewed approval if the backend spending limit changes during review", async () => {
    const api = stubApi({ getBudget: vi.fn().mockResolvedValueOnce(DEFAULT_BUDGET).mockResolvedValueOnce({ ...DEFAULT_BUDGET, run_limit_usd: 3 }) });
    renderWithProviders(<Composer />, { api });
    await openDialog();
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    await userEvent.click(screen.getByRole("button", { name: "Approve and start build" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The spending limit changed");
    expect(screen.getByRole("checkbox", { name: /\$3.00 USD/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Approve and start build" })).toBeDisabled();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("shows a loading state and cannot create two builds with a double click", async () => {
    const project = makeProject({ id: "p-duplicate", stack: "python-cli" });
    let resolveProject!: (value: typeof project) => void;
    const pendingProject = new Promise<typeof project>((resolve) => { resolveProject = resolve; });
    const api = stubApi({ createProject: vi.fn(() => pendingProject), createRun: vi.fn(() => Promise.resolve(makeRun({ id: "r-d", project_id: "p-duplicate" }))) });
    const onCreated = vi.fn();
    renderWithProviders(<Composer onCreated={onCreated} />, { api });
    await openDialog();
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    const approve = screen.getByRole("button", { name: "Approve and start build" });

    act(() => {
      fireEvent.click(approve);
      fireEvent.click(approve);
    });
    await waitFor(() => expect(api.createProject).toHaveBeenCalledTimes(1));
    expect(approve).toBeDisabled();
    expect(approve).toHaveAttribute("aria-busy", "true");
    await act(async () => { resolveProject(project); });
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(api.createRun).toHaveBeenCalledTimes(1);
    expect(api.startRun).toHaveBeenCalledTimes(1);
  });

  it("closes without starting anything when the user cancels", async () => {
    const api = stubApi();
    renderWithProviders(<Composer />, { api });
    await openDialog();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Review build spending" })).not.toBeInTheDocument();
    expect(api.createProject).not.toHaveBeenCalled();
    expect(api.startRun).not.toHaveBeenCalled();
  });

  it("moves focus into the dialog, keeps its controls in a footer outside the scrolling body, and cancels on Escape", async () => {
    const api = stubApi();
    renderWithProviders(<Composer />, { api });
    const dialog = await openDialog();
    expect(dialog).toHaveFocus();
    const body = screen.getByTestId("spend-body");
    for (const control of [screen.getByRole("checkbox"), screen.getByRole("button", { name: "Cancel" }), screen.getByRole("button", { name: "Approve and start build" })]) {
      expect(dialog).toContainElement(control);
      expect(body).not.toContainElement(control);
    }
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Review build spending" })).not.toBeInTheDocument();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("does not allow a start while spending prices are missing", async () => {
    const api = stubApi({ getBudget: async () => ({ ...DEFAULT_BUDGET, cost_known: false }) });
    renderWithProviders(<Composer />, { api });
    const dialog = await openDialog();
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    expect(screen.getByRole("button", { name: "Approve and start build" })).toBeDisabled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("spending prices are not set");
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("shows a project-creation error clearly and keeps the prompt", async () => {
    const api = stubApi({ createProject: vi.fn(() => Promise.reject(new ApiError(422, "/projects: stack not supported"))) });
    renderWithProviders(<Composer />, { api });
    await openDialog();
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    await userEvent.click(screen.getByRole("button", { name: "Approve and start build" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("/projects: stack not supported");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText(BOX)).toHaveValue(PROMPT);
  });

  it("surfaces a refused start with the created run so it can be resumed from its detail page", async () => {
    const project = makeProject({ id: "p2", stack: "python-cli" });
    const run = makeRun({ id: "r2", project_id: "p2" });
    const api = stubApi({
      createProject: vi.fn(() => Promise.resolve(project)),
      createRun: vi.fn(() => Promise.resolve(run)),
      startRun: vi.fn(() => Promise.reject(new ApiError(503, "not configured"))),
    });
    const onCreated = vi.fn();
    renderWithProviders(<Composer onCreated={onCreated} />, { api });
    await openDialog();
    await userEvent.click(screen.getByRole("checkbox", { name: APPROVE }));
    await userEvent.click(screen.getByRole("button", { name: "Approve and start build" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(project, run, "not configured"));
  });

  it("submits for approval with Ctrl+Enter", async () => {
    renderWithProviders(<Composer />);
    await userEvent.type(screen.getByLabelText(BOX), `${PROMPT}{Control>}{Enter}{/Control}`);
    expect(await screen.findByRole("dialog", { name: "Review build spending" })).toBeInTheDocument();
  });
});
