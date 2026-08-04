import assert from "node:assert/strict";
import test from "node:test";

import { MemoryPage } from "../../pages/dashboard/modules/memory-page.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createState() {
  return {
    memory: {
      items: [],
      total: 0,
      page: 1,
      pageSize: 20,
      hasMore: false,
      keyword: "",
      session: "",
      status: "all",
      type: "all",
      sort: "created_desc",
      selectedIds: new Set(),
    },
  };
}

function apiMemory(id, summary) {
  return {
    id,
    text: summary,
    metadata: {
      memory_type: "GENERAL",
      importance: 0.5,
      status: "active",
    },
  };
}

test("the latest memory fetch wins when responses arrive out of order", async () => {
  const slow = deferred();
  const fast = deferred();
  const state = createState();
  const api = {
    get(_path, params) {
      return params.keyword === "slow" ? slow.promise : fast.promise;
    },
  };
  const page = new MemoryPage(state, api, {});
  page.renderVirtual = () => {};
  page.updatePagination = () => {};

  state.memory.keyword = "slow";
  const slowFetch = page.fetch();
  state.memory.keyword = "fast";
  const fastFetch = page.fetch();

  fast.resolve({ items: [apiMemory(2, "FAST RESULT")], total: 1 });
  await fastFetch;
  slow.resolve({ items: [apiMemory(1, "SLOW RESULT")], total: 1 });
  await slowFetch;

  assert.equal(state.memory.items.length, 1);
  assert.equal(state.memory.items[0].memory_id, 2);
  assert.equal(state.memory.items[0].summary, "FAST RESULT");
});

test("the bound scroll listener uses current items and table spacer rows", () => {
  const state = createState();
  const tbody = { innerHTML: "", style: {} };
  const scrollEl = {
    scrollTop: 0,
    clientHeight: 460,
    listener: null,
    addEventListener(_type, listener) {
      this.listener = listener;
    },
  };
  globalThis.window = {
    requestAnimationFrame(callback) {
      callback();
    },
    t(key, ...args) {
      return [key, ...args].join(" ");
    },
  };
  globalThis.document = {
    createElement() {
      let text = "";
      return {
        set textContent(value) {
          text = String(value);
        },
        get innerHTML() {
          return text;
        },
      };
    },
    getElementById(id) {
      if (id === "memories-body") return tbody;
      if (id === "memories-scroll") return scrollEl;
      return null;
    },
  };

  try {
    const page = new MemoryPage(state, {}, {});
    page.updateSelectionControls = () => {};
    state.memory.items = Array.from({ length: 20 }, (_, index) => ({
      memory_id: index + 1,
      summary: `Memory ${index + 1}`,
      memory_type: "GENERAL",
      topic_count: 0,
      importance: 5,
      status: "active",
      created_at: "--",
      updated_at: "--",
    }));
    page.renderVirtual();
    assert.equal(typeof scrollEl.listener, "function");

    state.memory.items = Array.from({ length: 100 }, (_, index) => ({
      memory_id: index + 1,
      summary: `Memory ${index + 1}`,
      memory_type: "GENERAL",
      topic_count: 0,
      importance: 5,
      status: "active",
      created_at: "--",
      updated_at: "--",
    }));
    page.renderVirtual();
    scrollEl.scrollTop = 1095;
    scrollEl.listener();

    assert.match(tbody.innerHTML, /data-key="m:5"/);
    assert.match(tbody.innerHTML, /data-key="m:43"/);
    assert.doesNotMatch(tbody.innerHTML, /data-key="m:44"/);
    assert.match(tbody.innerHTML, /colspan="8"/);
    assert.match(tbody.innerHTML, /height:3192px/);
    assert.equal(tbody.style.paddingTop, "0");
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});

test("batch editing importance posts the display scale and clears successful selections", async () => {
  const state = createState();
  state.memory.selectedIds = new Set([11, 12]);
  const calls = [];
  const api = {
    async post(path, body) {
      calls.push({ path, body });
      return { updated_count: 2, failed_count: 0, failed_ids: [] };
    },
  };
  globalThis.window = { t: (key, ...args) => [key, ...args].join(" ") };
  try {
    const page = new MemoryPage(state, api, {}, {});
    page.updateSelectionControls = () => {};
    page.showBatchEditDialog = async () => ({
      field: "importance",
      value: 7.5,
      value_scale: "display",
      reason: "reviewed",
    });
    page.showToast = () => {};
    page.fetch = async () => {};

    await page.batchEdit();

    assert.deepEqual(calls, [{
      path: "memories/batch-update",
      body: {
        memory_ids: [11, 12],
        field: "importance",
        value: 7.5,
        reason: "reviewed",
        value_scale: "display",
      },
    }]);
    assert.deepEqual([...state.memory.selectedIds], []);
  } finally {
    delete globalThis.window;
  }
});

test("batch editing retains only failed items and ignores a repeated submit", async () => {
  const state = createState();
  state.memory.selectedIds = new Set([21, 22]);
  const dialog = deferred();
  let postCount = 0;
  const api = {
    async post() {
      postCount += 1;
      return { updated_count: 1, failed_count: 1, failed_ids: [22] };
    },
  };
  globalThis.window = { t: (key, ...args) => [key, ...args].join(" ") };
  try {
    const page = new MemoryPage(state, api, {}, {});
    page.updateSelectionControls = () => {};
    page.showBatchEditDialog = () => dialog.promise;
    page.showToast = () => {};
    page.fetch = async () => {};

    const first = page.batchEdit();
    const repeated = page.batchEdit();
    dialog.resolve({ field: "type", value: "FACT", reason: "" });
    await Promise.all([first, repeated]);

    assert.equal(postCount, 1);
    assert.deepEqual([...state.memory.selectedIds], [22]);
  } finally {
    delete globalThis.window;
  }
});

test("batch archive requires confirmation before sending the update", async () => {
  const state = createState();
  state.memory.selectedIds = new Set([31]);
  let postCount = 0;
  const api = { async post() { postCount += 1; } };
  const confirmDialog = { async show() { return false; } };
  globalThis.window = { t: (key, ...args) => [key, ...args].join(" ") };
  try {
    const page = new MemoryPage(state, api, {}, confirmDialog);
    page.updateSelectionControls = () => {};
    page.showBatchEditDialog = async () => ({
      field: "status",
      value: "archived",
      reason: "",
    });

    await page.batchEdit();

    assert.equal(postCount, 0);
    assert.deepEqual([...state.memory.selectedIds], [31]);
  } finally {
    delete globalThis.window;
  }
});
