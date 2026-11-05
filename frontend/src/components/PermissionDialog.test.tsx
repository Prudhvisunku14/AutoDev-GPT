import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ResourceCheck } from "../lib/types";
import { renderWithProviders, stubApi } from "../test/render";
import { PermissionDialog } from "./PermissionDialog";

const PRIVATE: ResourceCheck = {
  resources: [
    {
      kind: "github",
      key: "github:acme/secret@",
      url: "https://github.com/acme/secret",
      label: "https://github.com/acme/secret",
      full_name: "acme/secret",
      status: "authorization_required",
      message: "GitHub did not show acme/secret without a connection. It may be private, or the name may be wrong.",
      action: "connect_github",
    },
  ],
  problems: [],
  needs_permission: ["github:acme/secret@"],
};

const READABLE: ResourceCheck = {
  resources: [{ ...PRIVATE.resources[0]!, status: "accessible", message: "Read acme/secret at 0123456789ab.", action: null, visibility: "private" }],
  problems: [],
  needs_permission: [],
};

describe("PermissionDialog", () => {
  afterEach(() => vi.useRealTimers());

  it("connects GitHub in its own window, without a token in the dashboard, and continues by itself once access is granted", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const popup = { location: { href: "" }, close: vi.fn() } as unknown as Window;
    const open = vi.spyOn(window, "open").mockReturnValue(popup);
    const connected = { configured: true, blocker: null, connected: true, login: "octo", scopes: ["repo"], connected_at: 1, requested_scopes: ["repo"] };
    const api = stubApi({
      connectGitHub: vi.fn(() => Promise.resolve({ url: "https://github.com/login/oauth/authorize?state=s" })),
      githubStatus: vi.fn(() => Promise.resolve(connected)),
      checkResources: vi.fn(() => Promise.resolve(READABLE)),
    });
    const onReady = vi.fn();
    renderWithProviders(<PermissionDialog projectId="p1" initial={PRIVATE} onReady={onReady} onSkip={vi.fn()} onCancel={vi.fn()} />, { api });
    expect(screen.getByRole("dialog", { name: "Permission needed" })).toBeInTheDocument();
    expect(screen.getByText(/It may be private, or the name may be wrong/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Connect GitHub/ }));
    expect(open).toHaveBeenCalledWith("about:blank", "devpilot-consent", expect.any(String)); // opened in the click, so it is not blocked
    expect(popup.location.href).toBe("https://github.com/login/oauth/authorize?state=s");
    expect(await screen.findByRole("status")).toHaveTextContent("Waiting for you to finish in the GitHub window");
    await vi.advanceTimersByTimeAsync(2_100);
    expect(api.checkResources).toHaveBeenCalledWith("p1");
    expect(onReady).toHaveBeenCalledWith(READABLE);
    open.mockRestore();
  });

  it("lets the person build without the resource or cancel", async () => {
    const onSkip = vi.fn();
    const onCancel = vi.fn();
    renderWithProviders(<PermissionDialog projectId="p1" initial={PRIVATE} onReady={vi.fn()} onSkip={onSkip} onCancel={onCancel} />);
    await userEvent.click(screen.getByRole("button", { name: "Build without it" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onSkip).toHaveBeenCalledOnce();
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
