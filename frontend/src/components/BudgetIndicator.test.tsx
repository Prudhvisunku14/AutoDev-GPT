import { screen } from "@testing-library/react";
import { waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { DEFAULT_BUDGET, renderWithProviders, stubApi } from "../test/render";
import { BudgetIndicator } from "./BudgetIndicator";

describe("BudgetIndicator", () => {
  it("shows the backend's enforced per-run limit and recorded spending", async () => {
    renderWithProviders(<BudgetIndicator />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run budget" })).toHaveTextContent("Limit $2.00"));
    await userEvent.click(screen.getByRole("button", { name: "Run budget" }));
    expect(screen.getByText("Maximum per run").nextSibling).toHaveTextContent("$2.00");
    expect(screen.getByText("Recorded spend").nextSibling).toHaveTextContent("$0.0500");
  });

  it("shows when missing prices prevent paid builds", async () => {
    const api = stubApi({ getBudget: async () => ({ ...DEFAULT_BUDGET, cost_known: false }) });
    renderWithProviders(<BudgetIndicator />, { api });
    await userEvent.click(screen.getByRole("button", { name: "Run budget" }));
    expect(await screen.findByText("Prices are not configured; paid builds are blocked.")).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    renderWithProviders(<BudgetIndicator />);
    await userEvent.click(screen.getByRole("button", { name: "Run budget" }));
    expect(screen.getByRole("dialog", { name: "Run budget" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
