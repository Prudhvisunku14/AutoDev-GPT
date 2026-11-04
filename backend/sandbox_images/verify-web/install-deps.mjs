// DevPilot's fixed dependency installer. It runs in devpilot/verify-web on an internal network whose only way out is
// the install proxy (registry.npmjs.org:443), over a disposable copy of one component at /work.
//
//   node /opt/verify/install-deps.mjs
//
// It refuses a package.json that names a dependency outside the npm registry (git, URLs, file:, link:), then runs npm
// with lifecycle scripts disabled (no package code runs while the network is reachable), DevPilot's own npmrc and no
// audit or funding calls: `npm ci` when package-lock.json exists, otherwise `npm install` without writing a lockfile.
// The last line is `DEVPILOT_INSTALL_REPORT <json>`.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";

const report = { ok: false, command: null, detail: "" };
const done = (ok, detail) => {
  report.ok = ok;
  report.detail = String(detail).slice(-6000);
  console.log("DEVPILOT_INSTALL_REPORT " + JSON.stringify(report));
  process.exit(ok ? 0 : 1);
};
let manifest;
try {
  manifest = JSON.parse(readFileSync("/work/package.json", "utf8"));
} catch (e) {
  done(false, `package.json cannot be read: ${e.message}`);
}
const deps = { ...(manifest.dependencies || {}), ...(manifest.devDependencies || {}), ...(manifest.optionalDependencies || {}) };
const unsafe = Object.entries(deps).filter(([, v]) => typeof v !== "string" || /^(git|github:|gitlab:|bitbucket:|https?:|file:|link:|portal:|\.|\/)|:\/\//.test(v.trim()));
if (unsafe.length) done(false, `dependencies that are not registry versions are not installed: ${unsafe.map(([k, v]) => `${k}@${v}`).join(", ")}`);
if (existsSync("/work/.npmrc")) done(false, "a project .npmrc is present; DevPilot removes it before install, so this copy was not prepared by DevPilot");
const argv = existsSync("/work/package-lock.json")
  ? ["ci", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"]
  : ["install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock", "--loglevel=error"];
report.command = "npm " + argv.join(" ");
const env = { ...process.env, npm_config_userconfig: "/opt/verify/npmrc", npm_config_globalconfig: "/tmp/devpilot-no-global-npmrc", npm_config_registry: "https://registry.npmjs.org/",
  npm_config_ignore_scripts: "true", npm_config_cache: "/tmp/npm-cache", npm_config_update_notifier: "false", HOME: "/tmp" };
const result = spawnSync("npm", argv, { cwd: "/work", encoding: "utf8", env, timeout: 600_000 });
const output = `${result.stdout || ""}${result.stderr || ""}`.trim();
done(result.status === 0 && !result.error, result.error ? result.error.message : output || "installed");
