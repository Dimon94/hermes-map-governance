import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";


const bundlePath = process.argv[2];
const mode = process.argv[3] || "empty";
const requestedPaths = [];
const requestedOptions = [];
const openedSessions = [];
const effects = [];
const confirmations = [];
const state = [];
let hookIndex = 0;
let registeredName = null;
let registeredPage = null;
let chairmanDecisionRecorded = false;

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
  if (path.includes("/approvals/approval-delivery-001/decision?profile=")) {
    chairmanDecisionRecorded = true;
    return Promise.resolve({ status: "approved" });
  }
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
  if (path.includes("/maps/I_atlas_41/commission?profile=")) {
    return Promise.resolve({
      map_id: "I_atlas_41",
      state: "active",
      checkpoint: {
        state: "tracker_confirmed",
        record_id: "commission-ready-I_atlas_41",
      },
    });
  }
  if (path.includes("/maps/I_atlas_41/runtime?profile=")) {
    return Promise.resolve({
      map_id: "I_atlas_41",
      state: "repair_required",
      failure: {
        reason: "pane_ownership_mismatch",
        retryable: false,
        repair_required: true,
        resource_disposition: "no_cleanup_without_verified_ownership",
      },
    });
  }
  if (path.includes("/maps/I_atlas_41?profile=")) {
    const approval = mode === "approval" ? {
      request_id: "approval-delivery-001",
      decision_class: "delivery_authorization",
      proposed_action: "transition_map",
      alternatives: ["Authorize delivery", "Return to discovery"],
      rationale: "Delivery evidence is ready.",
      cost_risk: "Two engineering weeks.",
      evidence: ["https://github.com/acme/atlas/issues/41#issuecomment-7"],
      requested_scope: { map_id: "I_atlas_41" },
      decision_payload: {
        expected_stage: "awaiting-approval",
        requested_stage: "authorized",
      },
      payload_hash: "sha256:approval-payload",
      status: chairmanDecisionRecorded ? "approved" : "pending",
      requested_at: "2026-08-23T09:00:00Z",
      expires_at: chairmanDecisionRecorded ? "2026-08-24T09:30:00Z" : null,
    } : null;
    return Promise.resolve({
      id: "I_atlas_41",
      recent_decisions: [
        {
          decision_id: "decision-atlas-scope-002",
          type: "operational",
          rationale: "Hold the public beta until cohort evidence is reviewed.",
          authority: "ceo",
          affected_stage: "authorized",
          timestamp: "2026-08-23T09:28:00Z",
        },
      ],
      approvals: approval ? { count: 1, items: [approval] } : { count: 0, items: [] },
      delivery_summary: mode === "pm-report" ? {
        state: "reported",
        count: 2,
        latest: {
          assignment_map_id: "I_atlas_41",
          record_id: "failure-dashboard-001",
          type: "failure",
          summary: "Delivery terminated because the source contract is invalid.",
          timestamp: "2026-08-23T10:05:00Z",
          failure_code: "invalid-source-contract",
        },
        badges: [
          { type: "terminal_failure", count: 1 },
        ],
      } : { state: "not_reported" },
      pm_reports: mode === "pm-report" ? [
        {
          assignment_map_id: "I_atlas_41",
          record_id: "failure-dashboard-001",
          type: "failure",
          summary: "Delivery terminated because the source contract is invalid.",
          timestamp: "2026-08-23T10:05:00Z",
          failure_code: "invalid-source-contract",
        },
        {
          assignment_map_id: "I_atlas_41",
          record_id: "question-dashboard-001",
          type: "question",
          summary: "May legacy aliases remain available?",
          timestamp: "2026-08-23T10:02:00Z",
          blocking: false,
          continuation_requirement: "A yes/no answer about legacy aliases.",
          correlation_id: "compatibility-choice-001",
          decision_class: "product",
          scope: { map_id: "I_atlas_41", area: "compatibility" },
          evidence: ["Two supported clients still send aliases."],
          options: ["Keep aliases", "Remove aliases"],
        },
      ] : [],
      decision_acknowledgments: mode === "pm-report" ? [
        {
          correlation_id: "compatibility-choice-001",
          outcome: "continue",
          turn_id: "decision:compatibility-choice-001:continue",
          tracker: { id: "IC_decision_1", url: "https://github.com/acme/atlas/issues/41#issuecomment-1" },
          acknowledged_at: "2026-08-23T10:04:00Z",
        },
      ] : [],
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
        stage: mode === "approval"
          ? "awaiting-approval"
          : mode === "pm-report"
            ? "delivery"
            : mode === "transition-failure"
              ? "delivery"
            : "authorized",
        available_transitions: mode === "approval"
          ? ["authorized", "discovery", "parked"]
          : mode === "pm-report"
            ? ["decision", "acceptance", "parked"]
            : mode === "transition-failure"
              ? ["decision", "acceptance", "parked"]
          : ["delivery", "parked"],
        decision_summary: {
          count: 1,
          latest: {
            decision_id: "decision-atlas-market-001",
            type: "product",
            rationale: "Launch to the research cohort before widening access.",
            authority: "ceo",
            affected_stage: "authorized",
            timestamp: "2026-08-23T09:25:00Z",
          },
        },
        approval_summary: mode === "approval" ? {
          count: 1,
          pending_count: chairmanDecisionRecorded ? 0 : 1,
          latest: {
            request_id: "approval-delivery-001",
            status: chairmanDecisionRecorded ? "approved" : "pending",
          },
        } : { count: 0, pending_count: 0, latest: null },
        delivery_summary: mode === "pm-report" ? {
          state: "reported",
          count: 2,
          latest: {
            assignment_map_id: "I_atlas_41",
            record_id: "failure-dashboard-001",
            type: "failure",
            summary: "Delivery terminated because the source contract is invalid.",
            timestamp: "2026-08-23T10:05:00Z",
            failure_code: "invalid-source-contract",
          },
          badges: [
            { type: "terminal_failure", count: 1 },
          ],
        } : { state: "not_reported" },
        external_effects: mode === "outbox-terminal" ? {
          state: "needs_repair",
          pending_count: 0,
          retry_scheduled_count: 0,
          leased_count: 0,
          succeeded_count: 2,
          terminal_count: 1,
          latest_terminal: {
            effect_id: "tracker-decision:I_atlas_41:terminal-001",
            terminal_outcome: {
              message: "GitHub rejected the stable governance marker.",
            },
          },
        } : {
          state: "healthy",
          pending_count: 0,
          retry_scheduled_count: 0,
          leased_count: 0,
          succeeded_count: 0,
          terminal_count: 0,
          latest_terminal: null,
        },
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
  if (path.includes("/doctor?profile=")) {
    return Promise.resolve({
      status: "pass",
      checked_at: "2026-08-24T01:30:00Z",
      summary: { pass: 18, warning: 1, fail: 0 },
      checks: [{
        id: "repository.acme/atlas.publisher",
        status: "warning",
        summary: "Remote publication is not required for execution readiness",
        evidence: { required: false },
        remediation: { description: "No action is needed until publication is authorized." },
      }],
    });
  }
  if (path.includes("/setup/plan?profile=")) {
    return Promise.resolve({
      status: "planned",
      plan_id: "setup-plan:safe-preview",
      expires_at: "2026-08-24T01:45:00Z",
      actions: [
        {
          action_id: "config.prerequisites",
          description: "Store Map Governance behavioral prerequisites",
          before: null,
          after: { authorities: { publisher: { credential_ref: "gh:github.com:release-bot" } } },
        },
      ],
    });
  }
  if (path.includes("/setup/apply?profile=")) {
    return Promise.resolve({
      status: "applied",
      applied_action_ids: ["config.prerequisites"],
      not_selected_action_ids: [],
      readback: "confirmed",
    });
  }
  return Promise.reject(new Error(`Unexpected path: ${path}`));
}

globalThis.window = {
  location: { search: "?profile=worker" },
  confirm(message) {
    confirmations.push(message);
    return true;
  },
  prompt() {
    return "Approved for the declared scope.";
  },
  crypto: {
    randomUUID() {
      return "transition-approved-001";
    },
  },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement },
    fetchJSON,
    hooks: {
      useCallback: (callback) => callback,
      useEffect: (effect) => effects.push(effect),
      useRef: (initialValue) => ({ current: initialValue }),
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
  assert.match(
    renderedText,
    mode === "approval"
      ? /awaiting-approval/
      : mode === "pm-report" || mode === "transition-failure"
        ? /delivery/
        : /authorized/,
  );
  assert.match(renderedText, /1 confirmed decision/);
  assert.match(renderedText, /product/);
  assert.match(renderedText, /Launch to the research cohort before widening access/);
  const detailButton = findNode(
    readyTree,
    (node) => node.type === "Button" && /View Map detail/.test(textContent(node)),
  );
  assert.ok(detailButton, "Map card exposes its application detail projection");
  detailButton.props.onClick();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const detailIndex = requestedPaths.findIndex(
    (path) => path.includes("/maps/I_atlas_41?profile="),
  );
  assert.notEqual(detailIndex, -1);
  assert.equal(
    requestedPaths[detailIndex],
    "/api/plugins/map-governance/maps/I_atlas_41?profile=worker",
  );
  hookIndex = 0;
  const detailTree = registeredPage();
  const detailText = textContent(detailTree);
  assert.match(detailText, /Recent confirmed decisions/);
  assert.match(detailText, /decision-atlas-scope-002/);
  assert.match(detailText, /Hold the public beta until cohort evidence is reviewed/);
  assert.match(
    detailText,
    mode === "approval"
      ? /Chairman approval packets.*approval-delivery-001.*Delivery evidence is ready/s
      : /Chairman approval packets No approval requests/,
  );
  assert.match(
    detailText,
    mode === "pm-report" ? /Delivery: reported/ : /Delivery: not_reported/,
  );
  if (mode === "pm-report") {
    assert.match(renderedText, /2 PM executive reports/);
    assert.match(renderedText, /terminal failure/i);
    assert.match(renderedText, /Delivery terminated because the source contract is invalid/);
    assert.match(detailText, /PM executive reports/);
    assert.match(detailText, /failure-dashboard-001/);
    assert.match(detailText, /question-dashboard-001/);
    assert.match(detailText, /A yes\/no answer about legacy aliases/);
    assert.match(detailText, /compatibility-choice-001/);
    assert.match(detailText, /"area":"compatibility"/);
    assert.match(detailText, /Keep aliases/);
    assert.match(detailText, /Remove aliases/);
    assert.match(detailText, /Two supported clients still send aliases/);
    assert.match(detailText, /PM decision acknowledgments/);
    assert.match(detailText, /compatibility-choice-001 · continue/);
    assert.match(detailText, /Committed record: IC_decision_1/);
    assert.doesNotMatch(detailText, /worker lane|worktree|pane log|implementation ticket/i);
  }
  if (mode === "outbox-terminal") {
    assert.match(renderedText, /External effects need operator repair/);
    assert.match(renderedText, /GitHub rejected the stable governance marker/);
    assert.match(renderedText, /maps outbox repair --effect tracker-decision:I_atlas_41:terminal-001/);
  }
  if (mode === "approval") {
    assert.match(detailText, /Alternatives.*Authorize delivery.*Return to discovery/s);
    assert.match(detailText, /Cost \/ risk: Two engineering weeks/);
    assert.match(detailText, /Decision content:.*awaiting-approval.*authorized/s);
    assert.match(detailText, /sha256:approval-payload/);
    assert.match(detailText, /Expiry starts on approval/);
    const approveButton = findNode(
      detailTree,
      (node) => node.type === "Button" && /Approve/.test(textContent(node)),
    );
    const rejectButton = findNode(
      detailTree,
      (node) => node.type === "Button" && /Reject/.test(textContent(node)),
    );
    const revisionButton = findNode(
      detailTree,
      (node) => node.type === "Button" && /Request revision/.test(textContent(node)),
    );
    assert.ok(approveButton && rejectButton && revisionButton);
    assert.equal(approveButton.props["aria-label"], "Approve approval-delivery-001");
    approveButton.props.onClick();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const decisionIndex = requestedPaths.findIndex(
      (path) => path.includes("/approvals/approval-delivery-001/decision?"),
    );
    assert.notEqual(decisionIndex, -1);
    assert.deepEqual(JSON.parse(requestedOptions[decisionIndex].body), {
      decision: "approved",
      note: "Approved for the declared scope.",
    });
    assert.match(confirmations[0], /Confirm chairman decision/);
    hookIndex = 0;
    const approvedTree = registeredPage();
    const protectedTransition = findNode(
      approvedTree,
      (node) => node.type === "Button" && /Move to authorized/.test(textContent(node)),
    );
    assert.ok(protectedTransition);
    protectedTransition.props.onClick();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const protectedIndex = requestedPaths.findIndex(
      (path) => path.includes("/transitions?profile="),
    );
    assert.notEqual(protectedIndex, -1);
    assert.deepEqual(JSON.parse(requestedOptions[protectedIndex].body), {
      map_id: "I_atlas_41",
      expected_stage: "awaiting-approval",
      requested_stage: "authorized",
      approval_request_id: "approval-delivery-001",
      mutation_id: "approval-delivery-001",
    });
    assert.match(confirmations[1], /Confirm major Map action/);
  }
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
  if (mode === "commission") {
    const commissionButton = findNode(
      readyTree,
      (node) => node.type === "Button" && /Commission Hermes PM/.test(textContent(node)),
    );
    assert.ok(commissionButton, "authorized Map exposes explicit PM commission");
    commissionButton.props.onClick();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const commissionIndex = requestedPaths.findIndex(
      (path) => path.includes("/maps/I_atlas_41/commission?profile="),
    );
    assert.notEqual(commissionIndex, -1);
    assert.deepEqual(JSON.parse(requestedOptions[commissionIndex].body), {
      session_id: "canonical-tip",
    });
    hookIndex = 0;
    assert.match(
      textContent(registeredPage()),
      /PM runtime: active.*Revalidate \/ resume Hermes PM/s,
    );
  }
  if (mode === "runtime-status") {
    const statusButton = findNode(
      readyTree,
      (node) => node.type === "Button" && /Check PM runtime/.test(textContent(node)),
    );
    assert.ok(statusButton, "Map exposes an explicit PM runtime status action");
    statusButton.props.onClick();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const statusIndex = requestedPaths.findIndex(
      (path) => path.includes("/maps/I_atlas_41/runtime?profile="),
    );
    assert.notEqual(statusIndex, -1);
    assert.match(requestedPaths[statusIndex], /session_id=canonical-tip/);
    hookIndex = 0;
    const statusTree = registeredPage();
    assert.match(
      textContent(statusTree),
      /PM runtime: repair_required.*pane_ownership_mismatch.*no_cleanup_without_verified_ownership.*Revalidate \/ resume Hermes PM/s,
    );
  }
  assert.match(renderedText, /2026-08-23T07:30:00Z/);
  const transitionButton = findNode(
    readyTree,
    (node) => node.type === "Button" && new RegExp(
      mode === "approval"
        ? "Move to authorized"
        : mode === "pm-report" || mode === "transition-failure"
          ? "Move to decision"
          : "Move to parked",
    ).test(textContent(node)),
  );
  assert.ok(transitionButton, "Map card exposes an explicit governed transition control");
  if (mode !== "approval") {
    transitionButton.props.onClick();
  }

  hookIndex = 0;
  const pendingTree = registeredPage();
  assert.match(
    textContent(pendingTree),
    mode === "pm-report" || mode === "transition-failure" ? /delivery/ : /authorized/,
  );

  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const transitionIndex = requestedPaths.findIndex((path) => path.includes("/transitions?profile="));
  if (mode !== "approval") {
    assert.notEqual(transitionIndex, -1);
    assert.equal(
      requestedPaths[transitionIndex],
      "/api/plugins/map-governance/transitions?profile=worker",
    );
    assert.equal(requestedOptions[transitionIndex].method, "POST");
    assert.deepEqual(JSON.parse(requestedOptions[transitionIndex].body), {
      map_id: "I_atlas_41",
      expected_stage: mode === "pm-report" || mode === "transition-failure"
        ? "delivery"
        : "authorized",
      requested_stage: mode === "pm-report" || mode === "transition-failure"
        ? "decision"
        : "parked",
    });
  }
  if (mode === "transition-failure") {
    hookIndex = 0;
    const failedTree = registeredPage();
    const failedText = textContent(failedTree);
    assert.match(failedText, /delivery/);
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
const doctorButton = findNode(
  readyTree,
  (node) => node.type === "Button" && /Doctor|Run prerequisite doctor/.test(textContent(node)),
);
assert.ok(doctorButton, "Maps page exposes read-only prerequisite doctor");
doctorButton.props.onClick();
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));
hookIndex = 0;
const doctorTree = registeredPage();
assert.match(textContent(doctorTree), /Prerequisite doctor/);
assert.match(textContent(doctorTree), /Remote publication is not required/);
const setupButton = findNode(
  readyTree,
  (node) => node.type === "Button" && /Setup(?: prerequisites)?/.test(textContent(node)),
);
assert.ok(setupButton, "Maps page exposes explicit prerequisite setup");
setupButton.props.onClick();
hookIndex = 0;
const setupEditorTree = registeredPage();
const setupTextarea = findNode(
  setupEditorTree,
  (node) => node.type === "textarea" && node.props.id === "maps-setup-json",
);
assert.ok(setupTextarea, "Setup starts with operator-provided desired JSON");
setupTextarea.props.onChange({ target: { value: JSON.stringify({ schema_version: 1 }) } });
hookIndex = 0;
const setupReadyTree = registeredPage();
const planButton = findNode(
  setupReadyTree,
  (node) => node.type === "Button" && /Generate setup plan/.test(textContent(node)),
);
planButton.props.onClick();
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));
const planIndex = requestedPaths.findIndex((path) => path.includes("/setup/plan?profile="));
assert.notEqual(planIndex, -1);
assert.deepEqual(JSON.parse(requestedOptions[planIndex].body), {
  desired: { schema_version: 1 },
});
hookIndex = 0;
const setupPlanTree = registeredPage();
assert.match(textContent(setupPlanTree), /config\.prerequisites/);
assert.match(textContent(setupPlanTree), /Before.*After/s);
assert.doesNotMatch(textContent(setupPlanTree), /token|password|secret/i);
const firstAction = findNode(
  setupPlanTree,
  (node) => node.type === "input" && node.props.type === "checkbox",
);
firstAction.props.onChange();
hookIndex = 0;
const setupSelectedTree = registeredPage();
const applyButton = findNode(
  setupSelectedTree,
  (node) => node.type === "Button" && /Apply selected actions/.test(textContent(node)),
);
assert.equal(applyButton.props.disabled, false);
applyButton.props.onClick();
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));
const applyIndex = requestedPaths.findIndex((path) => path.includes("/setup/apply?profile="));
assert.notEqual(applyIndex, -1);
assert.deepEqual(JSON.parse(requestedOptions[applyIndex].body).selected_action_ids, [
  "config.prerequisites",
]);
hookIndex = 0;
assert.match(textContent(registeredPage()), /readback confirmed/);
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
    : mode === "approval"
      ? "dashboard chairman approval ready\n"
    : mode === "pm-report"
      ? "dashboard PM reporting ready\n"
    : mode === "outbox-terminal"
      ? "dashboard Outbox repair ready\n"
    : mode === "commission"
      ? "dashboard PM commission ready\n"
      : "dashboard board ready\n",
);
