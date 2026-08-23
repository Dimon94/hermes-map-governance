# Hermes Map Governance Plugin Spec

## Problem Statement

用户希望把 Hermes 从一个被动执行需求的 Agent，提升为真正承担产品治理职责的 CEO。董事长负责出资、投资判断以及对重大决策的审核和批准；Hermes CEO 负责理解想法、维护每个 Map 的长期上下文、作出授权范围内的产品决策、回答交付协调者的问题，并把重大事项升级给董事长；Hermes PM 负责运行既有的交付编排流程，再把具体实现分派给 Codex CLI 或 Claude Code。

现有 Hermes Kanban 面向 worker 调度：`ready`、`running`、`blocked`、`review` 等状态与 worker claim、并发预算、失败恢复和自动派发有执行语义。把 CEO 治理中的“交付中”直接写成 Kanban `running`，会把业务进度误认为真实 worker 进程，污染调度与恢复逻辑。另一方面，如果把 implementation tickets、worktree、pane 和 worker 日志全部搬到 CEO 看板，就会制造第二套执行真相源，使董事长难以区分战略状态和实现细节。

用户需要一个独立的治理平面：同一个 Hermes Project 下的 Map 聚合到同一块董事会；每个 Map 对应一张卡片和一条长期 Hermes CEO 会话；顶层 Map Issue 只呈现需求基本进度、CEO 决策、PM 问题及回答和验收证据；实现 tickets、lane registry、Git 和 PR/MR 继续分别承担自己的执行真相。系统需要在 Hermes 客户端内完成这些能力，并能在进程重启、会话压缩、Herdr workspace 重建和网络重试后恢复同一组持久坐标。

## Solution

构建一个独立安装、原生显示在 Hermes Desktop 中的 `map-governance` 插件。插件提供新的“Maps / 董事会”页面、治理后端、Map Registry、审批策略、持久 Outbox、GitHub Tracker Adapter、Hermes CEO Session Runner 和 Herdr Coordinator Runtime。它不复用现有 Kanban 的任务表和执行状态机，也不增加 Hermes 核心模型 Tool。

插件同时注册两个显式 Skill 和两个受 profile 限制的插件 Tool：`map-governance:ceo` 规定 CEO 的治理职责和升级边界；`map-governance:pm` 规定 Hermes PM 如何加载现有 `delivery-pipeline`、固定使用 Herdr 调度运行时、汇报执行摘要并在需要决策时安全停靠；CEO Tool 只允许治理与决策动作，PM Tool 只允许进度、提问和验收请求动作。

每个 Map Issue 是治理事实的单一真相源。董事会卡片从 Issue 的 open/closed 状态、唯一阶段标签和结构化决策评论派生，不保存一份竞争性的权威状态。插件自己的数据库只保存跨系统关系、审批执法事实、传输状态和可重建投影缓存。一个 Map 绑定一个稳定的 CEO profile 会话；卡片打开时采用精确标题查找和 adopt-before-mint，避免重复会话。Map 特定上下文通过第一条用户消息进入历史，不动态重建系统提示，保持会话级 prompt caching 稳定。

交付获批后，插件在自己拥有的命名 Herdr session 中为该 Map 建立 workspace，并在根 pane 中启动一个 Hermes PM Agent。PM 处于 `HERDR_ENV=1` 的真实 Herdr 上下文中，加载 `delivery-pipeline` 和 Herdr Skill，再按既有 routing 规则创建 Codex CLI 或 Claude Code panes。插件董事会只读取 PM 提交的 Gate、基本进度、blocker 和验收证据，不读取或复制 implementation ticket、pane、worktree、commit 的详细状态。

## User Stories

1. As a 董事长, I want to see every active Map grouped under its Hermes Project, so that I can understand where capital and attention are currently allocated.
2. As a 董事长, I want one board card to represent one top-level Map rather than one implementation ticket, so that the board remains an executive view.
3. As a 董事长, I want to open a Map card and land in its dedicated Hermes CEO conversation, so that every initiative has a durable decision context.
4. As a 董事长, I want reopening the same Map to reuse the original CEO conversation, so that decisions are not split across duplicate chats.
5. As a 董事长, I want major decisions to wait for my explicit approval, so that the CEO cannot expand investment or irreversible scope without authority.
6. As a 董事长, I want approval requests to show the question, options, CEO recommendation, impact and evidence, so that I can make an informed decision quickly.
7. As a 董事长, I want approval and rejection actions recorded against a stable decision identifier, so that later audits can reconstruct who authorized what.
8. As a 董事长, I want the board to distinguish discussion, authorization, delivery, blocking decision, acceptance and completion, so that I can read portfolio health at a glance.
9. As a 董事长, I want to park or cancel a Map without touching implementation details manually, so that I can control investment at the governance level.
10. As a 董事长, I want final acceptance and remote publication to be separately governable, so that completing code does not silently equal shipping a product.
11. As a 董事长, I want a read-only portfolio view across projects, so that I can compare initiatives without creating copied portfolio records.
12. As a 董事长, I want stale or offline projections clearly marked, so that cached data is never mistaken for current tracker truth.
13. As a Hermes CEO, I want one conversation to manage exactly one Map, so that my context and authority remain unambiguous.
14. As a Hermes CEO, I want to turn a loose idea into or bind it to a Wayfinder Map, so that strategic discovery has a durable starting artifact.
15. As a Hermes CEO, I want to record product decisions in the Map Issue, so that the PM and future reviewers share one decision record.
16. As a Hermes CEO, I want to answer operational and product questions inside the approved envelope, so that delivery is not unnecessarily blocked on the chairman.
17. As a Hermes CEO, I want the policy module to classify budget, scope, schedule, security, legal, cancellation and publication decisions, so that escalation behavior is consistent.
18. As a Hermes CEO, I want to formulate a recommendation before escalating a major question, so that the chairman receives a decision packet rather than raw uncertainty.
19. As a Hermes CEO, I want to commission a Hermes PM only after delivery authorization exists, so that implementation cannot begin from an unapproved conversation.
20. As a Hermes CEO, I want to inspect PM checkpoints and acceptance evidence without seeing lane logs, so that I can govern outcomes rather than micromanage execution.
21. As a Hermes CEO, I want to request changes at acceptance and return the Map to delivery, so that unmet product outcomes re-enter the correct loop.
22. As a Hermes CEO, I want my Map context to survive conversation compression, so that long-running initiatives retain their stable identity.
23. As a Hermes CEO, I want Map-specific data added as conversation content rather than mutable system instructions, so that prompt caching remains effective.
24. As a Hermes PM, I want to receive an authorized Map URL and project working directory, so that I can reconstruct the delivery chain from durable artifacts.
25. As a Hermes PM, I want `delivery-pipeline` preloaded at startup, so that the orchestration protocol is not left to model discovery.
26. As a Hermes PM, I want the runtime forced to Herdr when running outside Codex Desktop thread tools, so that dispatch behavior is deterministic.
27. As a Hermes PM, I want to run inside a Herdr-managed pane, so that Herdr control commands have valid caller context and stable workspace coordinates.
28. As a Hermes PM, I want one Herdr workspace per Map, so that its integration worktree and execution panes are isolated from other initiatives.
29. As a Hermes PM, I want each implementation ticket assigned to one Codex or Claude lane, so that lane ownership and recovery remain deterministic.
30. As a Hermes PM, I want frontend/design tickets routed to Claude and other implementation tickets routed according to the delivery policy, so that existing delivery routing remains reusable.
31. As a Hermes PM, I want to submit executive checkpoints separately from lane registry updates, so that the CEO board stays concise.
32. As a Hermes PM, I want to request a decision through a structured governance action and end my turn idle, so that the system can safely resume me after an answer.
33. As a Hermes PM, I want non-blocking work to continue when one decision request does not halt the entire Map, so that the board does not overstate local blockers.
34. As a Hermes PM, I want a Map to enter the decision stage only when the entire delivery path is blocked, so that executive state reflects real impact.
35. As a Hermes PM, I want to stop after local integration when publication authority is absent, so that local completion cannot bypass the release gate.
36. As a Hermes PM, I want to resume the same Hermes session and Herdr workspace after a restart, so that orchestration does not fork or lose lanes.
37. As a Codex or Claude worker, I want a complete implementation packet with all shared decisions, so that I do not invent product policy independently of sibling workers.
38. As a Codex or Claude worker, I want no access to CEO governance actions, so that implementation agents cannot approve their own work or investment.
39. As a Codex or Claude worker, I want to report through the existing ticket, lane and Git evidence chain, so that the governance plugin does not become a second execution tracker.
40. As a project operator, I want to bind an existing Map Issue without recreating it, so that current projects can adopt the plugin incrementally.
41. As a project operator, I want setup diagnostics for profiles, external Skill directories, `gh`, Herdr and agent integrations, so that missing prerequisites fail before delivery starts.
42. As a project operator, I want the plugin to use its own named Herdr session, so that it never controls or closes my personal Herdr panes.
43. As a project operator, I want every Herdr workspace and pane mutation based on IDs returned by Herdr, so that layout changes do not target guessed coordinates.
44. As a project operator, I want failed tracker writes retried idempotently through an Outbox, so that restarts do not duplicate comments, labels or approvals.
45. As a project operator, I want card transitions to commit only after tracker success, so that the UI never presents an unpersisted state as complete.
46. As a project operator, I want plugin configuration stored under the plugin settings tree, so that behavioral settings do not introduce new environment variables.
47. As a project operator, I want the CEO and PM profiles to expose only their required plugin toolsets, so that tool schemas and authority stay narrow.
48. As a project operator, I want project identity resolved from the CEO control-plane project catalog, so that profile-local project databases do not create competing project identities.
49. As a project operator, I want PM runtimes to receive absolute repository coordinates rather than resolve a separate project database, so that profile isolation is preserved.
50. As a project operator, I want a repair command to rebuild projections and reconcile missing runtime coordinates, so that routine corruption does not require editing SQLite manually.
51. As a project operator, I want the system to queue work when Hermes Desktop is closed, so that a PM decision request is not lost merely because the board UI is offline.
52. As a project operator, I want queued CEO work routed through the live Desktop session when present and a safe headless resume when absent, so that the same canonical conversation remains authoritative.
53. As a security-conscious operator, I want business approval to be distinct from shell command approval, so that governance records are not confused with terminal consent.
54. As a security-conscious operator, I want PM and workers to lack remote publication credentials by default, so that Skill compliance is backed by credential separation.
55. As a security-conscious operator, I want publisher authority granted only after a chairman approval record, so that push and PR/MR creation have a verifiable gate.
56. As an auditor, I want every decision, approval, stage change and publication linked to its Map Issue, so that governance history can be reconstructed without reading chat transcripts.
57. As an auditor, I want the plugin registry to store relationship coordinates but not duplicate Issue truth, so that each fact has one authoritative owner.
58. As an auditor, I want projection caches explicitly identified as rebuildable, so that they cannot silently become a shadow database.
59. As a Hermes maintainer, I want this capability delivered as an external standalone plugin, so that user-specific governance does not widen the core agent waist.
60. As a Hermes maintainer, I want plugin tools registered in named toolsets rather than the core tool list, so that unrelated sessions pay no schema cost.
61. As a Hermes maintainer, I want the existing Kanban dispatcher untouched, so that governance cards cannot regress worker scheduling, concurrency or failure recovery.
62. As a Hermes maintainer, I want dashboard and Tool surfaces to call the same governance module, so that policy and state transitions cannot drift by interface.
63. As a Hermes maintainer, I want profile and session identity resolved through request-scoped context rather than process environment, so that multiplexed Desktop sessions cannot leak authority.
64. As a Hermes maintainer, I want profile-scoped plugin manager behavior respected, so that the CEO and PM load the correct plugin state and configuration.
65. As a Hermes maintainer, I want external effects isolated behind Tracker, Session Runner and Coordinator Runtime adapters, so that the governance state machine can be tested at one high seam.
66. As a Hermes maintainer, I want all retries keyed by stable idempotency identifiers, so that at-least-once event delivery remains safe.
67. As a Hermes maintainer, I want the plugin to degrade to read-only when GitHub is unavailable, so that users can inspect cached governance state without making false writes.
68. As a Hermes maintainer, I want live updates delivered through a plugin event stream, so that board cards, approval badges and runtime state refresh without polling every screen independently.
69. As a Hermes maintainer, I want no unsolicited changes to existing user sessions, panes or worktrees, so that plugin ownership remains explicit and recoverable.
70. As a Hermes maintainer, I want a staged rollout beginning with bind-existing-Map and manual approval, so that session identity and single-source semantics are proven before autonomous dispatch is enabled.

## Implementation Decisions

- The product will be an external standalone Hermes plugin named `map-governance`. It will bundle a native plugin registration surface and a dashboard extension so it appears as a first-class Hermes Desktop page while remaining outside the core repository waist.
- Existing Kanban tasks and their database will not store governance cards. The new board may reuse Hermes theme variables and interaction patterns, but it owns a separate governance domain whose states have no worker claim, concurrency or reclaim semantics.
- The central deep module will expose one application interface for binding Maps, reading board projections, applying governed transitions, recording decisions, commissioning/resuming runtimes and reconciling external state. Dashboard REST handlers, plugin Tools and CLI commands will be thin adapters over this interface.
- External effects will sit behind three concrete seams: a Tracker Adapter for Map Issues, a CEO Session Runner for canonical Hermes sessions, and a Coordinator Runtime for the plugin-owned Herdr session. The first release will implement GitHub, Hermes session/gateway plus headless resume, and local Herdr CLI adapters only.
- The CEO profile's Hermes Project catalog is the canonical project catalog for the governance plane. A Map binding references that project identity and records the primary repository coordinate needed for PM startup. PM profiles do not independently reinterpret project identity.
- One project produces one board route. Same-project Map grouping is a first-class relationship, not a repeated label convention. A separate portfolio route aggregates project boards read-only.
- The Map Issue is the governance truth source. Exactly one active `map-stage/*` label is allowed while the Issue is open. Active stages are `discovery`, `awaiting-approval`, `authorized`, `delivery`, `decision`, `acceptance` and `parked`. A closed Issue with completed reason projects to `done`; a closed Issue with not-planned reason projects to `cancelled`.
- The `decision` stage is reserved for a whole-Map blocker. Non-blocking decision requests remain in `delivery` and appear as badges/inbox items, allowing independent delivery lanes to continue.
- Dragging a card is a request to transition the Map Issue, not a local optimistic state mutation. The backend validates the transition and authority, writes the tracker, reads back the result, and only then emits the new projection. Failed writes leave the card at its previous authoritative stage.
- Map Issue comments will carry human-readable structured records with stable identifiers for CEO decisions, PM questions, chairman approvals, executive checkpoints and acceptance evidence. Implementation lane details remain in the existing delivery-pipeline artifact chain and are not rendered by the board.
- The plugin database will own only cross-system coordinates and enforcement state: Map bindings, runtime bindings, approval ledger, persistent Outbox and rebuildable projection cache. It will not own an authoritative copy of the Map stage or Issue body.
- Map bindings will enforce uniqueness for tracker Map identity and for the CEO session relationship. Runtime bindings will enforce at most one active PM runtime per Map while retaining closed coordinates for audit and recovery.
- Approval records will include a stable request identifier, requested capability, normalized decision payload hash, approver identity, timestamp, expiry/consumption state and the resulting tracker record reference. A model assertion that approval happened is insufficient without a matching ledger record.
- The default chairman-required capabilities are delivery authorization, budget increase, scope expansion outside the approved Map, material schedule change beyond configured limits, cancellation, remote publication and final acceptance. Product and operational decisions inside the authorized envelope remain CEO authority.
- Authority thresholds are behavioral plugin settings under the normal Hermes configuration tree. No new environment variables will be introduced for budget, schedule, scope or decision policy.
- The CEO will use one dedicated Hermes profile with a byte-stable system prompt and fixed toolset. Every Map gets one visible long-lived conversation in that profile.
- CEO session identity will use a stable exact title derived from immutable tracker coordinates, not a mutable Map title. Card opening uses exact lookup, compression-tip resolution and adopt-before-mint. Concurrent creation is deduplicated with a per-Map lease.
- A newly created session is not considered bound until a bootstrap prompt has persisted the session. The bootstrap identifies the Map, loads `map-governance:ceo`, states the authority envelope and records the initial binding. Map-specific content is a user turn, not a dynamically generated system-prompt section.
- Session compression must preserve or resolve the Map relationship. The binding registry retains the root session coordinate, and session lookup resolves the live continuation before opening or waking the CEO.
- The plugin will register `map-governance:ceo` and `map-governance:pm` as explicit plugin Skills. CEO bootstrap explicitly loads its Skill. PM startup preloads its wrapper Skill, `delivery-pipeline` and the locally generated Herdr Skill.
- Existing `delivery-pipeline`, Wayfinder, spec, ticket, implementation and review owner Skills remain independently owned. The plugin orchestrates their use but does not copy their instructions or assume ownership of their artifacts.
- The CEO Skill forbids implementation and Herdr lane control. It may conduct discovery, create or bind a Map, record decisions, answer PM questions, request approval, commission delivery and govern acceptance.
- The PM Skill forces `dispatch_runtime: herdr` when running inside Hermes, forbids direct implementation, requires executive checkpoint reporting, and requires decision requests to be persisted before the PM ends its turn idle.
- The plugin will register two model-facing tools in two named toolsets. The CEO tool supports inspection, decision recording, decision answering, approval requests, authorized commission, acceptance, parking and cancellation. The PM tool supports checkpoints, decision requests, answer reads, acceptance requests and failure reports.
- Tool visibility is controlled by the CEO and PM profile toolsets. Runtime handlers additionally validate the active request-scoped profile/session relationship and the action's authority. Dependency checks are not used as session identity gates.
- The two Tools will not be added to Hermes core tool lists. Worker Codex/Claude sessions will not receive either governance toolset.
- A dedicated plugin-owned Herdr session will contain one workspace per Map. The plugin always targets that named session and recorded workspace/agent IDs; it never relies on a UI-focused pane or manipulates a workspace it did not create.
- Each Map workspace starts one Hermes PM Agent in its root pane with the project repository as initial working directory and the required Skills preloaded. The PM then follows delivery-pipeline to create or enter the Map Integration Worktree and to create execution panes.
- The PM must run inside a Herdr-managed pane so the Herdr Skill's `HERDR_ENV=1` requirement is satisfied. The Desktop backend itself does not impersonate a Herdr caller.
- New execution lanes use Herdr. Existing lane registry runtime coordinates remain authoritative and are resumed according to their recorded runtime; changing the preferred runtime never migrates an active lane.
- Codex CLI and Claude Code remain execution adapters selected by delivery-pipeline routing. The plugin records only the PM runtime coordinate and executive outcome; worker pane coordinates remain delivery-pipeline details.
- A PM decision request writes the structured tracker record and Outbox event before returning. For CEO-authority questions, the Outbox dispatcher wakes the canonical CEO session. For chairman-authority questions, it first asks the CEO to prepare a recommendation and then creates a chairman approval inbox item.
- When the Map's CEO session is actively mounted in Desktop, the dashboard session bridge handles a queued CEO turn. When no live UI lease exists, a guarded headless resume uses the same stored session. At most one CEO turn per Map may run at a time.
- After an answer is durably recorded, the Coordinator Runtime prompts the idle PM Agent using the plugin-owned Herdr session and stable agent name. The PM re-reads the decision record before resuming delivery.
- The Outbox implements at-least-once delivery with stable event identifiers, payload hashes, leases, exponential retry and terminal failure visibility. Every external write uses an idempotency key or read-before-write reconciliation.
- The dashboard backend exposes project boards, Map detail, binding, governed transitions, decisions, approvals, commissioning, repair and an event stream. Generic unrestricted mutation endpoints are not provided; high-impact actions have explicit routes and policy checks.
- The UI provides project selection, governance columns, stale markers, pending-decision badges, card detail, CEO-session opening, approval packets and a read-only portfolio view. It does not provide implementation-ticket creation, worker reassignment, lane log viewing or direct worktree controls.
- Live UI updates use a plugin event stream backed by committed registry/outbox/projection events. A reconnect starts from a cursor and performs a full projection read if the cursor is no longer retained.
- Tracker unavailability puts the board into stale read-only mode. The UI may display the last successful projection and sync timestamp but cannot accept transitions that would exist only locally.
- Production authority will be strengthened with credential separation: PM has Issue-write and repository-read access, implementation workers have local Git access without publication credentials, and publication credentials are made available only through a chairman-approved publisher path. The prototype may use an existing authenticated `gh` session but must label that as workflow enforcement rather than a security boundary.
- Remote publication is not part of the PM's default authority. Without a publication grant, delivery-pipeline stops after verified local integration and reports a single acceptance/publication gate.
- Setup and doctor flows validate the CEO and PM profiles, external Skill directories, required owner Skills, GitHub authentication, Herdr binary, Hermes/Claude/Codex integrations, writable plugin data and project repository coordinates.
- Repair rebuilds board projections from tracker truth, resolves CEO session lineages, verifies PM Herdr coordinates, requeues unfinished Outbox records and reports ambiguous duplicates without deleting data automatically.
- Phase 1 delivers external plugin packaging, GitHub Map binding, project board projection, manual stage transitions and canonical CEO sessions. No automatic PM runtime is started.
- Phase 2 adds CEO/PM Skills, role-specific plugin Tools, structured decisions and explicit chairman approval.
- Phase 3 adds the plugin-owned Herdr session, Hermes PM startup, delivery-pipeline execution, Codex/Claude routing, runtime recovery and executive checkpoints.
- Phase 4 adds autonomous CEO wake/resume, persistent Outbox routing, credential-separated publication, portfolio aggregation and operational repair tooling.

## Testing Decisions

- The primary test seam is the single Map Governance application interface used by REST, Tools and CLI adapters. Tests invoke externally meaningful operations such as bind Map, transition stage, request decision, approve, commission, checkpoint, resume and accept, while substituting Tracker, Session Runner and Coordinator Runtime adapters. Tests assert returned projections and observable adapter effects rather than private helper calls or SQL layout.
- Thin contract tests cover the dashboard REST adapter and the two plugin Tool adapters to prove they delegate to the same application interface, preserve request-scoped identity and expose consistent errors. Business policy is not duplicated in adapter tests.
- A real temporary Hermes home is used for integration tests so plugin discovery, profile scoping, plugin data, session persistence and project resolution exercise production imports and SQLite behavior rather than mocked module globals.
- Canonical-session tests cover exact-title adoption, concurrent double-open, empty-session persistence after bootstrap, missing session recovery, compression-lineage resolution, profile isolation, transient hydration failure and restart reopening. Prior art is the existing canonical Bot Chat and Desktop profile-routing test families.
- Board API tests cover one-project/multiple-Map grouping, cross-project isolation, read-only portfolio aggregation, stale projections, invalid stage transitions, partial tracker failure and reconnect from an event cursor. Prior art is the existing Kanban dashboard plugin API and WebSocket event tests.
- Governance state tests cover every allowed and forbidden actor transition, including CEO decisions inside the authority envelope, chairman-required actions, expired approvals, payload-hash mismatch, duplicate approval submission, decision stage only for whole-Map blockers and acceptance returning to delivery.
- Single-source tests prove that a board transition is not visible until the Tracker Adapter confirms it; clearing the projection cache and rebuilding from tracker data yields the same board; local registry data alone cannot create an authoritative stage.
- Tool authorization tests run simultaneous CEO and PM sessions in one process and verify request-scoped ContextVars prevent cross-profile and cross-Map authority leakage. Process environment values are intentionally made misleading to catch accidental ambient reads.
- Prompt-caching tests compare the persisted system prompt across turns in the same CEO conversation and verify that Map-specific bootstrap data appears only in conversation history. Compression must not rebuild a different system prompt.
- Outbox tests simulate crash-after-tracker-write, crash-before-ack, duplicate delivery, timeout, retry exhaustion and restart. Each scenario must produce one tracker decision/comment and one terminal event despite at-least-once processing.
- Herdr runtime unit tests use a command-runner adapter and assert that every command includes the plugin-owned named session and recorded opaque IDs. The adapter must refuse unknown or unowned workspaces, tabs, panes and agents.
- Opt-in Herdr integration tests use an isolated named test session, never the user's default session. They create one workspace, start Hermes PM, verify `HERDR_ENV=1`, prompt the Agent, read the returned session coordinate and clean up only resources created by the test.
- Delivery integration tests use a fake delivery-pipeline completion event to prove implementation tickets and lane coordinates do not appear in board responses. A separate opt-in smoke test may run a minimal real PM-to-worker lane after the Herdr integrations are installed.
- Publication-policy tests prove PM behavior stops at local integration without a grant, worker environments do not receive publisher credentials, rejected/expired grants cannot publish and an approved publisher action records remote evidence before closing the Map.
- Accessibility and UX tests cover keyboard card navigation, non-drag transition controls, confirmation for major actions, focus restoration after dialogs, readable stale/error states, approval packet semantics and screen-reader labels for stage and decision badges.
- Security tests cover plugin API authentication, cross-plugin route isolation, unsafe tracker content rendering, Markdown/link escaping, command argument construction without shell interpolation, secret redaction and denial of project/plugin traversal paths.
- Recovery tests begin with a populated real temporary home, terminate and restart the backend, then verify identical project boards, CEO session reuse, pending approvals, Outbox recovery and PM runtime reconciliation.
- Good tests assert relationships and invariants rather than fixed counts, literal generated IDs, current model lists or snapshotting full response objects. Model calls are avoided in deterministic policy tests and used only in explicit end-to-end smoke coverage.

## Out of Scope

- Replacing or redesigning the existing Hermes Kanban dispatcher, task database, worker claim model or execution dashboard.
- Displaying implementation tickets, Codex/Claude pane transcripts, worktree lists, commit logs or CI logs as first-class cards on the CEO board.
- Adding new Hermes core model Tools or placing governance tools into default platform toolsets.
- Making Codex Desktop native thread tools available to Hermes PM. The selected runtime for Hermes-hosted PM execution is Herdr.
- Supporting tracker providers other than GitHub in the first release. The Tracker Adapter seam permits later providers without defining speculative implementations now.
- Migrating running delivery lanes between Codex, Claude, Herdr and Codex Desktop runtimes.
- Automatically modifying existing user Herdr workspaces, sessions, panes or agent integrations without an explicit setup action.
- Treating behavioral prompt instructions as a hard security sandbox. Strong publication enforcement requires credential separation and remains a later rollout phase if the prototype begins with broad local credentials.
- Replacing Wayfinder, `delivery-pipeline`, spec generation, ticket generation, implementation or code-review owner Skills.
- Publishing this draft Spec to an issue tracker or applying `ready-for-agent`; the requested artifact is temporary and local only.
- Building a generalized enterprise portfolio, accounting, billing, resource forecasting or multi-tenant RBAC product in the first release.
- Automatically deleting ambiguous duplicate sessions, tracker records, worktrees or Herdr resources during repair.

## Further Notes

- The chosen vocabulary is: Chairman for investment authority, Hermes CEO for Map governance, Hermes PM for delivery coordination, Map Issue for governance truth, Spec for the product contract, implementation tickets for the execution graph, lane registry for worker coordinates, Git for code truth and PR/MR plus CI/CD for publication truth.
- Current local readiness already includes Hermes CLI, Herdr, Codex CLI, Claude Code and authenticated `gh`. Herdr's Hermes and Claude integrations are available; the Codex integration must be installed explicitly before mixed Codex/Claude dispatch is considered ready.
- The current broad GitHub authentication is adequate for a controlled prototype but does not enforce the intended publisher boundary. Production acceptance should require a fine-grained credential plan or equivalent publisher isolation.
- The highest testing seam selected for this Spec is the Map Governance application interface, with REST, Tool and CLI surfaces remaining thin. This matches Hermes' existing dashboard-plugin pattern of keeping domain logic out of the renderer and transport adapters.
- This draft intentionally keeps the governance plugin outside the existing Kanban task model. A future unification would require an explicit governance task kind and a complete audit of dispatch eligibility, running counts, reclaim, review and notification semantics; visual similarity alone is not sufficient justification.
