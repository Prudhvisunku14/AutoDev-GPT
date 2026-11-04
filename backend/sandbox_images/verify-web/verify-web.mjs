// DevPilot's fixed website verifier. It runs inside devpilot/verify-web with NO network, over a disposable copy of
// one component at /work (dependencies, if any, were installed into it beforehand through the install proxy).
//
//   node /opt/verify/verify-web.mjs --kind react-vite|static-html [--expect /spec/expect.json]
//
// Stages, each recorded with its result; the first failing mandatory stage stops the rest:
//   manifest   package.json is readable and declares no git/url/file/link dependency (react-vite)
//   installed  node_modules holds react, react-dom and vite (react-vite)
//   typecheck  tsc with the project's tsconfig, when the project is TypeScript (react-vite)
//   build      vite build into /tmp/devpilot-dist, DevPilot's output folder, never the project's scripts (react-vite)
//   serve      DevPilot's own static server on 127.0.0.1 (single-page fallback for react-vite)
//   http       GET / and every page answer 200 with HTML
//   assets     every same-origin script, stylesheet, image and icon the pages reference answers 200
//   browser    headless Chromium: each page renders visible content with no uncaught error, no failed same-origin
//              request and no console error; same-origin links are followed; visible buttons are clicked
//   acceptance DevPilot's frozen browser checks (/spec/expect.json, version 2), one result per criterion, when given
// The assets, browser and acceptance stages all run once the pages are served, so one run reports every problem.
// Requests to other origins are blocked (there is no network) and reported, not counted as failures.
// The last line of output is `DEVPILOT_WEB_REPORT <json>`. Exit code 0 only when every stage passed.

import { spawnSync } from "node:child_process";
import { createReadStream, existsSync, readFileSync, statSync, readdirSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";

const WORK = "/work";
const DIST = "/tmp/devpilot-dist";
const MAX_PAGES = 20, MAX_CLICKS = 25, STAGE_TIMEOUT_MS = 240_000;
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));
const kind = args.kind;
const report = { kind, stages: [], pages: [], blocked_external: [], verdict: "FAILED", reason: "" };

function stage(name, ok, detail, extra = {}) {
  report.stages.push({ name, ok, detail: String(detail).slice(0, 4000), ...extra });
  return ok;
}
function finish(reason) {
  const failed = report.stages.find((s) => !s.ok);
  report.verdict = failed ? "FAILED" : "PASSED";
  report.reason = reason || (failed ? `${failed.name}: ${failed.detail.split("\n")[0]}` : "every stage passed");
  console.log("DEVPILOT_WEB_REPORT " + JSON.stringify(report));
  process.exit(report.verdict === "PASSED" ? 0 : 1);
}
function run(argv, cwd = WORK) {
  const done = spawnSync(argv[0], argv.slice(1), { cwd, encoding: "utf8", timeout: STAGE_TIMEOUT_MS, env: { ...process.env, CI: "1", NODE_ENV: "production", BROWSERSLIST_IGNORE_OLD_DATA: "1" } });
  const output = `${done.stdout || ""}${done.stderr || ""}`.trim();
  return { ok: done.status === 0 && !done.error, output: done.error ? `${done.error.message}\n${output}` : output };
}

if (!["react-vite", "static-html"].includes(kind)) {
  stage("arguments", false, `unknown kind ${kind}`);
  finish();
}

let root = WORK;
if (kind === "react-vite") {
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(join(WORK, "package.json"), "utf8"));
  } catch (e) {
    stage("manifest", false, `package.json cannot be read: ${e.message}`);
    finish();
  }
  const deps = { ...(manifest.dependencies || {}), ...(manifest.devDependencies || {}) };
  const unsafe = Object.entries(deps).filter(([, v]) => typeof v !== "string" || /^(git|github:|gitlab:|bitbucket:|https?:|file:|link:|portal:|\.|\/)|:\/\//.test(v.trim()));
  if (!stage("manifest", unsafe.length === 0, unsafe.length ? `dependencies that are not registry versions: ${unsafe.map(([k, v]) => `${k}@${v}`).join(", ")}` : `${Object.keys(deps).length} registry dependencies`)) finish();
  const missing = ["react", "react-dom", "vite"].filter((d) => !existsSync(join(WORK, "node_modules", d, "package.json")));
  if (!stage("installed", missing.length === 0, missing.length ? `not installed: ${missing.join(", ")}` : "react, react-dom and vite are installed")) finish();

  const tsconfig = join(WORK, "tsconfig.json");
  const tsc = join(WORK, "node_modules", "typescript", "bin", "tsc");
  if (existsSync(tsconfig)) {
    if (!existsSync(tsc)) {
      stage("typecheck", false, "the project has tsconfig.json but typescript is not installed");
      finish();
    }
    let references = false;
    try {
      references = /"references"\s*:/.test(readFileSync(tsconfig, "utf8"));
    } catch {}
    const result = run(references ? ["node", tsc, "-b"] : ["node", tsc, "--noEmit", "-p", "tsconfig.json"]);
    if (!stage("typecheck", result.ok, result.ok ? "tsc reported no errors" : result.output)) finish();
  } else {
    stage("typecheck", true, "not a TypeScript project (no tsconfig.json)", { skipped: true });
  }
  const vite = join(WORK, "node_modules", "vite", "bin", "vite.js");
  const built = run(["node", vite, "build", "--outDir", DIST, "--emptyOutDir", "--logLevel", "warn"]);
  if (!stage("build", built.ok && existsSync(join(DIST, "index.html")), built.ok ? (existsSync(join(DIST, "index.html")) ? "vite build produced index.html" : "vite build produced no index.html") : built.output)) finish();
  root = DIST;
} else if (!existsSync(join(WORK, "index.html"))) {
  stage("serve", false, "there is no index.html");
  finish();
}

// --- DevPilot's own static server ------------------------------------------------------------------------
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon",
  ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".txt": "text/plain", ".xml": "application/xml", ".webmanifest": "application/manifest+json", ".mp4": "video/mp4" };
const base = resolve(root);
const server = createServer((req, res) => {
  let path;
  try {
    path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  } catch {
    res.writeHead(400).end();
    return;
  }
  let file = normalize(join(base, path));
  if (file !== base && !file.startsWith(base + sep)) return res.writeHead(403).end();
  if (existsSync(file) && statSync(file).isDirectory()) file = join(file, "index.html");
  if (!existsSync(file)) {
    if (kind === "react-vite" && !extname(path)) file = join(base, "index.html"); // client-side routes
    else return res.writeHead(404).end("not found");
  }
  res.writeHead(200, { "content-type": TYPES[extname(file).toLowerCase()] || "application/octet-stream" });
  createReadStream(file).pipe(res);
});
await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
const origin = `http://127.0.0.1:${server.address().port}`;
stage("serve", true, `serving ${root === DIST ? "the build output" : "the site folder"} on ${origin}`);

// --- HTTP and assets -----------------------------------------------------------------------------------------
function htmlPages(dir, prefix = "") {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (["node_modules", ".git"].includes(entry.name)) continue;
    const rel = prefix + entry.name;
    if (entry.isDirectory() && prefix.split("/").length < 4) found.push(...htmlPages(join(dir, entry.name), rel + "/"));
    else if (/\.html?$/i.test(entry.name)) found.push("/" + rel);
  }
  return found;
}
let expect = null;
if (args.expect) {
  try {
    expect = JSON.parse(readFileSync(args.expect, "utf8"));
  } catch (e) {
    stage("acceptance", false, `the acceptance checks cannot be read: ${e.message}`);
    finish();
  }
}
const pages = new Set(["/"]);
if (kind === "static-html") htmlPages(base).slice(0, MAX_PAGES).forEach((p) => pages.add(p.replace(/\/index\.html?$/i, "/")));
for (const r of expect?.routes || []) if (typeof r === "string" && r.startsWith("/")) pages.add(r);

const httpProblems = [], assetRefs = new Set();
for (const page of pages) {
  try {
    const res = await fetch(origin + page);
    const type = res.headers.get("content-type") || "";
    const body = await res.text();
    if (res.status !== 200 || !type.includes("html")) httpProblems.push(`${page}: HTTP ${res.status} ${type}`);
    for (const m of body.matchAll(/<(?:script|img|link|source)\b[^>]*?\b(?:src|href)=["']([^"'#]+)["']/gi)) {
      const ref = m[1];
      if (/^(data:|mailto:|tel:|javascript:)/i.test(ref)) continue;
      const url = new URL(ref, origin + page);
      if (url.origin === origin && extname(url.pathname)) assetRefs.add(url.pathname);
    }
  } catch (e) {
    httpProblems.push(`${page}: ${e.message}`);
  }
}
if (!stage("http", httpProblems.length === 0, httpProblems.length ? httpProblems.join("\n") : `${pages.size} page(s) answered 200 with HTML`)) finish();
const missingAssets = [];
for (const asset of assetRefs) {
  const res = await fetch(origin + asset);
  if (res.status !== 200) missingAssets.push(`${asset} (HTTP ${res.status})`);
}
stage("assets", missingAssets.length === 0, missingAssets.length ? `missing: ${missingAssets.join(", ")}` : `${assetRefs.size} referenced asset(s) answered 200`);

// --- Browser -----------------------------------------------------------------------------------------------------
let chromium;
try {
  ({ chromium } = await import("/opt/verify/node_modules/playwright-core/index.mjs"));
} catch (e) {
  stage("browser", false, `the browser automation library is missing: ${e.message}`);
  finish();
}
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const browserProblems = [];
const visited = new Set();
const queue = [...pages];
let clicks = 0;
try {
  const context = await browser.newContext();
  await context.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith(origin) || url.startsWith("data:") || url.startsWith("blob:")) return route.continue();
    if (report.blocked_external.length < 50) report.blocked_external.push(url.slice(0, 200));
    return route.abort("blockedbyclient");
  });
  while (queue.length && visited.size < MAX_PAGES) {
    const path = queue.shift();
    if (visited.has(path)) continue;
    visited.add(path);
    const page = await context.newPage();
    const errors = [], failed = [], consoleErrors = [];
    page.on("pageerror", (e) => errors.push(e.message.slice(0, 300)));
    page.on("console", (m) => { if (m.type() === "error" && !/net::ERR_BLOCKED_BY_CLIENT|blockedbyclient/i.test(m.text())) consoleErrors.push(m.text().slice(0, 300)); });
    page.on("requestfailed", (r) => { if (r.url().startsWith(origin)) failed.push(`${r.url().slice(origin.length)} (${r.failure()?.errorText})`); });
    page.on("response", (r) => { if (r.url().startsWith(origin) && r.status() >= 400) failed.push(`${r.url().slice(origin.length)} (HTTP ${r.status()})`); });
    const entry = { path, title: "", text_chars: 0, errors, failed, console_errors: consoleErrors, links: 0, clicked: 0 };
    try {
      await page.goto(origin + path, { waitUntil: "networkidle", timeout: 30_000 });
      entry.title = (await page.title()).slice(0, 120);
      entry.text_chars = (await page.evaluate(() => (document.body?.innerText || "").trim().length));
      const links = await page.$$eval("a[href]", (as) => as.map((a) => a.href));
      for (const href of links) {
        const u = new URL(href);
        if (u.origin === origin && !visited.has(u.pathname) && !queue.includes(u.pathname) && !/\.(pdf|zip|png|jpe?g|svg)$/i.test(u.pathname)) queue.push(u.pathname);
      }
      entry.links = links.length;
      const buttons = await page.$$("button:visible:not([disabled]), [role=button]:visible");
      for (const button of buttons.slice(0, Math.max(0, MAX_CLICKS - clicks))) {
        try {
          await button.click({ timeout: 3_000, noWaitAfter: true });
          clicks++;
          entry.clicked++;
          await page.waitForTimeout(150);
          if (page.url() !== origin + path && new URL(page.url()).origin === origin) await page.goto(origin + path, { waitUntil: "networkidle", timeout: 30_000 });
        } catch {}
      }
    } catch (e) {
      errors.push(`navigation: ${e.message.split("\n")[0]}`);
    }
    if (entry.text_chars === 0) browserProblems.push(`${path}: the page rendered no visible text`);
    for (const e of errors) browserProblems.push(`${path}: uncaught error: ${e}`);
    for (const f of failed) browserProblems.push(`${path}: failed request ${f}`);
    for (const c of consoleErrors) browserProblems.push(`${path}: console error: ${c}`);
    report.pages.push(entry);
    await page.close();
  }
  stage("browser", browserProblems.length === 0, browserProblems.length ? browserProblems.slice(0, 40).join("\n") : `${visited.size} page(s) rendered in Chromium with no error; ${clicks} button click(s)`, { ran: true });

  if (expect) {
    // Independent acceptance: DevPilot's frozen checks (/spec/expect.json, version 2), one fresh page per criterion.
    // Each step is data; only this code acts on it. A check passes when every step passed and the page threw nothing.
    const results = [];
    const locate = (page, s) => {
      const name = { name: s.target, exact: false };
      if (s.by === "role") return page.getByRole(s.role, name);
      if (s.by === "label") return page.getByLabel(s.target, { exact: false });
      if (s.by === "text") return page.getByText(s.target, { exact: false });
      if (s.by === "placeholder") return page.getByPlaceholder(s.target, { exact: false });
      if (s.by === "alt") return page.getByAltText(s.target, { exact: false });
      return page.locator(s.target);
    };
    const KEYS = { Space: " " };
    for (const check of (expect.version === 2 ? expect.checks : []) || []) {
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message.slice(0, 200)));
      const result = { id: String(check.id), ok: false, failed_step: null, detail: "", errors };
      let index = -1;
      try {
        await page.goto(origin + check.start, { waitUntil: "networkidle", timeout: 30_000 });
        for (const [i, s] of (check.steps || []).entries()) {
          index = i;
          const fail = (why) => { throw new Error(why); };
          const one = () => locate(page, s).first();
          if (s.action === "goto") await page.goto(origin + s.value, { waitUntil: "networkidle", timeout: 30_000 });
          else if (s.action === "click") await one().click({ timeout: 5_000 });
          else if (s.action === "fill") await one().fill(s.value, { timeout: 5_000 });
          else if (s.action === "select") await one().selectOption({ label: s.value }, { timeout: 5_000 }).catch(() => one().selectOption(s.value, { timeout: 5_000 }));
          else if (s.action === "check") await one().check({ timeout: 5_000 });
          else if (s.action === "press") await page.keyboard.press(KEYS[s.value] || s.value);
          else if (s.action === "expect_visible") { if (!(await one().isVisible())) await one().waitFor({ state: "visible", timeout: 5_000 }).catch(() => fail("it is not visible")); }
          else if (s.action === "expect_hidden") { await one().waitFor({ state: "hidden", timeout: 5_000 }).catch(() => fail("it is still visible")); }
          else if (s.action === "expect_text") { await page.getByText(s.value, { exact: false }).first().waitFor({ state: "visible", timeout: 5_000 }).catch(() => fail("that text is not visible")); }
          else if (s.action === "expect_no_text") { await page.waitForTimeout(300); if (await page.getByText(s.value, { exact: false }).first().isVisible()) fail("that text is visible"); }
          else if (s.action === "expect_url") {
            const deadline = Date.now() + 5_000;
            const here = () => { const u = new URL(page.url()); return u.pathname + u.search + u.hash; };
            while (!(here() === s.value || here().endsWith(s.value) || (s.value.startsWith("/#") && new URL(page.url()).hash === s.value.slice(1))) && Date.now() < deadline) await page.waitForTimeout(100);
            if (!(here() === s.value || here().endsWith(s.value) || (s.value.startsWith("/#") && new URL(page.url()).hash === s.value.slice(1)))) fail(`the page is ${here()}`);
          } else if (s.action === "expect_title") { if (!(await page.title()).toLowerCase().includes(s.value.toLowerCase())) fail(`the title is ${JSON.stringify(await page.title())}`); }
          else if (s.action === "expect_count") {
            const deadline = Date.now() + 5_000;
            let n = await locate(page, s).count();
            while (n < s.count && Date.now() < deadline) { await page.waitForTimeout(150); n = await locate(page, s).count(); }
            if (n < s.count) fail(`found ${n}`);
          } else fail(`unknown action ${s.action}`);
          if (s.action === "click" || s.action === "press") await page.waitForTimeout(250);
        }
        index = -1;
        result.ok = errors.length === 0;
        result.detail = errors.length ? "the page threw an uncaught error" : "passed";
      } catch (e) {
        result.failed_step = index >= 0 ? index : null;
        result.detail = String(e.message || e).split("\n")[0].replace(/\s+/g, " ").slice(0, 300);
      }
      results.push(result);
      await page.close();
    }
    report.acceptance = results;
    const failedChecks = results.filter((r) => !r.ok);
    stage("acceptance", failedChecks.length === 0 && results.length > 0,
      results.length === 0 ? "no acceptance checks were given" : failedChecks.length ? failedChecks.map((r) => `${r.id}: ${r.detail}`).join("\n") : `${results.length} acceptance check(s) passed`,
      { ran: results.length });
  }
} finally {
  await browser.close();
  server.close();
}
finish();
