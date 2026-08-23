import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";


const bundlePath = process.argv[2];
const mode = process.argv[3] || "empty";
const requestedPaths = [];
const requestedOptions = [];
const openedSessions = [];
const effects = [];
const state = [];
let hookIndex = 0;
let registeredName = null;
let registeredPage = null;

function createElement(type, props, ...children) {
  return { type, props: props || {}, children };
}

function useState(initialValue) {
  const index = hookIndex++;
  if (!(index in state)) {
    state[index] = initialValue;
  }
  return [
    state[index],
    (nextValue) => {
      state[index] =
        typeof nextValue === "function" ? nextValue(state[index]) : nextValue;
    },
  ];
}

function fetchJSON(path, options) {
  requestedPaths.push(path);
  requestedOptions.push(options || {});
  if (path.includes("/transitions?profile=")) {
    if (mode === "transition-failure") {
      return Promise.reject(new Error("GitHub denied the stage label update"));
    }
    return Promise.resolve({ stage: "delivery" });
  }
  if (path.includes("/maps/I_atlas_41/session?profile=")) {
    return Promise.resolve({
      map_id: "I_atlas_41",
      ceo_session: {
        state: "ready",
        root_session_id: "canonical-root",
        live_session_id: "canonical-tip",
        last_activity_at: "2026-08-23T09:05:00Z",
      },
    });
  }
  if (path.includes("/refresh?profile=")) {
    return Promise.resolve({ refreshed: true });
  }
  if (path.includes("/board?profile=")) {
    if (mode !== "empty") {
      const card = {
        id: "I_atlas_41",
        project: {
          id: "PVT_acme_7",
          url: "https://github.com/orgs/acme/projects/7",
        },
        tracker: {
          provider: "github",
          id: "I_atlas_41",
          identity: "acme/atlas#41",
          url: "https://github.com/acme/atlas/issues/41",
        },
        title: "Map the Atlas launch",
        stage: "authorized",
        available_transitions: ["delivery", "parked"],
        ceo_session: mode === "ready" || mode === "hydration-retry"
          ? { state: "ready", last_activity_at: "2026-08-23T09:05:00Z" }
          : { state: "unbound" },
        last_synchronized_at: "2026-08-23T07:30:00Z",
      };
      return Promise.resolve({
        projects: [{
          id: "PVT_acme_7",
          title: "Acme CEO portfolio",
          tracker: {
            owner: "acme",
            number: 7,
            url: "https://github.com/orgs/acme/projects/7",
          },
          maps: [card],
        }],
        maps: [card],
      });
    }
    return Promise.resolve({
      projects: [],
      maps: [],
      empty_state: {
        title: "No Maps are bound",
        description: "Bind an existing GitHub Map Issue to start a governance board.",
      },
    });
  }
  if (path.includes("/health?profile=")) {
    return Promise.resolve({ status: "ready" });
  }
  return Promise.reject(new Error(`Unexpected path: ${path}`));
}

globalThis.window = {
  location: { search: "?profile=worker" },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement },
    fetchJSON,
    hooks: {
      useCallback: (callback) => callback,
      useEffect: (effect) => effects.push(effect),
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
    host: {
      openSession(sessionId, options) {
        openedSessions.push({
          sessionId,
          options,
          hydrationAttempts: mode === "hydration-retry" ? 2 : 1,
          transientHydrationFailures: mode === "hydration-retry" ? 1 : 0,
        });
        if (mode === "hydration-retry" && !options.retryHydrationTimeoutOnce) {
          return Promise.reject(new Error("transient hydration timeout"));
        }
        return Promise.resolve();
      },
    },
  },
  __HERMES_PLUGINS__: {
    register(name, page) {
      registeredName = name;
      registeredPage = page;
    },
  },
};

vm.runInThisContext(fs.readFileSync(bundlePath, "utf8"), { filename: bundlePath });
assert.equal(registeredName, "map-governance");
assert.equal(typeof registeredPage, "function");

hookIndex = 0;
registeredPage();
assert.equal(effects.length, 1);
effects[0]();
await new Promise((resolve) => setImmediate(resolve));

hookIndex = 0;
const readyTree = registeredPage();

function textContent(node) {
  if (Array.isArray(node)) {
    return node.map(textContent).join(" ");
  }
  if (typeof node === "string") {
    return node;
  }
  if (!node || !Array.isArray(node.children)) {
    return "";
  }
  return node.children.map(textContent).join(" ");
}

function findNode(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const match = findNode(child, predicate);
      if (match) return match;
    }
    return null;
  }
  if (!node || typeof node === "string") {
    return null;
  }
  if (predicate(node)) {
    return node;
  }
  return findNode(node.children || [], predicate);
}

const renderedText = textContent(readyTree);
assert.deepEqual(requestedPaths, [
  "/api/plugins/map-governance/board?profile=worker",
  "/api/plugins/map-governance/health?profile=worker",
]);
if (mode !== "empty") {
  assert.match(renderedText, /Acme CEO portfolio/);
  assert.match(renderedText, /Map the Atlas launch/);
  assert.match(renderedText, /acme\/atlas#41/);
  assert.match(renderedText, /authorized/);
  if (mode === "ready" || mode === "hydration-retry") {
    assert.match(renderedText, /CEO session: ready/);
    assert.match(renderedText, /2026-08-23T09:05:00Z/);
    const openButton = findNode(
      readyTree,
      (node) => node.type === "Button" && /Open CEO session/.test(textContent(node)),
    );
    assert.ok(openButton, "ready Map card opens its canonical CEO session");
    openButton.props.onClick();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const openIndex = requestedPaths.findIndex((path) => path.includes("/maps/I_atlas_41/session?"));
    assert.notEqual(openIndex, -1);
    assert.equal(
      requestedPaths[openIndex],
      "/api/plugins/map-governance/maps/I_atlas_41/session?profile=worker",
    );
    assert.equal(requestedOptions[openIndex].method, "POST");
    assert.equal(openedSessions.length, 1);
    assert.deepEqual(openedSessions[0].sessionId, "canonical-tip");
    assert.deepEqual(openedSessions[0].options, {
        profile: "worker",
        intent: "main",
        keepAllProfilesScope: false,
        awaitHydration: true,
        expectHistory: true,
        retryHydrationTimeoutOnce: true,
    });
    assert.equal(
      openedSessions[0].hydrationAttempts,
      mode === "hydration-retry" ? 2 : 1,
    );
    assert.equal(
      openedSessions[0].transientHydrationFailures,
      mode === "hydration-retry" ? 1 : 0,
    );
  } else {
    assert.match(renderedText, /CEO session: unbound/);
  }
  assert.match(renderedText, /2026-08-23T07:30:00Z/);
  const transitionButton = findNode(
    readyTree,
    (node) => node.type === "Button" && /Move to delivery/.test(textContent(node)),
  );
  assert.ok(transitionButton, "Map card exposes an explicit governed transition control");
  transitionButton.props.onClick();

  hookIndex = 0;
  const pendingTree = registeredPage();
  assert.match(textContent(pendingTree), /authorized/);

  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const transitionIndex = requestedPaths.findIndex((path) => path.includes("/transitions?profile="));
  assert.notEqual(transitionIndex, -1);
  assert.equal(
    requestedPaths[transitionIndex],
    "/api/plugins/map-governance/transitions?profile=worker",
  );
  assert.equal(requestedOptions[transitionIndex].method, "POST");
  assert.deepEqual(JSON.parse(requestedOptions[transitionIndex].body), {
    map_id: "I_atlas_41",
    expected_stage: "authorized",
    requested_stage: "delivery",
  });
  if (mode === "transition-failure") {
    hookIndex = 0;
    const failedTree = registeredPage();
    const failedText = textContent(failedTree);
    assert.match(failedText, /authorized/);
    assert.match(failedText, /GitHub denied the stage label update/);
  }
} else {
  assert.match(renderedText, /No Maps are bound/);
  assert.match(renderedText, /Bind an existing GitHub Map Issue/);
  assert.match(renderedText, /Plugin ready/);
}

const refreshButton = findNode(
  readyTree,
  (node) => node.type === "Button" && /Refresh/.test(textContent(node)),
);
assert.ok(refreshButton, "Maps page exposes refresh in populated and empty states");
refreshButton.props.onClick();
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));
const refreshIndex = requestedPaths.findIndex((path) => path.includes("/refresh?profile="));
assert.notEqual(refreshIndex, -1);
assert.equal(requestedPaths[refreshIndex], "/api/plugins/map-governance/refresh?profile=worker");
assert.equal(requestedOptions[refreshIndex].method, "POST");
assert.deepEqual(JSON.parse(requestedOptions[refreshIndex].body), {});

process.stdout.write(
  mode === "empty"
    ? "dashboard shell ready\n"
    : mode === "ready"
      ? "dashboard session open ready\n"
    : mode === "hydration-retry"
      ? "dashboard hydration retry ready\n"
    : mode === "transition-failure"
      ? "dashboard transition failure ready\n"
      : "dashboard board ready\n",
);
