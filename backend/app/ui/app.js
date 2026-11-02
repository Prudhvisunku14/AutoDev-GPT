"use strict";
// DevPilot's read-only dashboard. It only reads the local API with the user's token. Every value from the API
// goes into the page as text (textContent), never as HTML, because generated code and reports are untrusted.

const API = "/api/v1";
const TOKEN_KEY = "devpilot-token";
const ACTIVE_POLL_MS = 2000;
let token = null;
let current = null; // {project, run}
let pollTimer = null;

// --- Small DOM helpers ------------------------------------------------------------------------------------------

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function badge(text, tone) {
  return el("span", { class: `badge ${tone || ""}` }, text);
}

function tone(value) {
  const v = String(value || "").toUpperCase();
  if (["COMPLETED", "PASSED", "KEPT", "VERIFIED"].includes(v)) return "ok";
  if (["FAILED", "CANCELLED", "STOP", "POLICY_VIOLATION", "QUARANTINED"].includes(v)) return "bad";
  if (["NOT_RUN", "UNVERIFIED", "REPAIR", "RETRY", "SWITCH_TOOL", "RECONFIGURE", "RESTORED"].includes(v)) return "warn";
  return "info";
}

function section(title, ...body) {
  return el("section", { class: "card" }, el("h3", {}, title), ...body);
}

function table(headers, rows) {
  return el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((c) => el("td", {}, c)))))));
}

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

// --- The API ----------------------------------------------------------------------------------------------------

class Unauthorized extends Error {}

async function api(path) {
  const answer = await fetch(API + path, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
  if (answer.status === 401) throw new Unauthorized();
  if (answer.status === 404) return null;
  if (!answer.ok) throw new Error(`${path}: HTTP ${answer.status}`);
  return answer.json();
}

async function allEvents(runId) {
  const events = [];
  let after = 0;
  for (;;) {
    const page = (await api(`/runs/${runId}/events?after_seq=${after}&limit=500`)) || [];
    events.push(...page);
    if (page.length < 500) return events;
    after = page[page.length - 1].seq;
  }
}

// --- The run list -----------------------------------------------------------------------------------------------

async function loadRuns() {
  setStatus("Loading...");
  const projects = (await api("/projects?limit=200")) || [];
  const entries = [];
  for (const project of projects) {
    for (const run of (await api(`/projects/${project.id}/runs`)) || []) entries.push({ project, run });
  }
  entries.sort((a, b) => String(b.run.created_at).localeCompare(String(a.run.created_at)));
  const list = document.getElementById("run-list");
  list.replaceChildren(...entries.map((entry) => el("li", {}, el("button", {
    type: "button", "data-run": entry.run.id, "aria-current": current && current.run.id === entry.run.id ? "true" : "false",
    onclick: () => showRun(entry.project, entry.run).catch(failed),
  }, el("span", { class: "name" }, entry.project.name), el("span", {}, badge(entry.run.state, tone(entry.run.state)), " ", el("span", { class: "muted" }, entry.run.outcome || ""))))));
  if (!entries.length) list.replaceChildren(el("li", { class: "muted" }, "No runs yet."));
  setStatus(`${entries.length} run(s)`);
}

// --- One run ----------------------------------------------------------------------------------------------------

async function showRun(project, run) {
  current = { project, run };
  clearTimeout(pollTimer);
  const [fresh, plan, report, events] = await Promise.all([api(`/runs/${run.id}`), api(`/runs/${run.id}/plan`), api(`/runs/${run.id}/report`), allEvents(run.id)]);
  render(project, fresh || run, plan, report, events);
  document.querySelectorAll("#run-list button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.run === run.id)));
  const active = fresh && !fresh.ended_at && !fresh.pause_reason;
  if (active) pollTimer = setTimeout(() => showRun(project, fresh).catch(failed), ACTIVE_POLL_MS);
  setStatus(active ? "Run in progress: refreshing every 2 s" : "");
}

function failed(error) {
  if (error instanceof Unauthorized) {
    sessionStorage.removeItem(TOKEN_KEY);
    showLogin();
  } else {
    setStatus(`Could not load: ${error.message}`);
  }
}

function byType(events, type) {
  return events.filter((e) => e.type === type).map((e) => e.payload);
}

function render(project, run, plan, report, events) {
  const simulated = report ? report.provenance.simulated : simulatedFromEvents(events);
  const detail = document.getElementById("detail");
  detail.replaceChildren(
    el("section", { class: "card" },
      el("h2", {}, project.name),
      el("p", {}, badge(run.state, tone(run.state)), " ", run.outcome ? el("span", { class: "muted" }, `outcome: ${run.outcome}`) : ""),
      run.status_detail ? el("p", { class: "muted" }, run.status_detail) : "",
      el("p", {}, el("strong", {}, "Requirement: "), project.requirement_text)),
    simulated.length
      ? el("div", { class: "banner", role: "note" }, el("strong", {}, "Simulated in this run: "), simulated.join("; "), ". Model replies are scripted (FakeLLM); no AI model was called.")
      : el("div", { class: "banner", role: "note" }, "Nothing in this run's report is marked as simulated."),
    el("div", { class: "grid2" }, planSection(plan, report), selectionSection(byType(events, "selection.chosen"), byType(events, "recovery.decided"))),
    verificationSection(byType(events, "verification.finished"), report ? report.acceptance : null, byType(events, "acceptance.finished")),
    recoverySection(byType(events, "recovery.decided"), byType(events, "coding.finished")),
    timelineSection(events),
    deliverySection(project, report),
  );
}

function simulatedFromEvents(events) {
  const chosen = byType(events, "selection.chosen");
  return chosen.some((c) => c.simulated) ? ["coding provider (from the selection events; the final report is not written yet)"] : [];
}

function planSection(plan, report) {
  const tasks = report ? report.tasks : plan ? plan.tasks.map((t) => ({ order: t.order, title: t.title, status: t.status || "" })) : [];
  return section("Task plan",
    plan ? el("p", { class: "muted" }, `Stack ${plan.stack}. ${plan.summary || ""}`) : el("p", { class: "muted" }, "No plan yet."),
    el("ol", { class: "steps" }, tasks.map((t) => el("li", {}, `${t.title} `, t.status ? badge(t.status, tone(t.status === "DONE" ? "PASSED" : t.status)) : ""))));
}

function selectionSection(choices, decisions) {
  const refusedOutright = decisions.filter((d) => d.failure_class === "NO_ELIGIBLE_TOOL");
  if (!choices.length && !refusedOutright.length) return section("Tool selection", el("p", { class: "muted" }, "No tool chosen yet."));
  const blocks = choices.map((c) => el("div", {},
    el("p", {}, `Task ${c.task}: `, el("strong", {}, c.provider), " ", badge(c.simulated ? "simulated" : "real", c.simulated ? "warn" : "ok"), ` (${c.policy || "fixed"} policy)`),
    c.explanation ? el("p", { class: "muted" }, c.explanation) : "",
    c.ranking && c.ranking.length ? table(["Eligible tool", "Score"], c.ranking.map((r) => [r.tool, r.score.toFixed(3)])) : "",
    c.rejected && c.rejected.length ? el("ul", { class: "plain" }, c.rejected.map((r) => el("li", {}, el("strong", {}, `${r.tool}: `), r.reasons.join("; ")))) : ""));
  const refusals = refusedOutright.map((d) => el("div", {}, el("p", {}, badge("NO ELIGIBLE TOOL", "bad"), " Nothing was started."), el("ul", { class: "plain" }, (d.evidence || []).map((e) => el("li", {}, e)))));
  return section("Tool selection: who writes the code, and why", ...blocks, ...refusals);
}

function verificationSection(checks, acceptance, acceptanceRuns) {
  const replayed = checks.some((c) => String(c.container || "").startsWith("replay:"))
    ? el("p", {}, badge("replayed", "warn"), " These test results are recorded runs of these exact files, not a Docker execution.")
    : "";
  const own = checks.length
    ? table(["Run", "Provider's tests", "Passed", "Failed tests", "Reason"], checks.map((c, i) => [String(i + 1), badge(c.verdict, tone(c.verdict)), `${c.passed} of ${c.ran}`, (c.failed_names || []).join(", ") || "none", c.reason]))
    : el("p", { class: "muted" }, "The provider's tests have not run.");
  let independent;
  if (!acceptance && !acceptanceRuns.length) independent = el("p", { class: "muted" }, "Independent acceptance: not run yet.");
  else if (acceptance && !acceptance.configured) independent = el("p", { class: "muted" }, acceptance.note);
  else {
    const last = acceptance && acceptance.criteria && acceptance.criteria.length ? acceptance : acceptanceRuns[acceptanceRuns.length - 1] || acceptance;
    independent = el("div", {},
      el("p", {}, el("strong", {}, "Independent acceptance (the evaluator's frozen suite, never shown to the coding provider): "),
        badge(last.verdict, tone(last.verdict)), ` ${last.passed ?? 0} of ${last.total ?? 0} criteria. `, el("span", { class: "muted" }, last.reason || "")),
      acceptanceRuns.length > 1 ? el("p", { class: "muted" }, `Acceptance runs: ${acceptanceRuns.map((a) => `${a.verdict} (${a.passed} of ${a.total})`).join(", then ")}`) : "",
      last.criteria && last.criteria.length ? table(["Criterion", "Description", "Result", "Evidence"], last.criteria.map((c) => [c.code, c.description, badge(c.status, tone(c.status)), c.evidence ? el("pre", {}, c.evidence) : ""])) : "",
      last.suite_sha256 ? el("p", { class: "muted" }, `Suite SHA-256 ${last.suite_sha256}`) : "");
  }
  return section("Verification", replayed, own, independent);
}

function recoverySection(decisions, attempts) {
  const rows = attempts.map((a) => [`task ${a.task_order}`, a.provider, badge(a.status, tone(a.status)), a.kept ? "kept" : "undone", a.phase, (a.violations || []).join("; ")]);
  return section("Coding attempts, failure classification and recovery",
    rows.length ? table(["Task", "Provider", "Status", "Result", "Phase", "Audit"], rows) : el("p", { class: "muted" }, "No coding attempt yet."),
    decisions.length
      ? table(["Failure", "Confidence", "Decision", "Why", "Evidence"], decisions.map((d) => [d.category || d.failure_class, d.confidence || "", badge(d.action, tone(d.action)), d.reason, (d.evidence || [])[0] || ""]))
      : el("p", { class: "muted" }, "No recovery decision was needed."));
}

function timelineSection(events) {
  return section("Progress", el("ol", { class: "timeline" }, events.map((e) => el("li", {}, el("span", { class: "seq" }, `#${e.seq}`), el("span", {}, summarise(e))))));
}

function summarise(event) {
  const p = event.payload || {};
  switch (event.type) {
    case "run.state_changed": return `state ${p.from || "?"} -> ${p.to || "?"}${p.reason ? `: ${p.reason}` : ""}`;
    case "tool.called": return `GPT called ${p.tool || p.name || "a tool"}`;
    case "tool.result": return `${p.tool || "tool"} returned ${p.ok === false ? "an error" : "a result"}`;
    case "llm.response": return `GPT replied${p.function_calls && p.function_calls.length ? `: ${p.function_calls.join(", ")}` : ""}`;
    case "selection.chosen": return `selected ${p.provider} for task ${p.task}${p.simulated ? " (simulated)" : ""}`;
    case "coding.started": return `coding started on task ${p.task_order ?? p.task ?? "?"}`;
    case "coding.prompt_sent": return "the brief was sent to the coding provider (once)";
    case "coding.finished": return `coding finished: ${p.status}, ${p.kept ? "kept" : "undone"}`;
    case "verification.started": return `the provider's tests started${p.independent_acceptance ? ", with the acceptance suite after" : ""}`;
    case "verification.finished": return `the provider's tests: ${p.verdict}, ${p.passed} of ${p.ran} passed`;
    case "acceptance.finished": return `independent acceptance: ${p.verdict}, ${p.passed} of ${p.total} criteria`;
    case "recovery.decided": return `recovery: ${p.category || p.failure_class} -> ${p.action}`;
    case "recovery.outcome": return `recovery outcome: ${p.action} ${p.resolved ? "resolved it" : "did not resolve it"}`;
    case "delivery.finished": return `delivered ${(p.files || []).length} file(s) into the project`;
    default: return event.type;
  }
}

function deliverySection(project, report) {
  if (!report) return section("Generated files and report", el("p", { class: "muted" }, "The final report is written when the run ends."));
  const viewer = el("div", {});
  const files = report.delivered_files || [];
  const showFile = async (path) => {
    const file = await api(`/projects/${project.id}/files/content?path=${encodeURIComponent(path)}`);
    viewer.replaceChildren(el("p", { class: "muted" }, path), el("pre", {}, file ? file.content : "This file is not in the project."));
  };
  return section("Generated files and report",
    files.length
      ? el("div", { class: "files" }, files.map((f) => el("button", { type: "button", onclick: () => showFile(f.path) }, `${f.path} (${f.change}, ${f.bytes} bytes)`)))
      : el("p", { class: "muted" }, "Nothing was delivered. The user's project was not changed."),
    el("p", {}, el("button", { type: "button", onclick: () => viewer.replaceChildren(el("pre", {}, JSON.stringify(report, null, 2))) }, "Show the full report (JSON)")),
    viewer);
}

// --- Start ------------------------------------------------------------------------------------------------------

function showLogin() {
  document.getElementById("app").hidden = true;
  document.getElementById("login").hidden = false;
}

async function start() {
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("token");
  if (fromHash) {
    sessionStorage.setItem(TOKEN_KEY, fromHash);
    history.replaceState(null, "", location.pathname); // the token leaves the address bar
  }
  token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) return showLogin();
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  try {
    await loadRuns();
  } catch (error) {
    if (error instanceof Unauthorized) {
      sessionStorage.removeItem(TOKEN_KEY);
      return showLogin();
    }
    setStatus(`Could not load runs: ${error.message}`);
  }
}

document.getElementById("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem(TOKEN_KEY, document.getElementById("token").value.trim());
  start();
});
document.getElementById("refresh").addEventListener("click", () => (current ? showRun(current.project, current.run).then(loadRuns) : loadRuns()).catch(failed));
start();
