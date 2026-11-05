import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ToolAnswer, ToolEntry, ToolsListing } from "../lib/types";
import { renderApp, stubApi } from "../test/render";

const N8N: ToolEntry = {
  id: "n8n",
  name: "n8n",
  role: "workflow automation",
  description: "Runs registered automation workflows through signed webhooks.",
  tasks: [{ id: "workflow_automation", text: "Run an automation workflow and receive its result" }],
  advantages: ["Hundreds of ready-made service connectors"],
  limitations: ["A delayed callback is stored but does not resume a run"],
  requirements: ["a running n8n instance"],
  readiness: { status: "not_configured", detail: "N8N_ENABLED is not true in this backend" },
  functions: [],
  evidence: [],
  strongest_evidence: { level: "offline_test", outcome: "passed", date: "2026-11-04", summary: "Signed triggers against a simulated n8n", source: "docs/PROGRESS.md" },
  verified_live: false,
};

const LISTING: ToolsListing = {
  tasks: { workflow_automation: "Run an automation workflow and receive its result" },
  tools: [N8N],
  planned: [{ id: "opencode_via_n8n", name: "OpenCode through n8n", description: "Not implemented.", readiness: { status: "planned", detail: "not implemented" } }],
};

describe("Tools page", () => {
  it("lists each tool with its readiness on this backend and its strongest evidence", async () => {
    renderApp({ api: stubApi({ listTools: vi.fn(() => Promise.resolve(LISTING)) }), route: "/tools" });
    const card = await screen.findByRole("listitem", { name: "n8n" });
    expect(within(card).getByText("Not set up")).toBeInTheDocument();
    expect(within(card).getByText("N8N_ENABLED is not true in this backend")).toBeInTheDocument();
    expect(within(card).getByText(/Offline tests only, passed/)).toBeInTheDocument();
    expect(screen.getByText("OpenCode through n8n: planned, not built")).toBeInTheDocument();
  });

  it("answers a comparison from the catalog and says plainly which name is not a DevPilot tool", async () => {
    const answer: ToolAnswer = {
      kind: "comparison",
      tools: [N8N],
      unknown: [{ name: "Cloudflare", note: "Cloudflare is not a tool DevPilot has an adapter for." }],
      planned: [],
      notes: ["n8n: strongest evidence is offline tests only"],
    };
    const askTools = vi.fn(() => Promise.resolve(answer));
    renderApp({ api: stubApi({ listTools: vi.fn(() => Promise.resolve(LISTING)), askTools }), route: "/tools" });
    await userEvent.type(await screen.findByLabelText("Ask about tools"), "Compare n8n and Cloudflare");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    const section = await screen.findByRole("region", { name: "Answer" });
    expect(askTools).toHaveBeenCalledWith("Compare n8n and Cloudflare");
    expect(within(section).getByText("Cloudflare: not a DevPilot tool")).toBeInTheDocument();
    expect(within(section).getByRole("cell", { name: "n8n" })).toBeInTheDocument();
    expect(within(section).getByText("Run an automation workflow and receive its result")).toBeInTheDocument();
  });

  it("is reachable from the main navigation", async () => {
    renderApp({ api: stubApi({ listTools: vi.fn(() => Promise.resolve(LISTING)) }) });
    const [link] = await screen.findAllByRole("link", { name: "Tools" });
    await userEvent.click(link!);
    expect(await screen.findByRole("heading", { name: "Tools" })).toBeInTheDocument();
  });
});
