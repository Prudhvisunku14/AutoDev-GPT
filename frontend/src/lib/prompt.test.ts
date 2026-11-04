import { describe, expect, it } from "vitest";
import type { StackCapability } from "./types";
import { decideStack, requestedStack } from "./prompt";

const stack = (id: string, verifiable: boolean, reason = "ok"): StackCapability => ({ id, name: id, language: "x", frameworks: [], description: "", verifiable, reason });

describe("requestedStack", () => {
  it.each([
    ["Create a homepage for my bakery", "static-html"],
    ["A landing page for a yoga studio", "static-html"],
    ["Build a React dashboard for sales", "react-vite"],
    ["A FastAPI service for todo items", "python-fastapi"],
    ["A weather forecast application", "python-cli"],
    ["A command-line tool that downloads a web page and counts words", "python-cli"],
    ["A Python script that checks a website for broken links", "python-cli"],
  ])("%s -> %s", (prompt, expected) => {
    expect(requestedStack(prompt).stackId).toBe(expected);
  });

  it("has no stack for mobile apps", () => {
    expect(requestedStack("An Android app for my gym").stackId).toBeNull();
  });
});

describe("decideStack", () => {
  it("builds what the backend can verify and declines the rest with its reason", () => {
    const stacks = [stack("python-cli", true), stack("static-html", false, "the verifier image is not self-tested")];
    expect(decideStack("A weather forecast application", stacks)).toEqual({ stack: stacks[0], refusal: null });
    expect(decideStack("A homepage for my bakery", stacks).refusal).toBe("static-html builds are not available on this backend yet: the verifier image is not self-tested.");
    expect(decideStack("A homepage for my bakery", [stack("python-cli", true), stack("static-html", true)]).stack?.id).toBe("static-html");
  });

  it("waits for the backend instead of guessing, but declines mobile apps at once", () => {
    expect(decideStack("A homepage for my bakery", undefined)).toEqual({ stack: null, refusal: null });
    expect(decideStack("An iOS app for recipes", undefined).refusal).toContain("no stack for mobile apps");
  });
});
