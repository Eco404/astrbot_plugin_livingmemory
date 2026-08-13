import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { UserProfileMaintenance } from "../../pages/dashboard/modules/user-profile-maintenance.js";
import { UserProfilePage } from "../../pages/dashboard/modules/user-profile-page.js";

test("relationship slider output follows input events while dragging", () => {
  const listeners = {};
  const detail = {
    addEventListener(type, listener) {
      listeners[type] = listener;
    },
  };
  globalThis.window = {
    addEventListener() {},
    t(key) { return key; },
  };
  globalThis.document = {
    getElementById(id) {
      return id === "user-profile-detail" ? detail : null;
    },
  };
  try {
    new UserProfileMaintenance({}, () => {}, {}).initEventListeners();
    const output = { textContent: "20" };
    const slider = {
      value: "73",
      nextElementSibling: output,
      closest(selector) {
        return selector === "[data-relationship-dimension]" ? this : null;
      },
    };
    listeners.input({ target: slider });
    assert.equal(output.textContent, "73");
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("relationship editor sends display values and optimistic revision", async () => {
  const calls = [];
  const dimensions = [
    ["familiarity", "20"],
    ["trust", "30"],
    ["warmth", "40"],
    ["ease", "50"],
    ["tension", "10"],
    ["concern", "60"],
  ].map(([key, value]) => ({ dataset: { relationshipDimension: key }, value }));
  const values = {
    "relationship-tags": { value: "curious, steady" },
    "relationship-summary": { value: "I am getting to know this user." },
    "relationship-sensitivity": { value: "slow" },
    "relationship-behavior": { value: "high_autonomy" },
  };
  globalThis.window = { t(key) { return key; } };
  globalThis.document = {
    querySelectorAll() { return dimensions; },
    getElementById(id) { return values[id] || null; },
  };
  try {
    const profile = new UserProfileMaintenance({
      async post(path, body) { calls.push({ path, body }); },
    }, () => {}, {});
    profile.selectedScopeUid = "scope-1";
    profile.detail = { relationship: { revision: 7 } };
    profile.loadDetail = async () => {};
    await profile.relationshipAction("save");
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }

  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "user-profiles/relationship/update");
  assert.equal(calls[0].body.expected_revision, 7);
  assert.deepEqual(calls[0].body.changes.stance_tags, ["curious", "steady"]);
  assert.equal(calls[0].body.changes.trust, 30);
  assert.equal(calls[0].body.sensitivity_override, "slow");
  assert.equal(calls[0].body.behavior_override, "high_autonomy");
});

test("excluded facts remain recoverable from the history section", () => {
  globalThis.window = { t(key) { return key; } };
  globalThis.document = {
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const profile = new UserProfileMaintenance({}, () => {}, {});
    const html = profile.renderFactSection("history", [{
      profile_fact_uid: "fact-1",
      category: "preference",
      raw_fact: "User likes tea",
      status: "excluded",
      confidence: 0.9,
      importance: 0.7,
      sources: [],
    }], false);
    assert.match(html, /data-fact-action="resume"/);
    assert.doesNotMatch(html, /data-fact-action="exclude"/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("profile architecture status separates serving facts, review facts, relationship, and jobs", () => {
  globalThis.window = { t(key, ...args) { return `${key}:${args.join(",")}`; } };
  globalThis.document = {
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const profile = new UserProfileMaintenance({}, () => {}, {});
    const html = profile.renderArchitectureStatus({
      scope: { enabled: true, bot_account: "bot-1", persona_id: "persona-1" },
      fact_revision: 7,
      facts: [
        { status: "active" },
        { status: "active" },
        { status: "pending" },
        { status: "conflict" },
        { status: "stale" },
      ],
      relationship: { revision: 4, persona_signature: { digest: "digest" } },
      relationship_revisions: [
        { revision: 4, diagnostics: { persona_basis: "current_config" } },
      ],
      tasks: [{ status: "running_facts" }, { status: "completed" }],
      gap: { pending_count: 3 },
    });
    assert.match(html, /data-profile-architecture-status/);
    assert.match(html, /r7 · 2 profile\.factStatus\.active/);
    assert.match(html, /profile\.state\.reviewCounts:1,1,1/);
    assert.match(html, /profile\.state\.executionPersona/);
    assert.match(html, /profile\.state\.queueCount:1/);
    assert.match(html, /profile\.state\.gapCount:3/);

    const signatureOnly = profile.renderArchitectureStatus({
      scope: { enabled: true },
      relationship: { revision: 4, persona_signature: { digest: "digest" } },
      relationship_revisions: [{ revision: 4, diagnostics: {} }],
    });
    assert.match(signatureOnly, /profile\.state\.noPersonaBasis/);
    assert.doesNotMatch(signatureOnly, /profile\.state\.executionPersona/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("profile rebuild submits both profile and Timeline history fingerprints", async () => {
  const calls = [];
  let confirmation;
  globalThis.window = { t(key, ...args) { return `${key}:${args.join(",")}`; } };
  globalThis.document = {
    getElementById(id) {
      return id === "profile-rebuild-clear-overrides" ? { checked: true } : null;
    },
  };
  try {
    const profile = new UserProfileMaintenance({
      async post(path, body) {
        calls.push({ path, body });
        if (path.endsWith("/preview")) {
          return {
            fingerprint: "profile-fingerprint",
            history_fingerprint: "history-fingerprint",
            timeline_count: 4,
            missing_timeline_count: 3,
            ambiguous_identity_count: 2,
            fact_count: 2,
            override_count: 1,
          };
        }
        return {};
      },
    }, () => {}, {
      async show(options) {
        confirmation = options;
        return true;
      },
    });
    profile.selectedScopeUid = "scope-1";
    profile.loadDetail = async () => {};
    profile.showToast = () => {};
    await profile.rebuildProfile();
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }

  assert.equal(calls.length, 2);
  assert.equal(confirmation.message, "profile.rebuildImpact:4,3,2,2,1");
  assert.equal(calls[1].path, "user-profiles/rebuild/start");
  assert.equal(calls[1].body.fingerprint, "profile-fingerprint");
  assert.equal(calls[1].body.history_fingerprint, "history-fingerprint");
  assert.equal(calls[1].body.clear_overrides, true);
});

test("legacy Timeline review sends a reversible scoped binding decision", async () => {
  const calls = [];
  const select = { value: "test:human:user-1" };
  const row = { querySelector() { return select; } };
  const button = {
    dataset: {
      identityReviewAction: "bind",
      timelineUid: "timeline-legacy",
      timelineRevision: "2",
      memorySpaceId: "space-1",
      evidenceFingerprint: "evidence-1",
    },
    closest() { return row; },
  };
  globalThis.window = { t(key) { return key; } };
  try {
    const profile = new UserProfileMaintenance({
      async post(path, body) { calls.push({ path, body }); },
    }, () => {}, {
      async show() { return true; },
    });
    profile.selectedScopeUid = "scope-1";
    profile.loadDetail = async () => {};
    await profile.identityReviewAction(button);
  } finally {
    delete globalThis.window;
  }

  assert.deepEqual(calls, [{
    path: "user-profiles/identity-reviews/action",
    body: {
      profile_scope_uid: "scope-1",
      timeline_uid: "timeline-legacy",
      timeline_revision: 2,
      memory_space_id: "space-1",
      evidence_fingerprint: "evidence-1",
      action: "bind",
      actor_id: "test:human:user-1",
    },
  }]);
});

test("profile build submits the exact server-issued candidate scope", async () => {
  const calls = [];
  globalThis.window = { t(key) { return key; } };
  try {
    const profile = new UserProfileMaintenance({
      async post(path, body) {
        calls.push({ path, body });
        return { profile_scope_uid: "scope-new" };
      },
    }, () => {}, { async show() { return true; } });
    profile.buildCandidates = [{
      actor_id: "test:human:user-1",
      bot_account: "bot-1",
      persona_id: "persona-1",
      display_name: "User One",
      timeline_count: 2,
      candidate_fingerprint: "candidate-fingerprint",
    }];
    profile.closeBuildPanel = () => {};
    profile.loadList = async () => {};
    profile.loadDetail = async () => {};
    await profile.buildCandidate({ dataset: { profileBuildCandidate: "0" }, disabled: false });
    assert.deepEqual(calls, [{
      path: "user-profiles/build",
      body: {
        actor_id: "test:human:user-1",
        bot_account: "bot-1",
        persona_id: "persona-1",
        candidate_fingerprint: "candidate-fingerprint",
      },
    }]);
    assert.equal(profile.selectedScopeUid, "scope-new");
  } finally {
    delete globalThis.window;
  }
});

test("profile tasks expose stage progress and readable job rows", () => {
  globalThis.window = { t(key, ...args) { return `${key}:${args.join(",")}`; } };
  globalThis.document = {
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const profile = new UserProfileMaintenance({}, () => {}, {});
    const task = {
      task_uid: "task-12345678",
      status: "running_relationship",
      progress_percent: 75,
      completed_stage_count: 1,
      total_stage_count: 2,
      total_count: 4,
      retries: 0,
      updated_at: 1,
      batch_timeline_count: 4,
      batch_candidate_count: 7,
      batch_prompt_estimate_chars: 12000,
    };
    const monitor = profile.renderTaskMonitor([task], { pending_count: 2 });
    const rows = profile.renderTaskRows([task]);
    assert.match(monitor, /width:75%/);
    assert.match(monitor, /profile\.taskStatus\.running_relationship/);
    assert.match(rows, /maintenance-task-progress/);
    assert.match(rows, /profile\.taskRowMeta:75,4,0/);
    assert.match(rows, /profile\.taskBatchMeta:4,7/);
    assert.doesNotMatch(rows, /data-task-retry/);
    assert.match(profile.renderTaskRows([{ ...task, status: "facts_completed", error: "provider failed", result_summary: { relationship_error: "provider failed" } }]), /profile\.taskStatus\.relationship_failed/);
    assert.match(profile.renderTaskMonitor([], { pending_count: 5 }), /class="user-profile-task-monitor hidden"/);
    profile.selectedScopeUid = "scope-1";
    profile.taskProgressBaseline = { scope: "scope-1", total: 5 };
    assert.match(profile.renderTaskMonitor([task], { pending_count: 5 }), /profile\.taskOverallProgress:60,3,5,2/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("task polling refreshes derived profile after the queue drains", async () => {
  const calls = [];
  globalThis.window = { t(key) { return key; } };
  globalThis.document = {
    getElementById() { return null; },
    querySelector() { return null; },
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const profile = new UserProfileMaintenance({
      async get(path) {
        calls.push(path);
        return { items: [{ status: "completed" }], gap: { pending_count: 0 } };
      },
    }, () => {}, {});
    profile.selectedScopeUid = "scope-1";
    profile.detail = { scope: { enabled: true }, tasks: [{ status: "running_facts" }], gap: { pending_count: 1 } };
    profile.loadList = async () => { calls.push("list"); };
    profile.loadDetail = async () => { calls.push("detail"); };
    await profile.pollTasks(profile.taskPollGeneration);
    assert.deepEqual(calls, ["user-profiles/tasks", "list", "detail"]);
    assert.equal(profile.taskProgressBaseline, null);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("task polling stops a tracked queue when its profile is disabled", async () => {
  const calls = [];
  globalThis.window = { t(key) { return key; } };
  globalThis.document = {
    getElementById() { return null; },
    querySelector() { return null; },
  };
  try {
    const profile = new UserProfileMaintenance({
      async get(path) {
        calls.push(path);
        return { items: [], gap: { has_gap: true, pending_count: 4 } };
      },
    }, key => calls.push(key), {});
    profile.selectedScopeUid = "scope-1";
    profile.detail = { scope: { enabled: false }, tasks: [], gap: { pending_count: 4 } };
    profile.taskProgressBaseline = { scope: "scope-1", total: 4 };
    await profile.pollTasks(profile.taskPollGeneration);
    assert.deepEqual(calls, ["user-profiles/tasks"]);
    assert.equal(profile.taskProgressBaseline, null);
    assert.equal(profile.taskPollTimer, null);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("task polling stops when automatic retries are exhausted", async () => {
  const calls = [];
  globalThis.window = { t(key) { return key; } };
  globalThis.document = {
    getElementById() { return null; },
    querySelector() { return null; },
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const failed = { status: "failed", failed_stage: "facts", error: "timeout" };
    const profile = new UserProfileMaintenance({
      async get(path) {
        calls.push(path);
        return { items: [failed], gap: { has_gap: true, pending_count: 4 } };
      },
    }, () => {}, {});
    profile.selectedScopeUid = "scope-1";
    profile.detail = { scope: { enabled: true }, tasks: [{ status: "running_facts" }], gap: { pending_count: 4 } };
    profile.taskProgressBaseline = { scope: "scope-1", total: 4 };
    await profile.pollTasks(profile.taskPollGeneration);
    assert.deepEqual(calls, ["user-profiles/tasks"]);
    assert.equal(profile.taskProgressBaseline, null);
    assert.equal(profile.taskPollTimer, null);
    assert.match(profile.renderTaskMonitor([failed], { pending_count: 4 }), /profile\.taskStatus\.failed/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("profile task rows expose actions appropriate to each state", () => {
  globalThis.window = { t(key) { return key; } };
  globalThis.document = {
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const profile = new UserProfileMaintenance({}, () => {}, {});
    const running = profile.renderTaskRows([{ task_uid: "run", status: "running_facts", items: [] }]);
    const failed = profile.renderTaskRows([{ task_uid: "fail", status: "failed", items: [] }]);
    const completed = profile.renderTaskRows([{ task_uid: "done", status: "completed", items: [] }]);
    assert.match(running, /data-task-cancel="run"/);
    assert.doesNotMatch(running, /data-task-delete/);
    assert.match(failed, /data-task-retry="fail"/);
    assert.match(failed, /data-task-cancel="fail"/);
    assert.match(completed, /data-task-delete="done"/);
    assert.doesNotMatch(completed, /data-task-cancel/);
    assert.match(profile.renderTasks([{ task_uid: "done", status: "completed", items: [] }]), /data-task-clear-completed/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("empty profile list keeps the toolbar as the only build entry", () => {
  globalThis.window = { t(key) { return key; } };
  const list = { innerHTML: "" };
  globalThis.document = {
    getElementById(id) { return id === "user-profile-list" ? list : null; },
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const profile = new UserProfileMaintenance({}, () => {}, {});
    profile.renderList();
    assert.doesNotMatch(list.innerHTML, /data-profile-empty-build/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("standalone profile page renders only read-only facts and relationship meters", () => {
  globalThis.window = { t(key) { return key; } };
  const detailRoot = { innerHTML: "" };
  globalThis.document = {
    getElementById(id) { return id === "profile-page-detail" ? detailRoot : null; },
    createElement() {
      let text = "";
      return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text; },
      };
    },
  };
  try {
    const page = new UserProfilePage({});
    const activeFacts = [
      {
        category: "preference",
        raw_fact: "User likes tea",
        status: "active",
        confidence: 0.9,
        importance: 0.8,
        sources: [{ timeline_uid: "timeline-1", timeline_revision: 2 }],
      },
      {
        category: "stable_info",
        raw_fact: "User writes software",
        status: "active",
        confidence: 0.95,
        importance: 0.85,
        sources: [],
      },
    ];
    const facts = page.renderFacts("facts", activeFacts);
    const relationship = page.renderRelationship({ trust: 0.72, stance_tags: ["steady"], subjective_summary: "Known well." }, {});
    assert.match(facts, /User likes tea/);
    assert.match(facts, /data-open-timeline="timeline-1"/);
    assert.ok(facts.indexOf('data-fact-group="stable_info"') < facts.indexOf('data-fact-group="preference"'));
    assert.doesNotMatch(facts, /data-fact-action/);
    assert.match(relationship, /width:72%/);
    assert.doesNotMatch(relationship, /type="range"/);

    page.selectedScopeUid = "scope-1";
    page.detail = {
      scope: { profile_scope_uid: "scope-1", logical_user_uid: "user-1", bot_account: "bot-1", persona_id: "persona-1", enabled: true },
      accounts: [{ last_observed_name: "User" }],
      facts: [...activeFacts, { category: "habit", raw_fact: "Needs review", status: "pending", confidence: 0.6, importance: 0.5 }],
      fact_revision: 3,
      relationship: { revision: 2, trust: 0.72, subjective_summary: "Known well." },
      injection_preview: { total_chars: 42, fact_count: 2, relationship_included: true, content: "preview" },
    };
    page.renderDetail();
    assert.match(detailRoot.innerHTML, /profile-view-facts-panel is-review/);
    assert.match(detailRoot.innerHTML, /<details class="profile-view-injection">/);
    assert.ok(detailRoot.innerHTML.indexOf("profile-view-primary-grid") < detailRoot.innerHTML.indexOf("profile-view-facts-panel is-review"));
    assert.ok(detailRoot.innerHTML.indexOf("profile-view-facts-panel is-review") < detailRoot.innerHTML.indexOf("profile-view-injection"));
    assert.doesNotMatch(detailRoot.innerHTML, /type="range"|data-fact-action/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("profile navigation and settings tabs follow the requested order", () => {
  const html = readFileSync(new URL("../../pages/dashboard/index.html", import.meta.url), "utf8");
  assert.ok(html.indexOf('data-page="topic"') < html.indexOf('data-page="profiles"'));
  assert.ok(html.indexOf('data-page="profiles"') < html.indexOf('data-page="identities"'));
  assert.ok(html.indexOf('data-settings-category="topic"') < html.indexOf('data-settings-category="user_profile"'));
  assert.ok(html.indexOf('data-settings-category="user_profile"') < html.indexOf('data-settings-category="session"'));
});
