import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FileBrowser } from "../components/FileBrowser";
import { ApiProvider } from "../components/ApiProvider";
import { createQueryClient } from "./queries";
import { clearToken, setToken } from "./token";
import { stubApi } from "../test/render";

afterEach(() => clearToken());

describe("the query cache across tokens", () => {
  it("is emptied when the token changes or is cleared, so nothing read with one token shows under another", () => {
    setToken("first-token");
    const client = createQueryClient();
    client.setQueryData(["projects"], [{ id: "p-secret" }]);
    setToken("first-token"); // the same token again: nothing to forget
    expect(client.getQueryData(["projects"])).toEqual([{ id: "p-secret" }]);
    setToken("second-token");
    expect(client.getQueryData(["projects"])).toBeUndefined();
    client.setQueryData(["projects"], [{ id: "p-other" }]);
    clearToken();
    expect(client.getQueryData(["projects"])).toBeUndefined();
  });
});

describe("FileBrowser", () => {
  it("drops the selected file when the project changes or the file disappears", async () => {
    setToken("test-token");
    const api = stubApi({ readProjectFile: vi.fn(() => Promise.resolve({ path: "app.py", content: "print(1)", bytes: 8 })) } as never);
    const client = createQueryClient();
    const view = (projectId: string, paths: string[]) => (
      <ApiProvider client={api}>
        <QueryClientProvider client={client}>
          <FileBrowser projectId={projectId} paths={paths} />
        </QueryClientProvider>
      </ApiProvider>
    );
    const { rerender } = render(view("p1", ["app.py", "README.md"]));
    await userEvent.click(screen.getByRole("button", { name: /app\.py/ }));
    expect(screen.getByText("app.py", { selector: "p" })).toBeInTheDocument();
    rerender(view("p2", ["app.py", "README.md"]));
    expect(screen.queryByText("app.py", { selector: "p" })).not.toBeInTheDocument();
    rerender(view("p1", ["README.md"]));
    expect(screen.queryByText("app.py", { selector: "p" })).not.toBeInTheDocument();
  });
});
