import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ev, makeRun, plannedEvents } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { ActivityPanel } from "./ActivityPanel";

describe("ActivityPanel", () => {
  it("is idle without a run, with every stage pending and no progress", () => {
    renderWithProviders(<ActivityPanel run={null} events={[]} />);
    expect(screen.getByRole("heading", { name: "Live Activity" })).toBeInTheDocument();
    expect(screen.getByTestId("activity-status")).toHaveTextContent("Idle");
    expect(screen.getByTestId("activity-progress")).toHaveTextContent("—");
    for (const key of ["analyze", "retrieve", "generate", "test", "deliver"]) expect(screen.getByTestId(`stage-${key}`)).toHaveAttribute("data-status", "pending");
  });

  it("shows indeterminate progress, not a made-up percentage, while the first stage runs", () => {
    renderWithProviders(<ActivityPanel run={makeRun({ state: "ANALYZING" })} events={[]} />);
    expect(screen.getByTestId("activity-status")).toHaveTextContent("Running");
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  });

  it("shows a failed build on the stage that failed", () => {
    const run = makeRun({ state: "FAILED", status_detail: "The coding engine stopped: rate limited" });
    renderWithProviders(<ActivityPanel run={run} events={[...plannedEvents(), ev("run.failed", { previous_state: "IMPLEMENTING" }, 40)]} projectName="Tracker" />);
    expect(screen.getByTestId("activity-status")).toHaveTextContent("Failed");
    const generate = screen.getByTestId("stage-generate");
    expect(generate).toHaveAttribute("data-status", "failed");
    expect(generate).toHaveTextContent("The coding engine stopped: rate limited");
    expect(screen.getByTestId("stage-retrieve")).toHaveTextContent("Skipped");
    expect(screen.getByTestId("activity-progress")).toHaveTextContent("2 of 5 stages");
    expect(screen.getByRole("link", { name: /View run/ })).toHaveAttribute("href", "/runs/r1-0000-aaaa");
  });

  it("says each status once to a screen reader", () => {
    renderWithProviders(<ActivityPanel run={makeRun({ state: "IMPLEMENTING" })} events={plannedEvents()} />);
    const corner = screen.getByTestId("stage-generate").querySelector("span.shrink-0");
    expect(corner?.textContent).toBe("Running");
  });
});
