import assert from "node:assert/strict";
import test from "node:test";

import { UserProfileMaintenance } from "../../pages/dashboard/modules/user-profile-maintenance.js";

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
