// DevPilot's own Vitest configuration for the react-vitest recipe, at /opt/verify/vitest.config.mjs in
// devpilot/verify-react:1. run-vitest.mjs always passes it with --config, so a project's vite.config or vitest.config is
// never read: it cannot redirect reporters, widen or narrow which tests run, or move the cache out of /tmp.
// NOT DOCKER-TESTED, and not yet checked against the pinned Vitest version (the versions are pending).
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const acceptance = process.env.DEVPILOT_VITEST_MODE === "acceptance"; // set by run-vitest.mjs, never by the project

export default defineConfig({
  plugins: [react()],
  cacheDir: "/tmp/vite-cache", // the image's node_modules and the workspace are read-only
  logLevel: "error",
  test: {
    environment: "jsdom",
    // The evaluator's frozen files only, or the component's own tests without them.
    include: acceptance ? ["__acceptance__/**/*.test.{js,jsx,ts,tsx}"] : ["**/*.{test,spec}.{js,jsx,ts,tsx}"],
    exclude: acceptance ? ["**/node_modules/**"] : ["**/node_modules/**", "__acceptance__/**"],
    watch: false,
    allowOnly: false, // a focused test (.only) would hide the others: it fails the run instead
    passWithNoTests: false,
    testTimeout: 20_000,
  },
});
