// The react-vitest recipe's fixed entry (app/verification/recipes.py), at /opt/verify/run-vitest.mjs in
// devpilot/verify-react:1:
//
//     node /opt/verify/run-vitest.mjs /workspace/<component> [--acceptance]
//
// NOT DOCKER-TESTED: the image does not exist. The copy and every check below are tested offline with the host's Node
// (backend/tests/unit/test_react_runner.py); Vitest itself has never run here.
//
// The workspace is read-only and holds no node_modules, and nothing is installed at test time. So the component's
// approved files are copied into /tmp/app (the container's 64 MB tmpfs), the image's own node_modules is linked beside
// them, and Vitest runs with DevPilot's fixed config (vitest.config.mjs), its cache in /tmp. Vitest's own output goes to
// stderr; only its JSON report goes to stdout, for the recipe's parser. A link, a special file, a hard-linked file, a
// name that could escape, or a tree over the limits stops the run before Vitest starts. The container has no network,
// and this script starts no process but Vitest.

import { spawnSync } from "node:child_process";
import { constants, copyFileSync, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, symlinkSync } from "node:fs";
import { extname, join } from "node:path";
import { pathToFileURL } from "node:url";

export const VERIFY = "/opt/verify";
export const APP = "/tmp/app";
export const REPORT = "/tmp/vitest-report.json";
export const LIMITS = Object.freeze({ files: 2000, bytes: 16 * 1024 * 1024, fileBytes: 1024 * 1024, depth: 12 });
const COMPONENT = /^\/workspace\/[a-z][a-z0-9_-]{0,31}$/; // the recipe's folder rule (recipes.FOLDER), one level only
const NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$/; // no dot files, no separators, nothing a path could read twice
const EXTENSIONS = new Set([".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".json", ".css", ".html", ".svg", ".md", ".txt"]);
const NOT_COPIED = new Set(["node_modules", "dist", "build", "coverage"]); // the image's node_modules is linked instead

export class Refused extends Error {}

export function componentDir(arg) {
  if (typeof arg !== "string" || !COMPONENT.test(arg)) throw new Refused(`${JSON.stringify(arg)} is not /workspace/<component>`);
  return arg;
}

/** Copy `source`'s approved files into `dest`, which must not exist yet. Returns what was copied and what was left out. */
export function prepare(source, dest, limits = LIMITS) {
  if (lstatSync(source).isSymbolicLink()) throw new Refused("the component folder is a link");
  const root = realpathSync(source);
  if (existsSync(dest)) throw new Refused(`${dest} already exists`);
  mkdirSync(dest, { mode: 0o700 });
  const copied = [];
  const skipped = [];
  let bytes = 0;
  const walk = (rel, depth) => {
    if (depth > limits.depth) throw new Refused(`${rel} is nested more than ${limits.depth} folders deep`);
    for (const entry of readdirSync(rel ? join(root, rel) : root).sort()) {
      const path = rel ? `${rel}/${entry}` : entry;
      if (!NAME.test(entry)) {
        skipped.push(path);
        continue;
      }
      const from = join(root, path);
      const stat = lstatSync(from);
      if (stat.isSymbolicLink()) throw new Refused(`${path} is a link; links are never followed`);
      if (stat.isDirectory()) {
        if (NOT_COPIED.has(entry)) {
          skipped.push(`${path}/`);
          continue;
        }
        mkdirSync(join(dest, path), { mode: 0o700 });
        walk(path, depth + 1);
      } else if (stat.isFile()) {
        if (!EXTENSIONS.has(extname(entry).toLowerCase())) {
          skipped.push(path);
          continue;
        }
        if (stat.nlink > 1) throw new Refused(`${path} has other names (a hard link)`);
        if (stat.size > limits.fileBytes) throw new Refused(`${path} is over ${limits.fileBytes} bytes`);
        bytes += stat.size;
        if (copied.length >= limits.files || bytes > limits.bytes) throw new Refused(`the component is over ${limits.files} files or ${limits.bytes} bytes`);
        if (realpathSync(from) !== from) throw new Refused(`${path} resolves outside the component`);
        copyFileSync(from, join(dest, path), constants.COPYFILE_EXCL);
        copied.push(path);
      } else {
        throw new Refused(`${path} is not a regular file or folder`);
      }
    }
  };
  walk("", 0);
  return { copied, skipped, bytes };
}

/** Vitest's arguments: always DevPilot's config, the copy as the root, and the JSON report in /tmp. */
export function vitestArgs() {
  return [`${VERIFY}/node_modules/vitest/vitest.mjs`, "run", "--config", `${VERIFY}/vitest.config.mjs`, "--root", APP, "--reporter=json", `--outputFile=${REPORT}`];
}

/** Vitest's environment: fixed values only. The mode chooses the config's test files; HOME and TMPDIR keep caches in /tmp. */
export function childEnv(acceptance, inherited = process.env) {
  return { PATH: inherited.PATH || "/usr/local/bin:/usr/bin:/bin", HOME: "/tmp", TMPDIR: "/tmp", CI: "true", NO_COLOR: "1", DEVPILOT_VITEST_MODE: acceptance ? "acceptance" : "component" };
}

export function main(args) {
  const acceptance = args.length === 2 && args[1] === "--acceptance";
  if (args.length !== 1 && !acceptance) throw new Refused("usage: run-vitest.mjs /workspace/<component> [--acceptance]");
  const { copied, skipped } = prepare(componentDir(args[0]), APP);
  if (copied.length === 0) throw new Refused("the component has no files to test");
  symlinkSync(`${VERIFY}/node_modules`, `${APP}/node_modules`, "dir");
  process.stderr.write(`run-vitest: copied ${copied.length} file(s), left out ${skipped.length}\n`);
  const run = spawnSync(process.execPath, vitestArgs(), { cwd: APP, env: childEnv(acceptance), stdio: ["ignore", 2, 2] });
  if (run.error) process.stderr.write(`run-vitest: Vitest did not start: ${run.error.message}\n`);
  if (existsSync(REPORT)) process.stdout.write(readFileSync(REPORT, "utf8"));
  return run.status ?? 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`run-vitest: ${error instanceof Refused ? "refused: " : ""}${error.message}\n`);
    process.exitCode = error instanceof Refused ? 3 : 2;
  }
}
