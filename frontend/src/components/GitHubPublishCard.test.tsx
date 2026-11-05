import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { GithubPRPreview } from "../lib/types";
import { makeRun } from "../test/fixtures";
import { renderWithProviders, stubApi } from "../test/render";
import { GitHubPublishCard } from "./GitHubPublishCard";

const AVAILABLE = { available: true, reason: null, filename: "app.zip", files: [{ path: "index.html", bytes: 10 }], excluded: [], total_bytes: 10, simulated: [] };
const PREVIEW: GithubPRPreview = {
  preview_id: "p".repeat(32),
  expires_in_seconds: 600,
  verification_level: "independent-acceptance",
  confirmation_required: "publish octo/site:devpilot/r1",
  github_account: "octo",
  preview: { run_id: "r1", repository: "octo/site", branch: "devpilot/r1", base: "main", title: "DevPilot verified implementation", total_bytes: 10, fingerprint: "f", files: [{ path: "index.html", bytes: 10, sha256: "a" }] },
};

describe("GitHubPublishCard", () => {
  it("is not offered for a run without a verified delivery", async () => {
    const api = stubApi();
    renderWithProviders(<GitHubPublishCard run={makeRun({ id: "r1", state: "COMPLETED" })} />, { api });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole("region", { name: "Publish a GitHub draft pull request" })).not.toBeInTheDocument();
  });

  it("previews every file first and publishes only after the exact confirmation is typed", async () => {
    const previewGithubPR = vi.fn(() => Promise.resolve(PREVIEW));
    const publishGithubPR = vi.fn(() => Promise.resolve({ url: "https://github.com/octo/site/pull/1", branch: "devpilot/r1", commit: "c", repository: "octo/site", draft: "true" }));
    const api = stubApi({ getArtifact: vi.fn(() => Promise.resolve(AVAILABLE)), previewGithubPR, publishGithubPR });
    renderWithProviders(<GitHubPublishCard run={makeRun({ id: "r1", state: "COMPLETED" })} />, { api });
    await userEvent.type(await screen.findByLabelText(/Repository/), "octo/site");
    await userEvent.click(screen.getByRole("button", { name: "Preview GitHub changes" }));
    expect(await screen.findByText(/index\.html \(10 bytes\)/)).toBeInTheDocument();
    expect(screen.getByText("As GitHub account octo")).toBeInTheDocument();
    const publish = screen.getByRole("button", { name: "Create draft PR (GitHub write)" });
    expect(publish).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/to publish/), "publish octo/site:devpilot/r");
    expect(publish).toBeDisabled(); // nearly right is not right
    await userEvent.type(screen.getByLabelText(/to publish/), "1");
    await userEvent.click(publish);
    expect(publishGithubPR).toHaveBeenCalledWith(PREVIEW.preview_id, "publish octo/site:devpilot/r1");
    expect(await screen.findByRole("link", { name: "View on GitHub" })).toHaveAttribute("href", "https://github.com/octo/site/pull/1");
  });
});
