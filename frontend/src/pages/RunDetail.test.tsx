import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../lib/api";
import type { Report, Run, RunEvent } from "../lib/types";
import { ev, makeProject, makeReport, makeRun, plannedEvents } from "../test/fixtures";
import { renderApp, stubApi } from "../test/render";

function backend(run: Run, events: RunEvent[], report: Report | null, extra: Partial<ApiClient> = {}) {
  return stubApi({
    getRun: vi.fn(() => Promise.resolve(run)),
    getProject: vi.fn(() => Promise.resolve(makeProject())),
    getAllEvents: vi.fn(() => Promise.resolve(events)),
    getReport: vi.fn(() => Promise.resolve(report)),
    ...extra,
  });
}

describe("Run details", () => {
  it("shows a failed build plainly, with no success message and no download", async () => {
    const run = makeRun({ state: "FAILED", outcome: "FAILED", status_detail: "The coding engine stopped: the gateway refused the request", cost_usd: 0.379808 });
    const events = [...plannedEvents(), ev("coding.finished", { task_order: 1, status: "FAILED", kept: false, detail: "gateway refused" }, 60), ev("run.failed", { previous_state: "IMPLEMENTING", reason: "error" }, 61)];
    const report = makeReport({
      run: { id: run.id, state: "FAILED", outcome: "FAILED", detail: run.status_detail, turns: 9, tokens: 1000, cost_usd: 0.379808, repairs: 0 },
      verifications: [],
      acceptance: { configured: false },
      delivered_files: [],
    });
    renderApp({ api: backend(run, events, report), route: `/runs/${run.id}` });

    const delivery = await screen.findByTestId("delivery");
    expect(delivery).toHaveAttribute("data-verdict", "failed");
    expect(delivery).toHaveTextContent("The build did not produce a verified application");
    expect(screen.queryByText("Application generated successfully")).not.toBeInTheDocument();
    expect(within(delivery).getByRole("button", { name: "Download project" })).toBeDisabled();

    const notice = screen.getByRole("alert");
    expect(notice).toHaveTextContent("Failed");
    expect(notice).toHaveTextContent("the gateway refused the request");
    expect(delivery).toHaveTextContent("$0.3798");
    expect(await screen.findByText("Coding task 1: FAILED, undone")).toBeInTheDocument();
    expect(screen.getByTestId("stage-generate")).toHaveAttribute("data-status", "failed");
  });

  it("says the application was generated only when independent acceptance passed", async () => {
    const run = makeRun({ state: "COMPLETED", outcome: "SUCCESS" });
    renderApp({ api: backend(run, [...plannedEvents(), ev("run.completed", {}, 90)], makeReport()), route: `/runs/${run.id}` });
    expect(await screen.findByRole("heading", { name: "Application generated successfully" })).toBeInTheDocument();
    expect(screen.getByTestId("delivery")).toHaveTextContent("PASSED (5 of 5)");
  });

  it("does not call an unverified completed run a success", async () => {
    const run = makeRun({ state: "COMPLETED", outcome: "SUCCESS" });
    renderApp({ api: backend(run, plannedEvents(), makeReport({ acceptance: { configured: false } })), route: `/runs/${run.id}` });
    expect(await screen.findByText("Completed without independent verification")).toBeInTheDocument();
    expect(screen.queryByText("Application generated successfully")).not.toBeInTheDocument();
  });

  it("shows the report as an artifact on request", async () => {
    const run = makeRun({ state: "COMPLETED", outcome: "SUCCESS" });
    renderApp({ api: backend(run, plannedEvents(), makeReport()), route: `/runs/${run.id}` });
    await userEvent.click(await screen.findByRole("button", { name: /Show the final report/ }));
    expect(screen.getByText(/"fully_real": true/)).toBeInTheDocument();
  });

  it("offers a download only after the backend confirms a verified artifact", async () => {
    const run = makeRun({ state: "COMPLETED", outcome: "SUCCESS" });
    const downloadArtifact = vi.fn(() => Promise.reject(new Error("The archive is unavailable.")));
    const api = backend(run, plannedEvents(), makeReport(), {
      getArtifact: vi.fn(() => Promise.resolve({
        available: true,
        reason: null,
        filename: "attendance-tracker-r1-0000.zip",
        files: [{ path: "attendance.py", bytes: 1200 }],
        excluded: [],
        total_bytes: 1200,
        simulated: [],
      })),
      downloadArtifact,
    });
    renderApp({ api, route: `/runs/${run.id}` });

    const button = await screen.findByRole("button", { name: "Download project" });
    await screen.findByText("1 files and the verification report; secrets and temporary files are excluded.");
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(downloadArtifact).toHaveBeenCalledWith(run.id);
    expect(await screen.findByRole("alert")).toHaveTextContent("The archive is unavailable.");
  });

  it("labels a download that was verified by the generated tests only, and still does not call the build a success", async () => {
    const run = makeRun({ state: "COMPLETED", outcome: "SUCCESS" });
    const api = backend(run, plannedEvents(), makeReport({ acceptance: { configured: false } }), {
      getArtifact: vi.fn(() => Promise.resolve({
        available: true, reason: null, verification: "generated-tests" as const, filename: "weather-r1-0000.zip",
        files: [{ path: "weather.py", bytes: 900 }, { path: "test_weather.py", bytes: 700 }], excluded: [], total_bytes: 1600, simulated: [],
      })),
    });
    renderApp({ api, route: `/runs/${run.id}` });
    expect(await screen.findByText(/Verified by the generated tests only; no independent acceptance suite judged it\. 2 files/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download project" })).toBeEnabled(); // enabled only once the backend's answer is in
    expect(screen.getByText("Completed without independent verification")).toBeInTheDocument();
    expect(screen.queryByText("Application generated successfully")).not.toBeInTheDocument();
  });

  it("allows explicit approval but blocks a run-detail start when backend coding readiness fails", async () => {
    const run = makeRun({ state: "CREATED" });
    const api = backend(run, [], null, {
      getProject: vi.fn(() => Promise.resolve(makeProject({ stack: "python-cli" }))),
      getCapabilities: vi.fn(() => Promise.resolve({
        gpt: { ready: true, label: "GPT", detail: null, simulated: false },
        coding: { ready: false, label: "No coding engine", detail: "OPENCODE_ENABLED is false", simulated: false },
        sandbox: { ready: true, label: "DockerSandbox", detail: null, simulated: false },
        prices: { ready: true, label: "prices configured", detail: null, simulated: false },
        stacks: [{ id: "python-cli", name: "Python CLI", language: "python", frameworks: [], description: "Python 3.12 CLI.", verifiable: true, reason: "approved" }],
        can_build: false,
        problems: ["The coding engine: OPENCODE_ENABLED is false"],
        simulated: [],
        acceptance_suites: true,
        history: true,
      })),
    });
    renderApp({ api, route: `/runs/${run.id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Start build" }));
    const approval = await screen.findByRole("checkbox", { name: /I approve spending up to/ });
    expect(approval).toBeEnabled();
    await userEvent.click(approval);
    expect(approval).toBeChecked();
    expect(screen.getByRole("button", { name: "Approve and start" })).toBeDisabled();
    expect(screen.getByText("Setup required — Coding engine is not configured")).toBeInTheDocument();
    expect(screen.queryByText(/OPENCODE_ENABLED/)).not.toBeInTheDocument();
    expect(api.startRun).not.toHaveBeenCalled();
  });

  it("rechecks readiness and sends explicit spending approval when starting from run details", async () => {
    const run = makeRun({ state: "CREATED" });
    const api = backend(run, [], null, {
      getProject: vi.fn(() => Promise.resolve(makeProject({ stack: "python-cli" }))),
    });
    renderApp({ api, route: `/runs/${run.id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Start build" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: /I approve spending up to/ }));
    await userEvent.click(screen.getByRole("button", { name: "Approve and start" }));
    await waitFor(() => expect(api.startRun).toHaveBeenCalledWith(run.id, 2));
    expect(api.status).toHaveBeenCalled();
    expect(api.getCapabilities).toHaveBeenCalled();
    expect(api.getBudget).toHaveBeenCalled();
  });

  it("says when the run does not exist", async () => {
    renderApp({ route: "/runs/missing" });
    expect(await screen.findByText("Run not found")).toBeInTheDocument();
  });
});
