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

test("standalone profile page renders only read-only facts and relationship meters", () => {
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
    const page = new UserProfilePage({});
    const facts = page.renderFacts("facts", [{
      category: "preference",
      raw_fact: "User likes tea",
      status: "active",
      confidence: 0.9,
      importance: 0.8,
      sources: [{ timeline_uid: "timeline-1", timeline_revision: 2 }],
    }]);
    const relationship = page.renderRelationship({ trust: 0.72, stance_tags: ["steady"], subjective_summary: "Known well." }, {});
    assert.match(facts, /User likes tea/);
    assert.match(facts, /data-open-timeline="timeline-1"/);
    assert.doesNotMatch(facts, /data-fact-action/);
    assert.match(relationship, /width:72%/);
    assert.doesNotMatch(relationship, /type="range"/);
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
