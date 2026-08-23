# Map Governance installed

If the plugin was installed without `--enable`, activate it first:

```bash
hermes plugins enable map-governance
```

Verify all shell components and plugin-owned storage:

```bash
hermes maps health
hermes maps doctor
```

`maps doctor` is strictly read-only. It does not initialize missing plugin
storage, install Herdr integrations, create profiles, register Skills, log in to
GitHub, or repair bindings. A missing prerequisite includes an argv-array
remediation in the JSON report for an operator to review and run separately.

Before the first binding, create an explicit desired-state JSON file and use
the two-phase workflow:

```bash
hermes maps setup plan --file map-governance-setup.json > setup-plan.json
hermes maps setup apply \
  --file setup-plan.json \
  --action config.prerequisites
```

Setup rejects secret fields. Reference the existing `gh` auth/keychain context
as `gh:HOST:ACCOUNT` instead of copying a token into plugin config; the active
account must match that reference. The worker authority is local
Git and remains separate from the optional publisher authority. Integration,
Skill, credential, and profile installation remain explicit operator actions;
setup/doctor never run those remediation commands automatically.
Behavioral expectations live in the current profile's plugin-owned
`prerequisites.yaml`; setup never rewrites the shared Hermes `config.yaml`.

The Maps dashboard exposes the same setup workflow: paste desired JSON, inspect
the secret-safe before/after plan, select each stable action ID explicitly, and
confirm apply. A successful apply reports verified config readback.

Then start `hermes dashboard` and open **Maps**. An empty-state board is expected
until an existing GitHub Map Issue is bound:

```bash
hermes maps project configure \
  --url https://github.com/orgs/OWNER/projects/PROJECT_NUMBER
hermes maps bind \
  --project PROJECT_NODE_ID \
  --issue https://github.com/OWNER/REPOSITORY/issues/ISSUE_NUMBER
```

Binding and refresh use authenticated `gh api graphql` reads. Governed stage
transitions and CEO decision recording require Issue write access. Repeating a
bind is idempotent. Run `hermes maps refresh` to rebuild dashboard projections,
including confirmed decisions, from GitHub and the plugin-owned binding
registry. The GitHub token needs the relevant Project read and Issue write
permissions.

The Maps page resumes its committed event cursor after renderer disconnects.
A renderer-only disconnect does not make governance truth stale. If GitHub
authority becomes unreachable, the affected project remains readable but all
governance writes fail closed until `hermes maps refresh --project PROJECT_NODE_ID`
successfully fetches and reconciles the authoritative Project, Issues, decisions,
approvals, and PM reports. A reconnect or cached projection alone does not clear
stale mode.

Open the bound Map from its dashboard card, or resolve the same canonical
conversation explicitly:

```bash
hermes maps open --map ISSUE_NODE_ID --profile CEO_PROFILE
```

The first open adopts one exact prior session or creates and bootstraps one.
`repair_required` means multiple exact candidates were found; the plugin does
not choose or delete one automatically.
Every adopted, newly created, or compression-resumed live lineage also receives
one idempotent CEO Skill user turn, so existing #6 sessions gain the same role
and negative-capability instructions without changing their system prompt or
toolset.

After the chairman-approved delivery authorization has been consumed and the
Map is `authorized`, explicitly commission its one root Hermes PM:

```bash
hermes maps commission \
  --map ISSUE_NODE_ID \
  --profile CEO_PROFILE \
  --session CANONICAL_CEO_SESSION_ID

hermes maps resume \
  --map ISSUE_NODE_ID \
  --profile CEO_PROFILE \
  --session CANONICAL_CEO_SESSION_ID

hermes maps runtime status \
  --map ISSUE_NODE_ID \
  --profile CEO_PROFILE \
  --session CANONICAL_CEO_SESSION_ID
```

Commissioning fails before Herdr mutation unless the request-scoped CEO
identity, consumed authorization, Project/repository binding, PM profile,
Skills, routing policy, and selected Herdr integrations all pass. The plugin
creates or resumes only its deterministic owned namespace; a same-name resource
without durable ownership proof is reported as `repair_required` and is never
attached, renamed, killed, or deleted. A started PM does not make the Map active:
the expected structured ready checkpoint must first be confirmed in GitHub Issue
history, followed by the existing tracker-confirmed `authorized → delivery`
transition. Retrying resumes the same workspace and PM.

The plugin registers the explicit `map-governance:ceo` Skill and the narrow
`map-governance-ceo` toolset. New canonical CEO conversations persist a stable
system prompt and the required toolset identity; Map-specific bootstrap data
remains in the first user turn. Configure the dedicated CEO profile to expose
this named toolset through normal Hermes profile toolset settings. The plugin's
request-scoped tool policy also blocks every other tool inside canonical CEO
sessions. The CEO Tool can inspect executive state, record an autonomous
structured decision, or submit a complete chairman approval packet. It cannot
approve its own packet, control worker lanes, edit implementation worktrees,
or publish code. Open Map detail to approve, reject, or request revision
explicitly; each result is confirmed in GitHub Issue history before the local
ledger projects it.

The default authority envelope and 24-hour approval TTL come from the plugin's
normal `plugins.entries.map-governance.settings.authority` configuration. A
protected `awaiting-approval → authorized` transition needs the approved
request id and a stable mutation id; missing, expired, revoked, consumed, or
content-mismatched grants fail before the tracker transition.

Before using Map detail approval controls, add the authenticated chairman
identity (`provider:user_id`) to that profile's `chairman_actor_ids`; the
default allowlist is empty. Optional `authority_thresholds` can grant CEO
autonomy inside numeric budget/schedule ceilings or configured scope values.
Missing or malformed threshold evidence remains chairman-required.

This phase uses the authenticated `gh` identity of the plugin process for
tracker writes, as defined by the prototype security boundary. Structured
comments written by GitHub collaborators are authoritative Issue history.
Role-specific toolsets and request policy provide workflow separation here;
production credential separation is a later hardening step, and local delivery
never grants remote publication authority.

The plugin also registers `map-governance:pm` and the independent
`map-governance-pm` toolset. PM model calls do not accept profile, session, Map,
chairman approval, publication, or worker-control coordinates. The host resolves
one immutable Map assignment from the active request and records only concise
tracker-confirmed executive reports. Whole-Map blockers may enter `decision`,
terminal failures do not change governance stage, acceptance outcome evidence may
request `acceptance`, and non-blocking questions remain in `delivery`. A confirmed
report or coordinator-confirmed dispatch ends the PM turn idle; failed reporting
stays in the active turn for identical-payload retry. The PM contract fixes
`dispatch_runtime: herdr`. This plugin commissions the single root PM runtime;
implementation worker-lane routing remains outside this phase.
