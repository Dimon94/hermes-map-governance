# Map Governance installed

If the plugin was installed without `--enable`, activate it first:

```bash
hermes plugins enable map-governance
```

Verify all shell components and plugin-owned storage:

```bash
hermes maps health
```

Then start `hermes dashboard` and open **Maps**. An empty-state board is expected
until an existing GitHub Map Issue is bound:

```bash
hermes maps project configure \
  --url https://github.com/orgs/OWNER/projects/PROJECT_NUMBER
hermes maps bind \
  --project PROJECT_NODE_ID \
  --issue https://github.com/OWNER/REPOSITORY/issues/ISSUE_NUMBER
```

The commands use authenticated, read-only `gh api graphql` requests. Repeating a
bind is idempotent. Run `hermes maps refresh` to rebuild dashboard projections
from GitHub and the plugin-owned binding registry. The GitHub token needs the
`read:project` scope.

Open the bound Map from its dashboard card, or resolve the same canonical
conversation explicitly:

```bash
hermes maps open --map ISSUE_NODE_ID --profile CEO_PROFILE
```

The first open adopts one exact prior session or creates and bootstraps one.
`repair_required` means multiple exact candidates were found; the plugin does
not choose or delete one automatically.
