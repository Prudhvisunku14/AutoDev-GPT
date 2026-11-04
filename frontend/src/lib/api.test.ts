import { describe, expect, it, vi } from "vitest";
import { ApiError, createApiClient, EVENT_PAGE, UnauthorizedError } from "./api";

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function clientWith(fetchImpl: typeof fetch, token: string | null = "tok") {
  return createApiClient({ baseUrl: "/api/v1/", getToken: () => token, fetchImpl });
}

function call(fetchImpl: ReturnType<typeof vi.fn<typeof fetch>>, index = 0): { url: string; init: RequestInit & { headers: Record<string, string> } } {
  const [url, init] = fetchImpl.mock.calls[index] ?? [];
  return { url: String(url), init: init as RequestInit & { headers: Record<string, string> } };
}

describe("api client", () => {
  it("sends the bearer token to the same-origin API", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply([]));
    await clientWith(fetchImpl).listProjects();
    const { url, init } = call(fetchImpl);
    expect(url).toBe("/api/v1/projects?limit=200");
    expect(init.method).toBe("GET");
    expect(init.headers.Authorization).toBe("Bearer tok");
  });

  it("sends no Authorization header without a token", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ status: "ok" }));
    await clientWith(fetchImpl, null).health();
    expect(call(fetchImpl).init.headers).not.toHaveProperty("Authorization");
  });

  it("checks a candidate token without using the stored one", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ status: "ok" }));
    await clientWith(fetchImpl, "stored").checkToken("candidate");
    const { url, init } = call(fetchImpl);
    expect(url).toBe("/api/v1/status");
    expect(init.headers.Authorization).toBe("Bearer candidate");
  });

  it("raises UnauthorizedError on 401", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ detail: "unauthorized" }, 401));
    await expect(clientWith(fetchImpl).status()).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("returns null when an optional resource does not exist yet", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ detail: "no plan" }, 404));
    await expect(clientWith(fetchImpl).getPlan("r1")).resolves.toBeNull();
    await expect(clientWith(fetchImpl).getReport("r1")).resolves.toBeNull();
  });

  it("still fails on a 404 for a required resource", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ detail: "run not found" }, 404));
    await expect(clientWith(fetchImpl).getRun("r1")).rejects.toMatchObject({ status: 404 });
  });

  it("carries the backend's error detail", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ detail: "requirement_text is too long" }, 422));
    const failure = await clientWith(fetchImpl)
      .createProject({ name: "x", requirement_text: "y" })
      .catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(422);
    expect((failure as ApiError).message).toContain("requirement_text is too long");
  });

  it("reports an unreachable backend as status 0", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(clientWith(fetchImpl).status()).rejects.toMatchObject({ status: 0, message: expect.stringContaining("could not be reached") });
  });

  it("reports a backend the dev proxy could not reach as status 0, not as a backend error", async () => {
    const proxied = new Response(JSON.stringify({ detail: "could not be reached" }), { status: 502, headers: { "X-DevPilot-Proxy": "backend-unreachable" } });
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(proxied);
    await expect(clientWith(fetchImpl).status()).rejects.toMatchObject({ status: 0, message: expect.stringContaining("Is it running?") });
  });

  it("keeps the backend's own 502 as a backend error", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ detail: "Google Drive access was not completed" }, 502));
    await expect(clientWith(fetchImpl).status()).rejects.toMatchObject({ status: 502 });
  });

  it("posts a new project as JSON", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ id: "p1" }, 201));
    await clientWith(fetchImpl).createProject({ name: "Tracker", requirement_text: "Build a tracker" });
    const { url, init } = call(fetchImpl);
    expect(url).toBe("/api/v1/projects");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ name: "Tracker", requirement_text: "Build a tracker" });
  });

  it("lists runs from the authenticated backend-wide endpoint", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply([]));
    await clientWith(fetchImpl).listRuns(25);
    expect(call(fetchImpl).url).toBe("/api/v1/runs?limit=25");
  });

  it("starts a run only with the explicitly approved maximum", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ id: "r1" }, 202));
    await clientWith(fetchImpl).startRun("run/1", 1.25);
    const { url, init } = call(fetchImpl);
    expect(url).toBe("/api/v1/runs/run%2F1/start");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ approved_max_usd: 1.25 });
  });

  it("downloads artifacts with the API token in the header and never in the URL", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response("zip-data", { status: 200 }));
    const blob = await clientWith(fetchImpl).downloadArtifact("r1");
    expect(blob.size).toBe(8);
    const { url, init } = call(fetchImpl);
    expect(url).toBe("/api/v1/runs/r1/artifact.zip");
    expect(init.headers.Authorization).toBeDefined();
  });

  it("exposes spending, capability, approval and historical API methods", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockImplementation(async () => reply({}));
    const api = clientWith(fetchImpl);
    await api.getBudget();
    expect(call(fetchImpl, 0).url).toBe("/api/v1/budget");
    await api.getCapabilities();
    expect(call(fetchImpl, 1).url).toBe("/api/v1/capabilities");
    await api.decideApproval("approval/1", "deny");
    expect(call(fetchImpl, 2).url).toBe("/api/v1/approvals/approval%2F1/decision");
    expect(JSON.parse(String(call(fetchImpl, 2).init.body))).toEqual({ decision: "deny" });
    await api.listHistoryRuns();
    expect(call(fetchImpl, 3).url).toBe("/api/v1/history/runs");
  });

  it("encodes ids and file paths", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(reply({ path: "a", content: "" }));
    await clientWith(fetchImpl).readProjectFile("p/1", "src/a b.py");
    expect(call(fetchImpl).url).toBe("/api/v1/projects/p%2F1/files/content?path=src%2Fa%20b.py");
  });

  it("pages through a run's events by sequence number", async () => {
    const first = Array.from({ length: EVENT_PAGE }, (_, i) => ({ seq: i + 1, type: "x", payload: {} }));
    const second = [{ seq: EVENT_PAGE + 1, type: "x", payload: {} }];
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValueOnce(reply(first)).mockResolvedValueOnce(reply(second));
    const events = await clientWith(fetchImpl).getAllEvents("r1");
    expect(events).toHaveLength(EVENT_PAGE + 1);
    expect(call(fetchImpl, 0).url).toContain("after_seq=0");
    expect(call(fetchImpl, 1).url).toContain(`after_seq=${EVENT_PAGE}`);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

});
