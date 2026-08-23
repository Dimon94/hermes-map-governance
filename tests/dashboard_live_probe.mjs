import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";


const bundlePath = process.argv[2];
const mode = process.argv[3];
const hermesRoot = process.argv[4];
const require = createRequire(import.meta.url);
const { chromium } = require(path.join(hermesRoot, "node_modules", "playwright"));
const browser = await chromium.launch({ headless: true });
const browserPage = await browser.newPage();
const bundleSource = fs.readFileSync(bundlePath, "utf8");
const result = await browserPage.evaluate(async ({ bundleSource, mode }) => {
const assert = {
  equal(actual, expected) {
    if (actual !== expected) throw new Error(`Expected ${expected}, got ${actual}`);
  },
  deepEqual(actual, expected) {
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    }
  },
  match(actual, expected) {
    if (!expected.test(actual)) throw new Error(`Expected ${actual} to match ${expected}`);
  },
  doesNotMatch(actual, expected) {
    if (expected.test(actual)) throw new Error(`Expected ${actual} not to match ${expected}`);
  },
};
const waitForBrowserTurn = () => new Promise((resolve) => {
  const channel = new MessageChannel();
  channel.port1.onmessage = resolve;
  channel.port2.postMessage(null);
});
const hooks = [];
const effects = [];
const sockets = [];
const timers = [];
const requested = [];
let hookIndex = 0;
let boardReads = 0;
let page = null;

function createElement(type, props, ...children) {
  return { type, props: props || {}, children };
}

function useState(initial) {
  const index = hookIndex++;
  if (!(index in hooks)) hooks[index] = initial;
  return [
    hooks[index],
    (value) => {
      hooks[index] = typeof value === "function" ? value(hooks[index]) : value;
    },
  ];
}

function useRef(initial) {
  const index = hookIndex++;
  if (!(index in hooks)) hooks[index] = { current: initial };
  return hooks[index];
}

function card(id, projectId, identity, title) {
  return {
    id,
    project: { id: projectId, url: `https://github.com/projects/${projectId}` },
    tracker: {
      provider: "github",
      id,
      identity,
      url: `https://github.com/${identity.replace("#", "/issues/")}`,
    },
    title,
    stage: "authorized",
    available_transitions: ["delivery", "parked"],
    decision_summary: { count: 0, latest: null },
    approval_summary: { count: 0, pending_count: 0, latest: null, statuses: [] },
    delivery_summary: { state: "not_reported" },
    external_effects: {
      state: "healthy",
      pending_count: 0,
      retry_scheduled_count: 0,
      leased_count: 0,
      succeeded_count: 0,
      terminal_count: 0,
      latest_terminal: null,
    },
    ceo_session: { state: "unbound" },
    last_synchronized_at: "2026-08-23T07:30:00Z",
  };
}

function board(cursor) {
  const atlas = card("I_atlas_41", "PVT_acme_7", "acme/atlas#41", "Map the Atlas launch");
  const hello = card("I_hello_9", "PVT_octocat_3", "octocat/hello-world#9", "Map a friendly launch");
  const acme = {
    id: "PVT_acme_7",
    title: "Acme CEO portfolio",
    tracker: { owner: "acme", number: 7, url: "https://github.com/orgs/acme/projects/7" },
    authority: {
      state: "healthy",
      last_success_at: "2026-08-23T07:30:00Z",
      reason: null,
      sources: [],
      recovery: null,
    },
    maps: [atlas],
  };
  const octocat = {
    id: "PVT_octocat_3",
    title: "Octocat CEO portfolio",
    tracker: { owner: "octocat", number: 3, url: "https://github.com/users/octocat/projects/3" },
    authority: {
      state: "healthy",
      last_success_at: "2026-08-23T07:30:00Z",
      reason: null,
      sources: [],
      recovery: null,
    },
    maps: [hello],
  };
  return {
    cursor,
    projects: [acme, octocat],
    maps: [atlas, hello],
    empty_state: { title: "No Maps are bound", description: "Bind a Map." },
  };
}

function fetchJSON(path) {
  requested.push(path);
  if (path.includes("/board?")) {
    boardReads += 1;
    return Promise.resolve(board(boardReads === 1 ? 5 : 12));
  }
  if (path.includes("/health?")) return Promise.resolve({ status: "ready" });
  return Promise.reject(new Error(`Unexpected full reload or per-card poll: ${path}`));
}

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.closed = false;
    sockets.push(this);
  }

  open() {
    this.onopen?.();
  }

  frame(payload) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.();
  }
}

function render() {
  hookIndex = 0;
  const tree = page();
  document.body.replaceChildren(toDom(tree));
  return tree;
}

function toDom(node) {
  if (Array.isArray(node)) {
    const fragment = document.createDocumentFragment();
    node.forEach((child) => fragment.append(toDom(child)));
    return fragment;
  }
  if (typeof node === "string" || typeof node === "number") {
    return document.createTextNode(String(node));
  }
  if (!node) return document.createTextNode("");
  const nativeTags = new Set(["article", "p", "time", "ul", "li", "button"]);
  const tag = node.type === "Button" ? "button" : (nativeTags.has(node.type) ? node.type : "div");
  const element = document.createElement(tag);
  if (node.props.disabled) element.disabled = true;
  if (node.props["aria-label"]) element.setAttribute("aria-label", node.props["aria-label"]);
  (node.children || []).forEach((child) => element.append(toDom(child)));
  return element;
}

function textContent(node) {
  if (Array.isArray(node)) return node.map(textContent).join(" ");
  if (typeof node === "string") return node;
  if (!node || !Array.isArray(node.children)) return "";
  return node.children.map(textContent).join(" ");
}

function nodes(node, predicate, matches = []) {
  if (Array.isArray(node)) {
    node.forEach((child) => nodes(child, predicate, matches));
  } else if (node && typeof node !== "string") {
    if (predicate(node)) matches.push(node);
    nodes(node.children || [], predicate, matches);
  }
  return matches;
}

Object.assign(window, {
  confirm: () => true,
  prompt: () => "confirmed",
  WebSocket: FakeWebSocket,
  setTimeout(callback) {
    timers.push(callback);
    return timers.length;
  },
  clearTimeout() {},
  __HERMES_PLUGIN_SDK__: {
    React: { createElement },
    fetchJSON,
    buildWsUrl(path, params) {
      return Promise.resolve(`ws://hermes${path}?profile=${params.profile}&cursor=${params.cursor}`);
    },
    hooks: {
      useCallback: (callback) => callback,
      useEffect: (effect) => effects.push(effect),
      useRef,
      useState,
    },
    components: {
      Badge: "Badge",
      Button: "Button",
      Card: "Card",
      CardContent: "CardContent",
      CardHeader: "CardHeader",
      CardTitle: "CardTitle",
    },
    host: { state: { profile: { get: () => "worker" } } },
  },
  __HERMES_PLUGINS__: {
    register(name, registered) {
      assert.equal(name, "map-governance");
      page = registered;
    },
  },
});

(0, eval)(bundleSource);
render();
assert.equal(effects.length, 1);
effects[0]();
await waitForBrowserTurn();
await waitForBrowserTurn();
assert.equal(sockets.length, 1);
assert.match(sockets[0].url, /cursor=5/);
sockets[0].open();

if (mode === "live-update") {
  sockets[0].frame({
    status: "open",
    cursor: 8,
    latest_cursor: 8,
    has_more: false,
    events: [
      {
        cursor: 6,
        project_id: "PVT_acme_7",
        map_id: "I_atlas_41",
        type: "map.upserted",
        payload: { card: {
          id: "I_atlas_41",
          project_id: "PVT_acme_7",
          repository: "acme/atlas",
          issue_number: 41,
          issue_url: "https://github.com/acme/atlas/issues/41",
          title: "Map the Atlas launch",
          stage: "delivery",
          synchronized_at: "2026-08-23T07:31:00Z",
        } },
      },
      {
        cursor: 7,
        project_id: "PVT_acme_7",
        map_id: "I_atlas_41",
        type: "pm-report.upserted",
        payload: { report: {
          assignment_map_id: "I_atlas_41",
          record_id: "blocker-live-1",
          type: "blocker",
          summary: "The whole Map needs an executive decision.",
          timestamp: "2026-08-23T07:31:00Z",
          blocking: true,
        } },
      },
      {
        cursor: 8,
        project_id: "PVT_acme_7",
        map_id: null,
        type: "reconcile.completed",
        payload: {
          last_success_at: "2026-08-23T07:32:00Z",
          cards: [{
            id: "I_atlas_41",
            project_id: "PVT_acme_7",
            repository: "acme/atlas",
            issue_number: 41,
            issue_url: "https://github.com/acme/atlas/issues/41",
            title: "Map the Atlas launch",
            stage: "delivery",
            synchronized_at: "2026-08-23T07:32:00Z",
          }],
          decisions: { I_atlas_41: [] },
          pm_reports: { I_atlas_41: [{
            assignment_map_id: "I_atlas_41",
            record_id: "blocker-live-1",
            type: "blocker",
            summary: "The whole Map needs an executive decision.",
            timestamp: "2026-08-23T07:31:00Z",
            blocking: true,
          }] },
          approvals: { I_atlas_41: [
            {
              request_id: "approval-live-2",
              status: "approved",
              requested_at: "2026-08-23T07:31:00Z",
            },
            {
              request_id: "approval-live-1",
              status: "pending",
              requested_at: "2026-08-23T07:30:00Z",
            },
          ] },
        },
      },
    ],
  });
  sockets[0].frame({
    status: "open",
    cursor: 9,
    latest_cursor: 9,
    has_more: false,
    events: [{
      cursor: 9,
      project_id: "PVT_acme_7",
      map_id: "I_atlas_41",
      type: "approval.upserted",
      payload: { approval: {
        request_id: "approval-live-1",
        status: "rejected",
      } },
    }],
  });
  assert.match(
    textContent(render()),
    /Map the Atlas launch.*delivery.*2 approval requests.*Latest: approved · approval-live-2.*1 PM executive report.*whole map blocker/s,
  );
  assert.doesNotMatch(textContent(render()), /pending chairman decision/);
  assert.equal(boardReads, 1);
} else if (mode === "disconnect") {
  sockets[0].close();
  const text = textContent(render());
  assert.match(text, /Live updates disconnected · reconnecting/);
  assert.doesNotMatch(text, /Read-only stale projection/);
} else if (mode === "reconnect") {
  sockets[0].frame({ status: "open", cursor: 6, latest_cursor: 6, events: [] });
  sockets[0].close();
  assert.equal(timers.length, 1);
  timers.shift()();
  await waitForBrowserTurn();
  assert.equal(sockets.length, 2);
  assert.match(sockets[1].url, /cursor=6/);
  sockets[1].open();
  sockets[1].frame({
    status: "open",
    cursor: 7,
    latest_cursor: 7,
    has_more: false,
    events: [{
      cursor: 7,
      project_id: "PVT_acme_7",
      map_id: "I_atlas_41",
      type: "session.updated",
      payload: { ceo_session: { state: "ready", last_activity_at: "2026-08-23T07:32:00Z" } },
    }],
  });
  const text = textContent(render());
  assert.match(text, /CEO session: ready/);
  assert.equal((text.match(/Map the Atlas launch/g) || []).length, 1);
  assert.equal(boardReads, 1);
} else if (mode === "cursor-expiry") {
  sockets[0].frame({
    status: "refresh_required",
    reason: "cursor_expired",
    cursor: 5,
    retained_after_cursor: 9,
    latest_cursor: 11,
  });
  await waitForBrowserTurn();
  await waitForBrowserTurn();
  assert.equal(boardReads, 2);
  assert.equal(sockets.length, 2);
  assert.match(sockets[1].url, /cursor=12/);
  const text = textContent(render());
  assert.equal((text.match(/Map the Atlas launch/g) || []).length, 1);
} else if (mode === "slow-consumer") {
  const events = Array.from({ length: 100 }, (_, index) => ({
    cursor: 6 + index,
    project_id: "PVT_acme_7",
    map_id: "I_atlas_41",
    type: "map.upserted",
    payload: { card: {
      id: "I_atlas_41",
      project_id: "PVT_acme_7",
      repository: "acme/atlas",
      issue_number: 41,
      issue_url: "https://github.com/acme/atlas/issues/41",
      title: "Map the Atlas launch",
      stage: index === 99 ? "delivery" : "authorized",
      synchronized_at: "2026-08-23T07:31:00Z",
    } },
  }));
  sockets[0].frame({ status: "open", cursor: 105, latest_cursor: 105, has_more: false, events });
  const text = textContent(render());
  assert.match(text, /Map the Atlas launch.*delivery/s);
  assert.equal((text.match(/Map the Atlas launch/g) || []).length, 1);
  assert.equal(boardReads, 1);
} else if (mode === "independent-stale") {
  sockets[0].frame({
    status: "open",
    cursor: 6,
    latest_cursor: 6,
    has_more: false,
    events: [{
      cursor: 6,
      project_id: "PVT_acme_7",
      map_id: null,
      type: "reachability.updated",
      payload: {
        source: "tracker",
        state: "stale",
        last_success_at: "2026-08-23T07:30:00Z",
        reason: "GitHub authority is unreachable",
      },
    }],
  });
  const tree = render();
  const text = textContent(tree);
  assert.match(text, /Read-only stale projection.*GitHub authority is unreachable.*authoritative reconcile/s);
  assert.match(text, /Octocat CEO portfolio/);
  const transitionButtons = nodes(
    tree,
    (node) => node.type === "Button" && /Move to delivery/.test(textContent(node)),
  );
  assert.equal(transitionButtons.length, 2);
  assert.deepEqual(transitionButtons.map((node) => Boolean(node.props.disabled)), [true, false]);
  assert.equal(boardReads, 1);
} else {
  throw new Error(`Unknown mode: ${mode}`);
}

assert.deepEqual(requested, [
  "/api/plugins/map-governance/board?profile=worker",
  "/api/plugins/map-governance/health?profile=worker",
  ...(mode === "cursor-expiry" ? [
    "/api/plugins/map-governance/board?profile=worker",
    "/api/plugins/map-governance/health?profile=worker",
  ] : []),
]);
return { mode, visibleText: document.body.textContent };
}, { bundleSource, mode });
await browser.close();
assert.equal(result.mode, mode);
assert.ok(result.visibleText.includes("Maps"));
process.stdout.write(`dashboard ${mode} ready\n`);
