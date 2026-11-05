import { describe, expect, it } from "vitest";
import type { StackCapability } from "./types";
import { validatePrompt, verifiableStacks } from "./prompt";

const stack = (id: string, verifiable: boolean, reason = "ok"): StackCapability => ({ id, name: id, language: "x", frameworks: [], description: "", verifiable, reason });

describe("verifiableStacks", () => {
  it("lists only what the backend can verify, which GPT chooses from; nothing is guessed from the request's words", () => {
    const stacks = [stack("python-cli", true), stack("static-html", false, "the verifier image is not self-tested"), stack("react-vite", true)];
    expect(verifiableStacks(stacks).map((s) => s.id)).toEqual(["python-cli", "react-vite"]);
    expect(verifiableStacks(undefined)).toEqual([]);
  });
});

describe("validatePrompt", () => {
  it("asks for enough detail and refuses an oversized request", () => {
    expect(validatePrompt("  ")).toBe("Describe the application you want to build.");
    expect(validatePrompt("a homepage")).toMatch(/at least 15 characters/);
    expect(validatePrompt("Create a homepage for my bakery")).toBeNull();
    expect(validatePrompt("x".repeat(19_001))).toMatch(/the limit is/);
  });
});
