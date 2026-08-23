import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";


const bundlePath = process.argv[2];
const requestedPaths = [];
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

function fetchJSON(path) {
  requestedPaths.push(path);
  if (path.includes("/board?profile=")) {
    return Promise.resolve({
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
  if (typeof node === "string") {
    return node;
  }
  if (!node || !Array.isArray(node.children)) {
    return "";
  }
  return node.children.map(textContent).join(" ");
}

const renderedText = textContent(readyTree);
assert.deepEqual(requestedPaths, [
  "/api/plugins/map-governance/board?profile=worker",
  "/api/plugins/map-governance/health?profile=worker",
]);
assert.match(renderedText, /No Maps are bound/);
assert.match(renderedText, /Bind an existing GitHub Map Issue/);
assert.match(renderedText, /Plugin ready/);

process.stdout.write("dashboard shell ready\n");
