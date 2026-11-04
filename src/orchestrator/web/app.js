"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let campaigns = [];
let defaults = {};
let selected = new URLSearchParams(location.search).get("campaign");
let current = null;
let selectedRound = null;
let pendingJob = null;
let launchError = null;
let sidebarSignature = "";
let campaignSignature = "";

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-Session-Token": token},
    body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Request failed");
  return value;
}

function badge(value) {
  const good = ["passed", "complete", "accepted", "improved", "observed_compliance"];
  const bad = ["failed", "error", "regressed", "observed_violation", "invalid_evaluator"];
  return node("span", String(value || "pending").replaceAll("_", " "),
    "badge " + (good.includes(value) ? "good" : bad.includes(value) ? "bad" : ""));
}

function empty(container, message) {
  container.append(node("p", message, "empty"));
}

function banner(message, error = false) {
  $("banner").hidden = !message;
  $("banner").textContent = message;
  $("banner").className = error ? "error" : "";
}

function selectCampaign(id) {
  launchError = null;
  selected = id;
  selectedRound = null;
  history.replaceState({}, "", "?campaign=" + encodeURIComponent(id));
  refresh();
}

function renderSidebar() {
  const signature = JSON.stringify([selected, campaigns]);
  if (signature === sidebarSignature) return;
  sidebarSignature = signature;
  $("campaign-list").replaceChildren();
  for (const card of campaigns) {
    const button = node("button", null, "campaign-item" + (card.id === selected ? " selected" : ""));
    button.append(node("b", card.runtime), node("small", (card.bundled ? "Example · " : "") + (card.stop_reason || card.phase).replaceAll("_", " ") + " · round " + (card.round || "—")));
    button.title = card.target + " · " + card.revision.slice(0, 8);
    button.onclick = () => selectCampaign(card.id);
    $("campaign-list").append(button);
  }
  if (!campaigns.length) empty($("campaign-list"), "Your campaigns will appear here.");
}

const phases = [["source", "Inspect source"], ["generation", "Generate"], ["preflight", "Validate"], ["review", "Review"], ["execution", "Execute"], ["analysis", "Analyze"]];
const descriptions = {
  source: "Preparing the pinned source checkout and its architecture index.",
  generation: "The orchestrator inspects source, proposes scenarios, and edits the active suite.",
  preflight: "Checking scenario contracts, authored reference actions, and evaluator tests.",
  review: "Independent reviewers examine task quality, experimental validity, and verification.",
  execution: "Running the active suite and recording requests, tool actions, state, and checks.",
  analysis: "Comparing trajectories, attributing contributing failures, and deciding what to test next.",
  terminal: "This attempt has reached its recorded stopping point.",
};

function renderCampaign(detail) {
  const signature = JSON.stringify([selected, detail]);
  if (signature === campaignSignature) return;
  campaignSignature = signature;
  const expanded = new Set(Array.from(document.querySelectorAll("details[data-key][open]"), (item) => item.dataset.key));
  current = detail;
  const card = campaigns.find((item) => item.id === selected);
  if (!card) return;
  $("welcome").hidden = true;
  $("campaign").hidden = false;
  $("campaign-repository").textContent = card.repository.replace(/^https?:\/\//, "").replace(/\.git$/, "");
  $("campaign-title").textContent = card.runtime[0].toUpperCase() + card.runtime.slice(1) + " / " + (detail.source.base_revision ? "Version regression" : "Property exploration");
  $("campaign-models").textContent = `${card.orchestrator} → ${card.target} · ${card.revision.slice(0, 10)}`;
  const phase = detail.progress.phase || "saved";
  $("stop").hidden = ["terminal", "saved"].includes(phase);
  $("phase-strip").replaceChildren(...phases.map(([key, title], index) => {
    const element = node("div", null, "phase" + (phase === key ? " active" : ""));
    element.append(node("span", "0" + (index + 1)), node("b", title));
    return element;
  }));
  const last = detail.rounds.at(-1);
  const trusted = last?.lanes?.trusted;
  const values = [
    ["EXECUTION ROUND", `${detail.progress.round || last?.number || 0} / ${detail.limits.rounds || 3}`, "All active tests rerun each round"],
    ["ACTIVE SCENARIOS", detail.tests.length, `Up to ${detail.limits.active_tests || 12} tests`],
    ["TRUSTED EVALUATOR", trusted ? `${trusted.passed} / ${trusted.total}` : "—", trusted ? "Passed / branch phases in latest round" : "Awaiting target execution"],
    ["ORCHESTRATOR REQUESTS", detail.requests, "Includes main agent and reviewers"],
  ];
  $("stats").replaceChildren(...values.map(([label, value, note]) => {
    const stat = node("div", null, "stat");
    stat.append(node("div", label, "label"), node("strong", value), node("small", note));
    return stat;
  }));
  $("activity-status").textContent = (detail.progress.role || detail.progress.lane || phase).replaceAll("_", " ");
  $("phase-description").textContent = descriptions[phase] || "Saved evidence from an earlier campaign.";
  $("activity").replaceChildren(...detail.activity.slice().reverse().map((event) => {
    const row = node("div", null, "activity-event");
    row.append(node("b", event.kind), node("span", event.subject));
    return row;
  }));
  if (!detail.activity.length) empty($("activity"), phase === "source" ? "Inspecting repository metadata…" : "Waiting for the next recorded action.");
  $("iterations").replaceChildren();
  for (const round of detail.rounds) {
    const row = node("div", null, "iteration");
    const heading = node("div", null, "iteration-title");
    heading.append(node("b", `Round ${round.number}`), node("span", `${round.generations.length} generation${round.generations.length === 1 ? "" : "s"}`, "muted"));
    row.append(heading);
    for (const [name, lane] of Object.entries(round.lanes)) {
      const result = node("div", null, "lane");
      const track = node("div", null, "track");
      const fill = node("div", null, "fill");
      fill.style.width = `${lane.total ? lane.passed / lane.total * 100 : 0}%`;
      track.append(fill);
      result.append(node("span", name.replaceAll("_", " ")), track, node("b", `${lane.passed}/${lane.total} phases`), badge(lane.status));
      row.append(result);
      if (lane.audit?.passed === false) row.append(node("p", "Evidence audit failed. Interpret this lane with its recorded validity issues.", "error"));
      if (lane.cases.length) {
        const cases = node("details");
        cases.dataset.key = `cases-${selected}-${round.number}-${name}`;
        cases.open = expanded.has(cases.dataset.key);
        cases.append(node("summary", name.replaceAll("_", " ") + " · case outcomes"));
        for (const item of lane.cases) {
          const line = node("p", item.id + " · ");
          line.append(badge(item.status));
          for (const [condition, status] of Object.entries(item.branches)) {
            line.append(document.createTextNode(" · " + condition + " "), badge(status));
          }
          cases.append(line);
        }
        row.append(cases);
      }
    }
    if (!Object.keys(round.lanes).length) row.append(node("span", "Design and review in progress", "muted"));
    $("iterations").append(row);
  }
  if (!detail.rounds.length) empty($("iterations"), "The first generation starts after source preparation.");
  renderTests(detail.tests);
  $("round-select").replaceChildren(...detail.rounds.map((round) => new Option("Round " + round.number, round.number)));
  selectedRound = selectedRound || last?.number;
  $("round-select").value = selectedRound || "";
  renderReviews();
  for (const section of document.querySelectorAll("details[data-key]")) section.open = expanded.has(section.dataset.key);
  const usage = detail.usage.reported_tokens || {};
  const tokens = usage.input_tokens !== undefined ? ` · Orchestrator tokens ${Number(usage.input_tokens).toLocaleString()} in / ${Number(usage.output_tokens).toLocaleString()} out` : "";
  $("evidence-path").textContent = (detail.bundled ? "Bundled dashboard snapshot · " : "Evidence · ") + detail.path + tokens;
  if (launchError) banner(launchError, true);
  else if (detail.error) banner(detail.error, true);
  else if (phase === "terminal") banner("Campaign stopped · " + (detail.stop_reason || "unknown").replaceAll("_", " ") + (detail.preemption_reason ? " · " + detail.preemption_reason : ""));
  else if (detail.stop_requested) banner("Stop requested. Waiting for the current request or execution stage to finish.");
  else if (!pendingJob) banner("");
}

function renderTests(tests) {
  $("test-count").textContent = tests.length + " scenarios";
  $("test-grid").replaceChildren();
  for (const test of tests) {
    const card = node("button", null, "test-card");
    card.append(badge(test.complexity?.family || "property test"), node("h3", test.name || test.id), node("p", test.turns?.[0]?.prompt || test.property_tested));
    const chips = node("div", null, "chips");
    chips.append(node("span", `${test.turns?.length || 0} turns`), node("span", `${test.complexity?.input_files || 0} input files`));
    if (test.experiment) chips.append(node("span", "Controlled pair"));
    if (test.complexity?.dependency_depth) chips.append(node("span", "Depth " + test.complexity.dependency_depth));
    card.append(chips);
    card.onclick = () => showTest(test);
    $("test-grid").append(card);
  }
  if (!tests.length) empty($("test-grid"), "Scenarios appear as the orchestrator writes them.");
}

function renderReviews() {
  const round = current?.rounds.find((value) => value.number === Number(selectedRound));
  $("reviews").replaceChildren();
  $("analysis").replaceChildren();
  $("earlier-reviews").replaceChildren();
  if (!round) return;
  const generation = round.generations.at(-1);
  for (const role of ["task_quality", "experimental_validity", "verification_attribution"]) {
    const review = generation?.reviews.find((value) => value.role === role);
    const card = node("article", null, "review-card");
    card.append(node("h3", role.replaceAll("_", " ")), badge(review ? (review.accepted ? "accepted" : "revision requested") : "pending"));
    const list = node("ul");
    for (const issue of review?.issues || []) list.append(node("li", issue.description));
    card.append(list);
    if (review?.accepted) card.append(node("p", "Reviewer accepted this generation.", "hint"));
    $("reviews").append(card);
  }
  for (const earlier of round.generations.slice(0, -1)) {
    const section = node("details");
    section.dataset.key = `reviews-${selected}-${round.number}-${earlier.number}`;
    section.append(node("summary", `Generation ${earlier.number} · earlier review findings`));
    for (const review of earlier.reviews) {
      section.append(node("h3", review.role.replaceAll("_", " ")));
      for (const issue of review.issues || []) section.append(node("p", issue.description));
    }
    $("earlier-reviews").append(section);
  }
  renderFindings(round);
  const analysis = round.analysis;
  if (analysis.convergence_reason) {
    const decision = node("details", null, "decision");
    decision.dataset.key = `decision-${selected}-${round.number}`;
    decision.append(node("summary", analysis.converged ? "Why the campaign converged" : "Why another iteration is needed"), node("p", analysis.convergence_reason));
    $("analysis").append(decision);
  }
  const differential = round.differential;
  if (differential.table?.length) {
    $("analysis").append(node("h3", "Framework version comparison"));
    for (const row of differential.table) {
      const line = node("p", `${row.id} / ${row.condition} · `);
      line.append(badge(row.verdict));
      $("analysis").append(line);
    }
  }
}

const contributorNames = {
  model: "Model", serving_protocol: "Serving protocol", hermes_runtime: "Hermes runtime",
  agent_runtime: "Agent runtime", mock_service: "Mock service", harness: "Harness",
  verifier: "Verifier", infrastructure: "Infrastructure", unresolved: "Unresolved",
};

function renderFindings(round) {
  const result = round.findings || {status: "awaiting_analysis"};
  $("findings-count").textContent = result.status === "ready" ? `${result.presentation.cards.length} findings` : result.status.replaceAll("_", " ");
  if (result.status !== "ready") {
    const message = result.status === "awaiting_analysis"
      ? "Findings appear after the agent completes its tests and the orchestrator analyzes the results."
      : result.status === "error" ? "The findings presentation could not be prepared."
        : `Preparing readable findings with ${result.model || "the orchestrator"}…`;
    empty($("analysis"), message);
    if (result.status === "error") {
      $("analysis").append(node("p", result.error, "error"));
      const retry = node("button", "Retry findings", "secondary");
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          await api("/api/findings/retry", {id: selected, round: round.number});
          await refresh();
        } catch (error) { banner(error.message, true); retry.disabled = false; }
      };
      $("analysis").append(retry);
    }
    return;
  }
  $("analysis").append(node("p", result.presentation.overview, "findings-overview"));
  const grid = node("div", null, "findings-grid");
  for (const [index, card] of result.presentation.cards.entries()) {
    const article = node("article", null, "finding-card");
    const sources = card.source_ids.map((id) => result.sources[id]);
    const header = node("div", null, "finding-heading");
    header.append(node("span", String(index + 1).padStart(2, "0"), "finding-number"), node("h3", card.title));
    article.append(header);
    const tags = node("div", null, "chips");
    const outcomes = [...new Set(sources.map((source) => source.property_outcome).filter(Boolean))];
    for (const outcome of outcomes) tags.append(badge(outcome));
    for (const certainty of new Set(sources.map((source) => source.certainty))) tags.append(badge(certainty));
    article.append(tags);
    for (const [label, text] of [["What happened", card.observation], ["Why it happened", card.causal_explanation], ["What this tells us", card.practical_implication]]) {
      const section = node("div", null, "finding-section");
      section.append(node("h4", label), node("p", text));
      article.append(section);
    }
    const owners = [...new Set(sources.flatMap((source) => source.contributors || []))];
    if (owners.length) article.append(node("div", "Recorded contributors · " + owners.map((owner) => contributorNames[owner] || owner).join(" · "), "finding-contributors"));
    if (card.limitations.length) {
      const limits = node("div", null, "finding-limits");
      limits.append(node("h4", "Limits of the evidence"));
      const list = node("ul");
      for (const text of card.limitations) list.append(node("li", text));
      limits.append(list); article.append(limits);
    }
    const evidence = node("details", null, "finding-evidence");
    evidence.dataset.key = `evidence-${selected}-${round.number}-${index}`;
    evidence.append(node("summary", `Evidence & original findings (${sources.length})`));
    for (const source of sources) {
      const record = node("div", null, "source-finding");
      record.append(node("b", source.scenario_id || "Unresolved evidence gap"), node("p", source.claim));
      const context = [source.property_outcome, source.certainty, ...(source.contributors || [])].filter(Boolean);
      record.append(node("p", context.map((text) => text.replaceAll("_", " ")).join(" · "), "hint"));
      const outcomes = [source.task_outcome && `Task · ${source.task_outcome}`, source.challenge_exposure && `Challenge · ${source.challenge_exposure}`].filter(Boolean);
      if (outcomes.length) record.append(node("p", outcomes.join(" / ").replaceAll("_", " "), "hint"));
      if (source.evidence_refs?.length) record.append(node("code", source.evidence_refs.join(" · ")));
      record.append(node("small", "analysis.json · " + source.analysis_location));
      evidence.append(record);
    }
    article.append(evidence);
    grid.append(article);
  }
  $("analysis").append(grid);
  const usage = result.usage || {};
  const tokens = usage.reported_tokens;
  const usageText = tokens ? ` · ${Number(tokens.input_tokens).toLocaleString()} input / ${Number(tokens.output_tokens).toLocaleString()} output tokens reported${usage.usage_missing?.length ? " · some usage unavailable" : ""}` : "";
  $("analysis").append(node("p", `Presentation by ${result.model} · ${usage.requests || 0} additional API requests${usageText}`, "hint presentation-usage"));
}

function showTest(test) {
  $("test-title").textContent = test.name || test.id;
  $("test-detail").replaceChildren(node("p", test.property_tested || "", "muted"));
  if (test.experiment) $("test-detail").append(node("p", `Baseline / treatment branches begin at turn ${test.experiment.branch_turn}.`, "hint"));
  for (const turn of test.turns || []) {
    const section = node("section", null, "turn");
    section.append(node("h3", "Turn " + turn.turn_number), node("div", turn.prompt, "request"));
    const table = node("table", null, "check-table");
    const header = node("tr");
    for (const text of ["Check group", "Assertion", "Target / expected"]) header.append(node("th", text));
    const head = node("thead"); head.append(header); table.append(head);
    const body = node("tbody");
    for (const group of ["state", "process", "output"]) {
      for (const check of turn[group + "_checks"] || []) {
        const row = node("tr");
        const details = Object.fromEntries(Object.entries(check).filter(([key]) => key !== "type"));
        row.append(node("td", group), node("td", check.type), node("td", JSON.stringify(details)));
        body.append(row);
      }
    }
    table.append(body); section.append(table);
    if (turn.pre_turn_perturbations?.length) {
      const faults = node("details");
      faults.append(node("summary", "Setup and perturbations"), node("pre", JSON.stringify(turn.pre_turn_perturbations, null, 2)));
      section.append(faults);
    }
    $("test-detail").append(section);
  }
  const raw = node("details");
  raw.append(node("summary", "Full scenario contract"), node("pre", JSON.stringify(test, null, 2)));
  $("test-detail").append(raw);
  $("test-dialog").showModal();
}

function updateSetup() {
  const runtime = $("runtime").value;
  $("source").value = defaults.repositories?.[runtime] || "";
  $("python").value = defaults.runtimes?.[runtime]?.python || defaults.python || "";
  $("runtime-image").value = defaults.runtimes?.[runtime]?.runtime_image || "";
  $("image-label").hidden = runtime === "hermes";
  $("base-revision").disabled = runtime !== "hermes";
  $("approval").disabled = runtime !== "hermes";
  updatePrior();
}

function updatePrior() {
  const normalize = (value) => value.replace(/\.git$/, "").replace(/\/$/, "");
  const source = normalize($("source").value.trim());
  const runtime = $("runtime").value;
  const matches = campaigns.filter((card) => !card.bundled && card.runtime === runtime && (!source.startsWith("http") || normalize(card.repository) === source));
  $("prior").replaceChildren(new Option("Start a new test suite", ""));
  for (const card of matches) $("prior").append(new Option(`${card.revision.slice(0, 10)} · ${card.target} · ${card.stop_reason || card.phase}`, card.id));
  if (matches.length) $("prior").value = matches[0].id;
}

function openSetup() {
  $("form-error").hidden = true;
  updateSetup();
  $("credentials").textContent = defaults.meta_key_present ? "META_KEY is configured. Credentials are read from .env or your shell." : "Add META_KEY to the repository .env before launching a Meta campaign.";
  $("setup").showModal();
}

function updateModelSummary() {
  $("orch-summary").textContent = $("orch-model").value;
  $("target-summary").textContent = $("target-model").value;
}

$("setup-form").onsubmit = async (event) => {
  event.preventDefault();
  $("submit").disabled = true;
  $("form-error").hidden = true;
  const model = (prefix) => ({base_url: $(prefix + "-url").value, model: $(prefix + "-model").value, api_key_env: $(prefix + "-key").value});
  try {
    const result = await api("/api/start", {
      runtime: $("runtime").value, source: $("source").value, revision: $("revision").value,
      python: $("python").value, runtime_image: $("runtime-image").value,
      orchestrator: model("orch"), target: model("target"), scope: $("scope").value,
      limits: {rounds: Number($("rounds").value), generations: Number($("generations").value), initial_tests: Number($("initial-tests").value), active_tests: Number($("active-tests").value)},
      prior_campaign: $("prior").value || null, base_revision: $("base-revision").value,
      hermes_approval: $("approval").value, execute: $("execute").checked,
    });
    pendingJob = result.id;
    launchError = null;
    $("setup").close();
    banner("Campaign launched. Cloning and pinning the source checkout…");
    await refresh();
  } catch (error) {
    $("form-error").textContent = error.message;
    $("form-error").hidden = false;
  } finally { $("submit").disabled = false; }
};

$("stop").onclick = async () => {
  try {
    await api("/api/stop", {id: selected});
    banner("Stop requested. The current request or execution stage will finish before the campaign stops.");
  } catch (error) { banner(error.message, true); }
};
$("new-campaign").onclick = openSetup;
$("welcome-start").onclick = openSetup;
$("runtime").onchange = updateSetup;
$("source").onchange = updatePrior;
$("orch-model").oninput = updateModelSummary;
$("target-model").oninput = updateModelSummary;
$("round-select").onchange = () => { selectedRound = Number($("round-select").value); renderReviews(); };
for (const button of document.querySelectorAll(".close-dialog")) button.onclick = () => button.closest("dialog").close();

async function refresh() {
  try {
    const data = await api("/api/campaigns");
    campaigns = data.campaigns;
    if (pendingJob) {
      const job = data.jobs.find((item) => item.id === pendingJob);
      if (job?.status === "error") { launchError = job.error; banner(launchError, true); pendingJob = null; }
      else if (job) {
        const newest = campaigns.find((item) => item.id === job.campaign_id);
        if (newest) { selected = newest.id; pendingJob = null; }
      }
    }
    if (!campaigns.some((item) => item.id === selected)) {
      selected = campaigns[0]?.id || null;
      selectedRound = null;
      history.replaceState({}, "", selected ? "?campaign=" + encodeURIComponent(selected) : location.pathname);
      $("welcome").hidden = Boolean(selected);
      $("campaign").hidden = !selected;
    }
    renderSidebar();
    if (selected) {
      const requested = selected;
      const detail = await api("/api/campaign/" + requested);
      if (selected === requested) renderCampaign(detail);
    }
    $("connection").textContent = "● Live · " + new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  } catch (error) { $("connection").textContent = "Connection interrupted"; banner(error.message, true); }
}

async function initialize() {
  try { defaults = await api("/api/defaults"); } catch (error) { banner(error.message, true); }
  await refresh();
  // Schedule after completion so slow evidence reads never overlap.
  async function poll() { await refresh(); setTimeout(poll, 4000); }
  setTimeout(poll, 4000);
}
initialize();
